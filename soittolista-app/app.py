"""Soittolista: paikallinen puhelinmyynnin soittolistasovellus (FastAPI + SQLite)."""
import csv
import importlib.util
import io
import re
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
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
    "y_tunnus": ["y_tunnus"],
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
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    title TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company_id);
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
    migrate(conn)
    conn.commit()
    conn.close()


def migrate(conn):
    """Turvalliset, toistettavat migraatiot: vain lisäyksiä, ei poistoja eikä uudelleenkirjoitusta."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    if "y_tunnus" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN y_tunnus TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_c_ytunnus ON companies(y_tunnus)")
    # Täytä puuttuva y-tunnus muistiinpanoista ("Y-tunnus 1234567-8"), jonka 02_enrich_places.py kirjoitti.
    # Ei koskaan ylikirjoita olemassa olevaa arvoa eikä muokkaa muistiinpanoja.
    todo = conn.execute(
        "SELECT id, notes FROM companies "
        "WHERE COALESCE(y_tunnus,'')='' AND notes LIKE '%Y-tunnus%'").fetchall()
    for cid, notes in todo:
        m = re.search(r"Y-tunnus\s+(\d{7}-\d)", notes or "", re.I)
        if m:
            conn.execute("UPDATE companies SET y_tunnus=? WHERE id=? AND COALESCE(y_tunnus,'')=''",
                         (m.group(1), cid))


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


def find_existing(conn, rec, nc_index=None):
    """Etsii rekisteristä saman yrityksen. Palauttaa (id, mistä) tai (None, None).

    Järjestys: y_tunnus, place_id, maps_url, phone, domain (ketjuilla ei domainia), ja vain jos
    nc_index annetaan, täsmälleen sama nimi + kaupunki ("name_city"). Mikään heikompi avain
    (place_id, maps_url, phone, domain, name_city) ei yhdistä yritystä, jolla on jo eri y-tunnus,
    eikä name_city yhdistä ketjun toimipisteitä.
    """
    checks = []
    if rec.get("y_tunnus"):
        checks.append(("y_tunnus", "y_tunnus", rec["y_tunnus"]))
    if rec.get("place_id"):
        checks.append(("place_id", "place_id", rec["place_id"]))
    if rec.get("maps_url"):
        checks.append(("maps_url", "maps_url", rec["maps_url"]))
    if rec.get("phone_norm"):
        checks.append(("phone", "phone_norm", rec["phone_norm"]))
    # Ketjun toimipisteet jakavat usein verkkosivun, joten ketjuille ei yhdistetä domainilla
    if rec.get("domain") and not rec.get("chain"):
        checks.append(("domain", "domain", rec["domain"]))
    for via, col, val in checks:  # col tulee vakioista, ei käyttäjältä
        for r in conn.execute(f"SELECT id, y_tunnus FROM companies WHERE {col}=?", (val,)):
            # Eri y-tunnus = eri yritys, vaikka verkkosivu/puhelin olisi sama (esim. konsernin yhteinen sivu)
            if via != "y_tunnus" and rec.get("y_tunnus") and r["y_tunnus"] \
                    and r["y_tunnus"] != rec["y_tunnus"]:
                continue
            return r["id"], via
    if nc_index is not None and not rec.get("chain") and rec.get("name") and rec.get("city"):
        for cid, ytunnus, _name in nc_index.get(nc_key(rec["name"], rec["city"]), []):
            if not ytunnus or ytunnus == rec.get("y_tunnus"):
                return cid, "name_city"
    return None, None


def nc_key(name, city):
    return (" ".join((name or "").split()).casefold(), (city or "").strip().casefold())


def name_city_index(conn):
    """{(nimi, kaupunki): [(id, y_tunnus, nimi)]} ei-ketjuyrityksistä nimi+kaupunki-vertailuun."""
    idx = {}
    for r in conn.execute("SELECT id, name, city, y_tunnus FROM companies WHERE chain=0"):
        idx.setdefault(nc_key(r["name"], r["city"]), []).append(
            (r["id"], r["y_tunnus"] or "", r["name"]))
    return idx


def upsert_company(conn, rec, list_id, nc_index=None):
    """Lisää yrityksen tai täydentää puuttuvat kentät vanhaan. Palauttaa ("added"|"matched", mistä)."""
    existing, via = find_existing(conn, rec, nc_index)
    if existing:
        conn.execute(
            """UPDATE companies SET
               phone = CASE WHEN COALESCE(phone,'')='' THEN ? ELSE phone END,
               phone_norm = CASE WHEN COALESCE(phone_norm,'')='' THEN ? ELSE phone_norm END,
               website = CASE WHEN COALESCE(website,'')='' THEN ? ELSE website END,
               domain = CASE WHEN COALESCE(domain,'')='' THEN ? ELSE domain END,
               place_id = CASE WHEN COALESCE(place_id,'')='' THEN ? ELSE place_id END,
               maps_url = CASE WHEN COALESCE(maps_url,'')='' THEN ? ELSE maps_url END,
               y_tunnus = CASE WHEN COALESCE(y_tunnus,'')='' THEN ? ELSE y_tunnus END
               WHERE id=?""",
            (rec["phone"], rec["phone_norm"], rec["website"], rec["domain"], rec["place_id"],
             rec["maps_url"], rec.get("y_tunnus", ""), existing))
        conn.execute("INSERT OR IGNORE INTO company_lists VALUES (?,?)", (existing, list_id))
        return "matched", via

    given = (rec.get("priority") or "").upper()
    prio = given if given in ("A", "B", "C") else compute_priority(
        rec["real_site"], rec["reviews"], rec["chain"], bool(rec["phone"]))
    size = rec.get("size", "")
    size = size if size in SIZES else ""
    now = now_iso()
    cid = conn.execute(
        """INSERT INTO companies
           (name, phone, phone_norm, website, domain, address, city, reviews, score, chain,
            category, place_id, maps_url, priority, size_estimate, notes, status, y_tunnus,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec["name"], rec["phone"], rec["phone_norm"], rec["website"], rec["domain"],
         rec.get("address", ""), rec.get("city", ""), rec["reviews"], rec.get("score"),
         int(rec["chain"]), rec.get("category", ""), rec["place_id"], rec["maps_url"], prio,
         size, rec.get("notes", ""), "Ei soitettu", rec.get("y_tunnus", ""), now, now)).lastrowid
    conn.execute("INSERT INTO company_lists VALUES (?,?)", (cid, list_id))
    return "added", None


def rec_from_website(name, website, phone, chain_flag=False):
    """Yhteiset johdetut kentät: domain (vain oikeat sivut), puhelimen normalisointi, ketjuarvaus."""
    domain = domain_of(website)
    real = is_real_website(domain)
    domain = domain if real else ""
    return {"domain": domain, "real_site": real, "phone_norm": norm_phone(phone),
            "chain": is_chain(name, domain) or chain_flag}


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
    d["contacts"] = get_contacts(conn, cid)
    return d


def get_contacts(conn, cid):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM contacts WHERE company_id=? ORDER BY name COLLATE NOCASE, id", (cid,))]


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
        chain_flag = pick(row, "chain").lower() in ("kyllä", "kylla", "true", "1", "yes")
        rec = {
            "name": cname, "phone": phone, "website": website,
            **rec_from_website(cname, website, phone, chain_flag),
            "address": pick(row, "address"), "city": pick(row, "city"),
            "reviews": to_int(pick(row, "reviews")), "score": to_float(pick(row, "score")),
            "category": pick(row, "category"), "place_id": pick(row, "place_id"),
            "maps_url": pick(row, "maps_url"), "priority": pick(row, "priority"),
            "size": pick(row, "size"), "notes": pick(row, "notes"),
            "y_tunnus": pick(row, "y_tunnus"),
        }
        result, _via = upsert_company(conn, rec, list_id)
        if result == "matched":
            matched += 1
        else:
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
    name: Optional[str] = None
    size_estimate: Optional[str] = None
    notes: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    category: Optional[str] = None
    priority: Optional[str] = None
    chain: Optional[bool] = None
    status: Optional[str] = None
    next_call_date: Optional[str] = None
    do_not_call: Optional[bool] = None


@app.patch("/api/companies/{cid}")
def patch_company(cid: int, body: CompanyPatch, conn=Depends(get_db)):
    if not conn.execute("SELECT 1 FROM companies WHERE id=?", (cid,)).fetchone():
        raise HTTPException(404, "Yritystä ei löytynyt")
    data = body.model_dump(exclude_unset=True)
    sets, params = [], []

    if "name" in data:
        nm = (data["name"] or "").strip()
        if not nm:
            raise HTTPException(400, "Yrityksen nimi ei voi olla tyhjä")
        sets.append("name=?")
        params.append(nm)
    for col in ("address", "city", "category"):
        if col in data:
            sets.append(f"{col}=?")  # col tulee vakioista, ei käyttäjältä
            params.append((data[col] or "").strip())
    if "priority" in data:
        if data["priority"] not in ("A", "B", "C"):
            raise HTTPException(400, "Virheellinen prioriteetti")
        sets.append("priority=?")
        params.append(data["priority"])
    if "chain" in data:
        sets.append("chain=?")
        params.append(int(bool(data["chain"])))
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


class ContactIn(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None


CONTACT_FIELDS = ("name", "title", "phone", "email", "notes")


@app.get("/api/companies/{cid}/contacts")
def list_contacts(cid: int, conn=Depends(get_db)):
    if not conn.execute("SELECT 1 FROM companies WHERE id=?", (cid,)).fetchone():
        raise HTTPException(404, "Yritystä ei löytynyt")
    return get_contacts(conn, cid)


@app.post("/api/companies/{cid}/contacts")
def create_contact(cid: int, body: ContactIn, conn=Depends(get_db)):
    if not conn.execute("SELECT 1 FROM companies WHERE id=?", (cid,)).fetchone():
        raise HTTPException(404, "Yritystä ei löytynyt")
    vals = {k: (getattr(body, k) or "").strip() for k in CONTACT_FIELDS}
    if not vals["name"]:
        raise HTTPException(400, "Yhteyshenkilön nimi puuttuu")
    now = now_iso()
    conn.execute(
        "INSERT INTO contacts(company_id, name, title, phone, email, notes, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (cid, vals["name"], vals["title"], vals["phone"], vals["email"], vals["notes"], now, now))
    return get_contacts(conn, cid)


@app.patch("/api/contacts/{contact_id}")
def patch_contact(contact_id: int, body: ContactIn, conn=Depends(get_db)):
    row = conn.execute("SELECT company_id FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Yhteyshenkilöä ei löytynyt")
    data = body.model_dump(exclude_unset=True)
    sets, params = [], []
    for k in CONTACT_FIELDS:
        if k in data:
            v = (data[k] or "").strip()
            if k == "name" and not v:
                raise HTTPException(400, "Yhteyshenkilön nimi puuttuu")
            sets.append(f"{k}=?")  # k tulee vakioista, ei käyttäjältä
            params.append(v)
    if sets:
        sets.append("updated_at=?")
        params += [now_iso(), contact_id]
        conn.execute(f"UPDATE contacts SET {', '.join(sets)} WHERE id=?", params)
    return get_contacts(conn, row["company_id"])


@app.delete("/api/contacts/{contact_id}")
def delete_contact(contact_id: int, conn=Depends(get_db)):
    row = conn.execute("SELECT company_id FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Yhteyshenkilöä ei löytynyt")
    conn.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
    return get_contacts(conn, row["company_id"])


# ---------- PRH-haku (Hae toimiala) ----------
# Hakulogiikka on repon juuren 01_fetch_prh.py:ssä (sama koodi kuin komentorivillä). Ei Google Places -kutsuja.

PRH_CACHE = {}  # hakuavain -> (aikaleima, tulos); sama haku esikatselusta tuontiin ilman uutta hakua
PRH_TTL = 30 * 60
_prh_mod = None


def prh_module():
    global _prh_mod
    if _prh_mod is None:
        path = BASE.parent / "01_fetch_prh.py"
        if not path.exists():
            raise HTTPException(503, "Tiedostoa 01_fetch_prh.py ei löydy projektin juuresta.")
        try:
            spec = importlib.util.spec_from_file_location("fetch_prh", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except ModuleNotFoundError as e:
            raise HTTPException(
                503, f"Puuttuva kirjasto: {e.name}. Asenna: .venv\\Scripts\\pip install -r requirements.txt")
        _prh_mod = mod
    return _prh_mod


class PrhSearch(BaseModel):
    name: str = ""
    line_codes: str = ""
    keywords: str = ""
    city: str = "Helsinki"
    company_form: str = ""
    restrict_to_code: bool = True
    include_inactive: bool = False


class PrhImport(PrhSearch):
    list_name: str = ""


def split_terms(s):
    """Pilkulla/puolipisteellä/rivinvaihdolla erotetut haut; ilman niitä välilyönnillä."""
    s = (s or "").strip()
    if not s:
        return []
    parts = re.split(r"[,;\n]+", s) if re.search(r"[,;\n]", s) else s.split()
    return [p.strip() for p in parts if p.strip()]


def prh_run(body):
    codes, keywords, names = split_terms(body.line_codes), split_terms(body.keywords), split_terms(body.name)
    city = body.city.strip()
    if not city:
        raise HTTPException(400, "Kaupunki puuttuu")
    if not (codes or keywords or names):
        raise HTTPException(400, "Anna vähintään nimi, TOL-koodi tai avainsana")
    if any(not re.fullmatch(r"\d{2,6}", c) for c in codes):
        raise HTTPException(400, "TOL-koodin pitää olla numeroita, esim. 69201")
    only_lines = codes if body.restrict_to_code else []
    form = body.company_form.strip()
    key = (tuple(codes + keywords), tuple(names), city.casefold(), form.casefold(),
           tuple(only_lines), bool(body.include_inactive))

    now = time.time()
    for k in [k for k, (t, _) in PRH_CACHE.items() if now - t > PRH_TTL]:
        del PRH_CACHE[k]
    if key in PRH_CACHE:
        return PRH_CACHE[key][1]

    mod = prh_module()
    try:
        res = mod.search_companies(
            location=city, lines=codes + keywords, names=names, forms=[form] if form else None,
            only_lines=only_lines or None, include_inactive=body.include_inactive, log=lambda m: None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"PRH-haku epäonnistui: {e}")
    if res["errors"] and res["total_unique"] == 0:
        raise HTTPException(502, "PRH-haku epäonnistui: " + "; ".join(res["errors"]))
    PRH_CACHE[key] = (now, res)
    return res


def rec_from_prh(r):
    name, website, phone = r.get("nimi", ""), r.get("verkkosivu", ""), r.get("puhelin", "")
    return {
        "name": name, "phone": phone, "website": website,
        **rec_from_website(name, website, phone),
        "address": r.get("osoite", ""), "city": (r.get("kaupunki") or "").title(),
        "reviews": 0, "score": None, "category": r.get("toimiala", ""),
        "place_id": "", "maps_url": "", "priority": "", "size": "", "notes": "",
        "y_tunnus": r.get("y_tunnus", ""),
    }


@app.post("/api/prh/preview")
def prh_preview(body: PrhSearch, conn=Depends(get_db)):
    res = prh_run(body)
    nc = name_city_index(conn)
    by = {}
    new = existing = with_website = other_city = 0
    pairs, sample = [], []
    want_city = body.city.strip().casefold()
    for r in res["rows"]:
        rec = rec_from_prh(r)
        cid, via = find_existing(conn, rec, nc)
        if cid:
            existing += 1
            by[via] = by.get(via, 0) + 1
            if via == "name_city" and len(pairs) < 20:
                db_name = next((n for i, _y, n in nc.get(nc_key(rec["name"], rec["city"]), []) if i == cid), "")
                pairs.append({"prh_nimi": rec["name"], "db_nimi": db_name, "kaupunki": rec["city"]})
        else:
            new += 1
        if r.get("verkkosivu"):
            with_website += 1
        if rec["city"] and rec["city"].casefold() != want_city:
            other_city += 1
        if len(sample) < 20:
            sample.append({
                "nimi": r.get("nimi", ""), "y_tunnus": r.get("y_tunnus", ""),
                "toimiala": r.get("toimiala", ""), "kaupunki": rec["city"],
                "verkkosivu": r.get("verkkosivu", ""),
                "tila": f"olemassa ({via})" if cid else "uusi"})
    return {
        "total_unique": res["total_unique"], "matched": len(res["rows"]), "new": new,
        "existing": existing, "existing_by": by, "name_city_pairs": pairs,
        "with_website": with_website, "other_city": other_city,
        "truncated": res["truncated"], "errors": res["errors"], "sample": sample,
    }


@app.post("/api/prh/import")
def prh_import(body: PrhImport, conn=Depends(get_db)):
    res = prh_run(body)
    rows = res["rows"]
    if not rows:
        raise HTTPException(400, "Haulla ei löytynyt tuotavia yrityksiä")
    name = body.list_name.strip() or f"PRH {body.city.strip()} {today_iso()}"
    list_id = conn.execute(
        "INSERT INTO lists(name, imported_at) VALUES (?,?)", (name, now_iso())).lastrowid
    nc = name_city_index(conn)
    added = matched = 0
    by = {}
    for r in rows:
        result, via = upsert_company(conn, rec_from_prh(r), list_id, nc)
        if result == "matched":
            matched += 1
            by[via] = by.get(via, 0) + 1
        else:
            added += 1
    return {"list_id": list_id, "list_name": name, "added": added, "existing": matched,
            "existing_by": by}


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


@app.get("/company/{cid}")
def company_page(cid: int):
    """Yrityssivu: sama yhden tiedoston frontend, joka tunnistaa polun itse."""
    return FileResponse(BASE / "static" / "index.html")


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
