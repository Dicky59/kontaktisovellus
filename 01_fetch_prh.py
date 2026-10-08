"""
Hakee yritykset PRH:n avoimesta YTJ-rajapinnasta (v3) ja tallentaa ne CSV:ksi.
Ei vaadi API-avainta eikä Google Cloudia.

Käyttö (projektin juuresta, venv aktiivisena, tarvitsee vain 'requests'):
    python 01_fetch_prh.py                     # oletushaut, vertailu
    python 01_fetch_prh.py --forms 16          # vain osakeyhtiöt (koodi 16; myös --forms osakeyhtiö)
    python 01_fetch_prh.py --only-lines 69201  # vain tämän toimialakoodin rivit
    python 01_fetch_prh.py --line 69201 --name tilitoimisto --location Helsinki
    python 01_fetch_prh.py --all               # mukaan myös lopettaneet

Toimialakoodit: esimerkkivastauksessa 69202 = tilintarkastus (Auditing). Kirjanpito/tilitoimistot
ovat todennäköisesti 69201, mutta varmista koodi ajon yhteenvedon "Toimialat"-listasta.

Rajapinta (skeema: https://avoindata.prh.fi/opendata-ytj-api/v3/schema?lang=en):
  - hakuparametrit: name, location, mainBusinessLine (TOL-koodi tai teksti), companyForm,
    postCode, page. Enintään 100 tulosta per sivu.
  - PRH ei kerro henkilömäärää eikä (yleensä) puhelinnumeroa.

HUOM toimialahaku: rajapinnan dokumentaatio puhuu TOL 2008 -koodeista, mutta toimialaluokitus on
päivittynyt (TOL 2025), ja vanhoilla koodeilla voi löytyä lähinnä lopettaneita yrityksiä.
Siksi skripti ajaa oletuksena sekä toimialahaun että nimihaun ja vertailee tuloksia.
"""
import argparse
import csv
import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path

import requests

BASE = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies"
PAGE_SIZE = 100
MAX_PAGES = 60  # suoja: 6000 yritystä per haku

DEFAULT_LINES = ["69201", "kirjanpito"]
DEFAULT_NAMES = ["tilitoimisto", "kirjanpito"]


def get(params, retries=5):
    for attempt in range(retries):
        r = requests.get(BASE, params=params, timeout=30,
                         headers={"User-Agent": "tilitoimisto-leads/1.0"})
        if r.status_code == 429:
            wait = 5 * (attempt + 1)
            print(f"  429 (liian monta pyyntöä), odotetaan {wait} s…")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(3)
            continue
        if r.status_code == 400:
            raise RuntimeError(f"PRH palautti 400: {r.text[:300]}")
        r.raise_for_status()
        return r.json()
    raise RuntimeError("PRH-rajapinta ei vastannut (429/5xx useita kertoja).")


def fetch_all(params):
    first = get({**params, "page": 1})
    total = first.get("totalResults", 0)
    companies = list(first.get("companies", []))
    pages = math.ceil(total / PAGE_SIZE)
    if pages > MAX_PAGES:
        print(f"  Varoitus: {total} tulosta, haetaan vain {MAX_PAGES} sivua. Rajaa hakua.")
        pages = MAX_PAGES
    for page in range(2, pages + 1):
        time.sleep(0.4)
        companies += get({**params, "page": page}).get("companies", [])
    return total, companies


def desc(entries, prefer=("1", "3", "2")):
    """Kuvaus kielikoodin mukaan: 1 = suomi, 2 = ruotsi, 3 = englanti."""
    by = {str(e.get("languageCode")): e.get("description") for e in entries or []}
    for k in prefer:
        if by.get(k):
            return by[k]
    return next((v for v in by.values() if v), "")


def current_name(c):
    names = [n for n in c.get("names") or [] if n.get("version") == 1] or (c.get("names") or [])
    names.sort(key=lambda n: str(n.get("type")))
    return names[0].get("name", "") if names else ""


def pick_address(c):
    addrs = c.get("addresses") or []
    addr = next((a for a in addrs if a.get("type") == 1), None) or (addrs[0] if addrs else {})
    offices = addr.get("postOffices") or []
    office = next((o for o in offices if str(o.get("languageCode")) == "1"), None) \
        or (offices[0] if offices else {})
    street = (addr.get("street") or "").strip()
    if street:
        # PRH palauttaa talonnumeron, rapun ja huoneiston erillisinä kenttinä
        number = (addr.get("buildingNumber") or "").strip()
        entrance = (addr.get("entrance") or "").strip()
        apt = ((addr.get("apartmentNumber") or "") + (addr.get("apartmentIdSuffix") or "")).strip()
        street = " ".join(p for p in (street, number, entrance, apt) if p)
    else:
        street = (addr.get("freeAddressLine") or "").replace("_", " ").strip()
    return street, addr.get("postCode") or "", office.get("city") or ""


def is_active(c):
    if c.get("endDate"):
        return False
    for s in c.get("companySituations") or []:
        if s.get("type") in ("KONK", "SELTILA") and not s.get("endDate"):
            return False
    return True


PHONE_LIKE = re.compile(r"[+\d][\d\s\-()]{5,}")


def to_row(c, found_by):
    street, post, city = pick_address(c)
    form = (c.get("companyForms") or [{}])[0]
    line = c.get("mainBusinessLine") or {}
    website = ((c.get("website") or {}).get("url") or "").strip()
    phone = ""
    if website and PHONE_LIKE.fullmatch(website):  # PRH:ssa verkkosivukenttään on joskus kirjattu puhelin
        phone, website = website, ""
    return {
        "y_tunnus": (c.get("businessId") or {}).get("value", ""),
        "nimi": current_name(c),
        "yhtiomuoto_koodi": form.get("type", ""),
        "yhtiomuoto": desc(form.get("descriptions")),
        "toimiala_koodi": line.get("type", ""),
        "toimiala": desc(line.get("descriptions")),
        "puhelin": phone,
        "verkkosivu": website,
        "osoite": street,
        "postinumero": post,
        "kaupunki": city,
        "rekisteroity": c.get("registrationDate") or "",
        "tila": c.get("tradeRegisterStatus") or "",
        "aktiivinen": "kyllä" if is_active(c) else "ei",
        "haku": found_by,
    }


def main():
    ap = argparse.ArgumentParser(description="Hae yritykset PRH:n YTJ-rajapinnasta")
    ap.add_argument("--location", default="Helsinki", help="kotipaikka (oletus Helsinki)")
    ap.add_argument("--line", nargs="*", help="toimiala (TOL-koodi tai teksti), esim. 69202 kirjanpito")
    ap.add_argument("--name", nargs="*", help="nimihaku, esim. tilitoimisto kirjanpito")
    ap.add_argument("--forms", nargs="*",
                    help="rajaa yhtiömuotoon koodilla tai nimellä, esim. 16 tai osakeyhtiö")
    ap.add_argument("--only-lines", nargs="*",
                    help="säilytä vain rivit, joiden toimialakoodi alkaa näillä, esim. 69201")
    ap.add_argument("--all", action="store_true", help="sisällytä myös lopettaneet")
    ap.add_argument("--out", default="data/prh_pohja.csv")
    args = ap.parse_args()

    if args.line is None and args.name is None:
        args.line, args.name = DEFAULT_LINES, DEFAULT_NAMES
    searches = [(f"toimiala={t}", {"mainBusinessLine": t}) for t in (args.line or [])] + \
               [(f"nimi={n}", {"name": n}) for n in (args.name or [])]

    found = {}  # y-tunnus -> (yritys, [hakujen nimet])
    first_raw_saved = False
    for label, extra in searches:
        params = {"location": args.location, **extra}
        print(f"Haku {label} ({args.location})…")
        try:
            total, companies = fetch_all(params)
        except Exception as e:  # noqa: BLE001
            print(f"  Virhe: {e}")
            continue
        active = sum(1 for c in companies if is_active(c))
        note = "  (PRH:n kokonaisluku on suurempi kuin haettujen määrä)" if len(companies) < total else ""
        print(f"  tuloksia {total}, haettu {len(companies)}, aktiivisia {active}{note}")
        if companies and not first_raw_saved:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            sample = Path(args.out).with_name("prh_esimerkkivastaus.json")
            sample.write_text(json.dumps(companies[0], ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  Esimerkkivastaus tallennettu: {sample}")
            first_raw_saved = True
        for c in companies:
            bid = (c.get("businessId") or {}).get("value")
            if not bid:
                continue
            found.setdefault(bid, (c, []))[1].append(label)

    if not found:
        sys.exit("Ei tuloksia. Tarkista haut tai yhteys.")

    rows = [to_row(c, " | ".join(labels)) for c, labels in found.values()]
    total_unique = len(rows)
    if not args.all:
        rows = [r for r in rows if r["aktiivinen"] == "kyllä"]
    if args.forms:
        # Vastaa joko koodia (16) tai nimen osaa (osakeyhtiö)
        wanted = [f.lower() for f in args.forms]
        rows = [r for r in rows
                if r["yhtiomuoto_koodi"].lower() in wanted
                or any(w in r["yhtiomuoto"].lower() for w in wanted if not w.isdigit())]
    if args.only_lines:
        rows = [r for r in rows if any(r["toimiala_koodi"].startswith(p) for p in args.only_lines)]
    rows.sort(key=lambda r: r["nimi"].lower())

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["y_tunnus"])
        w.writeheader()
        w.writerows(rows)

    # Yhteenveto
    print("\n=== Yhteenveto ===")
    print(f"Uniikkeja yrityksiä yhteensä: {total_unique}")
    print(f"Tallennettu: {len(rows)} riviä -> {out}")
    print("Yhtiömuodot:", dict(Counter(
        f"{r['yhtiomuoto_koodi']} {r['yhtiomuoto']}".strip() or "?" for r in rows).most_common(8)))
    print("Toimialat (tallennetuissa riveissä):")
    for (code, name), n in Counter((r["toimiala_koodi"], r["toimiala"]) for r in rows).most_common(10):
        print(f"  {n:4d}  {code} {name}")
    print("Rekisterin tila:", dict(Counter(r["tila"] or "?" for r in rows).most_common(5)))
    print(f"Verkkosivu: {sum(1 for r in rows if r['verkkosivu'])}, "
          f"puhelin: {sum(1 for r in rows if r['puhelin'])}")
    both = sum(1 for r in rows if len(r["haku"].split(" | ")) > 1)
    print(f"Löytyi useammalla haulla: {both}")


if __name__ == "__main__":
    main()