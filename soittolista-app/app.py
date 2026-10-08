"""Soittolista: paikallinen puhelinmyynnin soittolistasovellus (FastAPI + SQLite)."""
import csv
import io
import re
import shutil
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
DATA = BASE / "data"
DB_PATH = DATA / "soittolista.db"

STATUSES = [
    "Ei soitettu", "Ei vastaa", "Soita uudelleen", "Kiinnostunut",
    "Ei kiinnostunut", "Väärä numero", "Ei markkinointisoittoja",
]
OUTCOMES = STATUSES[1:]
SIZES = ["", "1-2", "3-9", "10+"]

CHAIN_KEYWORDS = [
    "talenom", "accountor", "azets", "administer", "visma", "kpmg", "pwc",
    "ernst", "deloitte", "bdo", "grant thornton", "crowe", "baker tilly",
]
SOCIAL_DOMAINS = ("facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com")

# Sarakkeiden mahdolliset nimet: sekä 03_clean_leads.py:n CSV että Apifyn raakavienti
FIELDS = {
    "name": ["nimi", "title", "name"],
    "phone": ["puhelin", "phone", "phoneUnformatted"],
    "website": ["verkkosivu", "website"],
    "address": ["osoite", "street", "address"],
    "city": ["kaupunki", "city"],
    "reviews": ["arvostelut", "reviewsCount"],
    "score": ["arvosana", "totalScore"],
    "chain": ["ketju"],
    "category": ["kategoria", "categoryName"],
    "place_id": ["placeId"],
    "maps_url": ["google_maps", "url"],
    "closed": ["permanentlyClosed"],
    "priority": ["prioriteetti"],
    "size": ["koko_arvio"],
    "notes": ["muistiinpanot"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT DEFAULT '',
    phone_norm TEXT DEFAULT '',
    website TEXT DEFAULT '',
    domain TEXT DEFAULT '',
    address TEXT DEFAULT '',
    city TEXT DEFAULT '',
    reviews INTEGER DEFAULT 0,
    score REAL,
    chain INTEGER DEFAULT 0,
    category TEXT DEFAULT '',
    place_id TEXT DEFAULT '',
    maps_url TEXT DEFAULT '',
    priority TEXT DEFAULT 'C',
    size_estimate TEXT DEFAULT '',
    status TEXT DEFAULT 'Ei soitettu',
    notes TEXT DEFAULT '',
    next_call_date TEXT,
    last_called_at TEXT,
    do_not_call INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_c_phone ON companies(phone_norm);
CREATE INDEX IF NOT EXISTS idx_c_domain ON companies(domain);
CREATE INDEX IF NOT EXISTS idx_c_place ON companies(place_id);
CREATE INDEX IF NOT EXISTS idx_c_maps ON companies(maps_url);
CREATE TABLE IF NOT EXISTS company_lists (
    company_id INTEGER NOT NULL,
    list_id INTEGER NOT NULL,
    PRIMARY KEY (company_id, list_id)
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL,
    called_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_calls_company ON calls(company_id);
"""


# ---------- apufunktiot ----------

def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today_iso():
    return date.today().isoformat()


def backup_db():
    """Ottaa päivittäisen varmuuskopion (säilyttää 14 viimeisintä)."""
    if not DB_PATH.exists():
        return
    bdir = DATA / "backups"
    bdir.mkdir(exist_ok=True)
    target = bdir / f"soittolista-{date.today():%Y%m%d}.db"
    if not target.exists():
        shutil.copy2(DB_PATH, target)
    for old in sorted(bdir.glob("soittolista-*.db"))[:-14]:
        old.unlink()


def init_db():
    DATA.mkdir(exist_ok=True)
    backup_db()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def norm_phone(s):
    digits = re.sub(r"\D", "", s or "")
    if digits.startswith("358"):
        digits = "0" + digits[3:]
    return digits


def domain_of(url):
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_real_website(domain):
    return bool(domain) and not any(domain.endswith(s) for s in SOCIAL_DOMAINS)


def is_chain(name, domain):
    hay = f"{name} {domain}".lower()
    return any(k in hay for k in CHAIN_KEYWORDS)


def compute_priority(real_site, reviews, chain, has_phone):
    if not has_phone or chain:
        return "C"
    if real_site and reviews >= 10:
        return "A"
    if real_site:
        return "B"
    return "C"


def pick(row, key):
    for col in FIELDS[key]:
        val = row.get(col)
        if val is not None:
            val = str(val).strip()
            if val and val.lower() not in ("undefined", "null", "none", "nan"):
                return val
    return ""


def to_int(s):
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def to_float(s):
    try:
        return float(str(s).replace(",", "."))
    except (ValueError, TypeError):
        return None


def parse_csv(raw: bytes):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    first = text.splitlines()[0] if text.strip() else ""
    delim = max([",", ";", "\t"], key=first.count)
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


def find_existing(conn, place_id, maps_url, phone_norm, domain):
    checks = []
    if place_id:
        checks.append(("place_id", place_id))
    if maps_url:
        checks.append(("maps_url", maps_url))
    if phone_norm:
        checks.append(("phone_norm", phone_norm))
    if domain:
        checks.append(("domain", domain))
    for col, val in checks:  # col tulee vakioista, ei käyttäjältä
        r = conn.execute(f"SELECT id FROM companies WHERE {col}=? LIMIT 1", (val,)).fetchone()
        if r:
            return r["id"]
    return None


def get_detail(conn, cid):
    c = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
    if not c:
        return None
    d = dict(c)
    d["calls"] = [dict(r) for r in conn.execute(
        "SELECT id, called_at, outcome, note FROM calls WHERE company_id=? "
        "ORDER BY called_at DESC, id DESC", (cid,))]
    d["lists"] = [r["name"] for r in conn.execute(
        "SELECT l.name FROM lists l JOIN company_lists cl ON cl.list_id=l.id "
        "WHERE cl.company_id=?", (cid,))]
    return d


def valid_date(s):
    return bool(s) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))


def prio_clause(prio):
    letters = [p for p in (prio or "").upper().split(",") if p in ("A", "B", "C")]
    if not letters:
        return "", []
    return f" AND c.priority IN ({','.join('?' * len(letters))})", letters


QUEUE_WHERE = """
    c.do_not_call=0 AND COALESCE(c.phone,'')<>'' AND c.size_estimate<>'1-2'
    AND (
        (c.status='Soita uudelleen' AND c.next_call_date IS NOT NULL AND c.next_call_date<=?)
        OR c.status='Ei soitettu'
        OR (c.status='Ei vastaa' AND date(c.last_called_at)<?)
    )
"""


def queue_query(list_id, prio):
    join, params = "", []
    if list_id:
        join = " JOIN company_lists cl ON cl.company_id=c.id AND cl.list_id=?"
        params.append(list_id)
    today = today_iso()
    params += [today, today]
    pc, pp = prio_clause(prio)
    return join, f" WHERE {QUEUE_WHERE}{pc}", params + pp


# ---------- sovellus ----------

init_db()
app = FastAPI(title="Soittolista")


@app.get("/api/lists")
def get_lists(conn=Depends(get_db)):
    rows = conn.execute(
        "SELECT l.id, l.name, l.imported_at, COUNT(cl.company_id) AS companies "
        "FROM lists l LEFT JOIN company_lists cl ON cl.list_id=l.id "
        "GROUP BY l.id ORDER BY l.id DESC").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/import")
def import_csv(file: UploadFile = File(...), list_name: str = Form(""), conn=Depends(get_db)):
    rows = parse_csv(file.file.read())
    if not rows or not any(pick(r, "name") for r in rows):
        raise HTTPException(400, "CSV:stä ei löytynyt yritysten nimiä (sarake 'nimi' tai 'title').")

    name = list_name.strip() or Path(file.filename or "lista").stem
    list_id = conn.execute(
        "INSERT INTO lists(name, imported_at) VALUES (?,?)", (name, now_iso())).lastrowid

    added = matched = closed = invalid = 0
    for row in rows:
        cname = pick(row, "name")
        if not cname:
            invalid += 1
            continue
        if pick(row, "closed").lower() in ("true", "1", "yes", "kyllä"):
            closed += 1
            continue

        phone, website = pick(row, "phone"), pick(row, "website")
        domain = domain_of(website)
        real = is_real_website(domain)
        domain = domain if real else ""
        pnorm = norm_phone(phone)
        chain = is_chain(cname, domain) or pick(row, "chain").lower() in ("kyllä", "kylla", "true", "1", "yes")
        place_id, maps_url = pick(row, "place_id"), pick(row, "maps_url")

        # Ketjun toimipisteet jakavat usein verkkosivun, joten ketjuille ei yhdistetä domainilla
        existing = find_existing(conn, place_id, maps_url, pnorm, "" if chain else domain)
        if existing:
            conn.execute(
                """UPDATE companies SET
                   phone = CASE WHEN COALESCE(phone,'')='' THEN ? ELSE phone END,
                   phone_norm = CASE WHEN COALESCE(phone_norm,'')='' THEN ? ELSE phone_norm END,
                   website = CASE WHEN COALESCE(website,'')='' THEN ? ELSE website END,
                   domain = CASE WHEN COALESCE(domain,'')='' THEN ? ELSE domain END,
                   place_id = CASE WHEN COALESCE(place_id,'')='' THEN ? ELSE place_id END,
                   maps_url = CASE WHEN COALESCE(maps_url,'')='' THEN ? ELSE maps_url END
                   WHERE id=?""",
                (phone, pnorm, website, domain, place_id, maps_url, existing))
            conn.execute("INSERT OR IGNORE INTO company_lists VALUES (?,?)", (existing, list_id))
            matched += 1
            continue

        reviews = to_int(pick(row, "reviews"))
        given = pick(row, "priority").upper()
        prio = given if given in ("A", "B", "C") else compute_priority(real, reviews, chain, bool(phone))
        size = pick(row, "size")
        size = size if size in SIZES else ""
        now = now_iso()
        cid = conn.execute(
            """INSERT INTO companies
               (name, phone, phone_norm, website, domain, address, city, reviews, score, chain,
                category, place_id, maps_url, priority, size_estimate, notes, status,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cname, phone, pnorm, website, domain, pick(row, "address"), pick(row, "city"),
             reviews, to_float(pick(row, "score")), int(chain), pick(row, "category"),
             place_id, maps_url, prio, size, pick(row, "notes"), "Ei soitettu", now, now)).lastrowid
        conn.execute("INSERT INTO company_lists VALUES (?,?)", (cid, list_id))
        added += 1

    return {"list_id": list_id, "list_name": name, "added": added,
            "existing": matched, "closed": closed, "invalid": invalid}


@app.get("/api/companies")
def list_companies(list_id: Optional[int] = None, priority: str = "", status: str = "",
                   size: str = "", q: str = "", limit: int = 500, offset: int = 0,
                   conn=Depends(get_db)):
    join, where, params = "", ["1=1"], []
    if list_id:
        join = " JOIN company_lists cl ON cl.company_id=c.id AND cl.list_id=?"
        params.append(list_id)
    if priority in ("A", "B", "C"):
        where.append("c.priority=?")
        params.append(priority)
    if status in STATUSES:
        where.append("c.status=?")
        params.append(status)
    if size == "unset":
        where.append("c.size_estimate=''")
    elif size in SIZES and size:
        where.append("c.size_estimate=?")
        params.append(size)
    if q.strip():
        like = f"%{q.strip()}%"
        where.append("(c.name LIKE ? OR c.phone LIKE ? OR c.website LIKE ? OR c.notes LIKE ?)")
        params += [like] * 4
    base = f"FROM companies c{join} WHERE {' AND '.join(where)}"
    total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT c.* {base} ORDER BY c.priority, c.reviews DESC, c.name COLLATE NOCASE "
        f"LIMIT ? OFFSET ?", params + [min(limit, 2000), offset]).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}


@app.get("/api/next")
def next_company(list_id: Optional[int] = None, prio: str = "", skip: str = "",
                 conn=Depends(get_db)):
    join, where, params = queue_query(list_id, prio)
    queue = conn.execute(f"SELECT COUNT(*) FROM companies c{join}{where}", params).fetchone()[0]

    skip_ids = [int(x) for x in skip.split(",") if x.strip().isdigit()]
    skip_sql, skip_params = "", []
    if skip_ids:
        skip_sql = f" AND c.id NOT IN ({','.join('?' * len(skip_ids))})"
        skip_params = skip_ids
    row = conn.execute(
        f"""SELECT c.id FROM companies c{join}{where}{skip_sql}
            ORDER BY CASE c.status WHEN 'Soita uudelleen' THEN 0
                                   WHEN 'Ei soitettu' THEN 1 ELSE 2 END,
                     c.priority, c.reviews DESC, c.id
            LIMIT 1""", params + skip_params).fetchone()
    return {"queue": queue, "company": get_detail(conn, row["id"]) if row else None}


@app.get("/api/companies/{cid}")
def company_detail(cid: int, conn=Depends(get_db)):
    d = get_detail(conn, cid)
    if not d:
        raise HTTPException(404, "Yritystä ei löytynyt")
    return d


class CompanyPatch(BaseModel):
    size_estimate: Optional[str] = None
    notes: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    status: Optional[str] = None
    next_call_date: Optional[str] = None
    do_not_call: Optional[bool] = None


@app.patch("/api/companies/{cid}")
def patch_company(cid: int, body: CompanyPatch, conn=Depends(get_db)):
    if not conn.execute("SELECT 1 FROM companies WHERE id=?", (cid,)).fetchone():
        raise HTTPException(404, "Yritystä ei löytynyt")
    data = body.model_dump(exclude_unset=True)
    sets, params = [], []

    if "size_estimate" in data:
        if data["size_estimate"] not in SIZES:
            raise HTTPException(400, "Virheellinen koko")
        sets.append("size_estimate=?")
        params.append(data["size_estimate"])
    if "notes" in data:
        sets.append("notes=?")
        params.append(data["notes"] or "")
    if "phone" in data:
        sets += ["phone=?", "phone_norm=?"]
        params += [data["phone"] or "", norm_phone(data["phone"])]
    if "website" in data:
        d = domain_of(data["website"])
        sets += ["website=?", "domain=?"]
        params += [data["website"] or "", d if is_real_website(d) else ""]
    if "status" in data:
        if data["status"] not in STATUSES:
            raise HTTPException(400, "Virheellinen tila")
        sets.append("status=?")
        params.append(data["status"])
        if data["status"] == "Soita uudelleen" and not data.get("next_call_date"):
            data["next_call_date"] = (date.today() + timedelta(days=2)).isoformat()
        if data["status"] == "Ei markkinointisoittoja":
            data["do_not_call"] = True
    if "next_call_date" in data:
        nd = data["next_call_date"] or None
        if nd and not valid_date(nd):
            raise HTTPException(400, "Päivämäärän muoto on VVVV-KK-PP")
        sets.append("next_call_date=?")
        params.append(nd)
    if "do_not_call" in data:
        sets.append("do_not_call=?")
        params.append(int(bool(data["do_not_call"])))

    if sets:
        sets.append("updated_at=?")
        params += [now_iso(), cid]
        conn.execute(f"UPDATE companies SET {', '.join(sets)} WHERE id=?", params)
    return get_detail(conn, cid)


class CallIn(BaseModel):
    outcome: str
    note: str = ""
    next_call_date: Optional[str] = None


@app.post("/api/companies/{cid}/calls")
def log_call(cid: int, body: CallIn, conn=Depends(get_db)):
    c = conn.execute("SELECT do_not_call FROM companies WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404, "Yritystä ei löytynyt")
    if body.outcome not in OUTCOMES:
        raise HTTPException(400, "Virheellinen tulos")

    next_date = None
    if body.outcome == "Soita uudelleen":
        next_date = body.next_call_date if valid_date(body.next_call_date) else \
            (date.today() + timedelta(days=2)).isoformat()
    dnc = 1 if body.outcome == "Ei markkinointisoittoja" else c["do_not_call"]
    now = now_iso()
    conn.execute("INSERT INTO calls(company_id, called_at, outcome, note) VALUES (?,?,?,?)",
                 (cid, now, body.outcome, body.note.strip()))
    conn.execute(
        "UPDATE companies SET status=?, last_called_at=?, next_call_date=?, do_not_call=?, "
        "updated_at=? WHERE id=?", (body.outcome, now, next_date, dnc, now, cid))
    return get_detail(conn, cid)


@app.get("/api/stats")
def stats(list_id: Optional[int] = None, prio: str = "", conn=Depends(get_db)):
    join, where, params = queue_query(list_id, prio)
    queue = conn.execute(f"SELECT COUNT(*) FROM companies c{join}{where}", params).fetchone()[0]

    lj, lp = "", []
    if list_id:
        lj = " JOIN company_lists cl ON cl.company_id=c.id AND cl.list_id=?"
        lp = [list_id]
    by_status = {r["status"]: r["n"] for r in conn.execute(
        f"SELECT c.status, COUNT(*) n FROM companies c{lj} GROUP BY c.status", lp)}
    total = sum(by_status.values())
    called_today = conn.execute(
        "SELECT COUNT(*) FROM calls WHERE date(called_at)=?", (today_iso(),)).fetchone()[0]
    return {"queue": queue, "total": total, "by_status": by_status, "called_today": called_today}


@app.get("/api/export.csv")
def export_csv(conn=Depends(get_db)):
    cols = [("priority", "prioriteetti"), ("name", "nimi"), ("phone", "puhelin"),
            ("website", "verkkosivu"), ("address", "osoite"), ("city", "kaupunki"),
            ("reviews", "arvostelut"), ("score", "arvosana"), ("chain", "ketju"),
            ("size_estimate", "koko_arvio"), ("status", "tulos"),
            ("last_called_at", "viimeksi_soitettu"), ("next_call_date", "seuraava_soitto"),
            ("do_not_call", "ei_soittoja"), ("notes", "muistiinpanot"), ("maps_url", "google_maps")]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([h for _, h in cols])
    for r in conn.execute("SELECT * FROM companies ORDER BY priority, reviews DESC, name"):
        row = []
        for k, _ in cols:
            v = r[k]
            if k == "chain":
                v = "kyllä" if v else ""
            elif k == "do_not_call":
                v = "kyllä" if v else ""
            row.append("" if v is None else v)
        w.writerow(row)
    data = ("\ufeff" + out.getvalue()).encode("utf-8")
    return Response(data, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="soittolista-{today_iso()}.csv"'})


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
