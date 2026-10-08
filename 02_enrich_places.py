"""
Hakee puhelinnumerot Google Places API:lla (Text Search, New) PRH-listan yrityksille.

Käyttö (projektin juuresta, venv aktiivisena):
    python 02_enrich_places.py --dry-run          # näyttää montako hakua tehtäisiin, ei kuluja
    python 02_enrich_places.py --limit 20         # testiajo 20 yrityksellä
    python 02_enrich_places.py                    # kaikki, joilla on verkkosivu (max 250 uutta hakua)
    python 02_enrich_places.py --all --max-queries 800   # myös ilman verkkosivua
    python 02_enrich_places.py --chains           # ketjujen (Talenom, Accountor, Azets, Administer) Helsingin toimistot
    python 02_enrich_places.py --chains BDO Crowe --city Helsinki   # omat ketjut

Tarvitsee tiedoston .env:   GOOGLE_PLACES_KEY=...
Syöte:  data/prh_pohja.csv  (01_fetch_prh.py)
Tulos:  data/prh_rikastettu.csv   varmat ja todennäköiset osumat, tuotavissa suoraan soittolista-sovellukseen
        data/prh_tarkista.csv     epävarmat, ei löytyneet ja ilman numeroa olevat, syy sarakkeessa

KULUT: puhelinnumero ja verkkosivu kuuluvat Googlen kalliimpaan hintaluokkaan, ja jokainen haku
maksaa, vaikka mitään ei löytyisi. Tarkista hinnat ja ilmaiskiintiö osoitteesta
https://cloud.google.com/maps-platform/pricing ennen isoa ajoa. --max-queries (oletus 250) on
turvaraja: se katkaisee ajon, vaikka listassa olisi enemmän. Tulokset tallentuvat välimuistiin
(data/places_cache.json), joten uudelleenajo ja hakulogiikan säätö eivät maksa mitään.
"""
import argparse
import csv
import difflib
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv ei pakollinen, jos avain on ympäristömuuttujassa
    pass

URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join("places." + f for f in [
    "id", "displayName", "formattedAddress", "nationalPhoneNumber", "websiteUri",
    "googleMapsUri", "rating", "userRatingCount", "businessStatus",
])

LEGAL_SUFFIXES = {"oy", "oyj", "ab", "ky", "ay", "tmi", "ltd", "ry", "as"}
# Yleissanat, jotka eivät erota yrityksiä toisistaan
GENERIC_WORDS = {
    "tilitoimisto", "kirjanpito", "kirjanpitotoimisto", "tilipalvelu", "tilipalvelut",
    "taloushallinto", "taloushallintopalvelut", "palvelut", "helsinki", "finland", "accounting",
    "tilit", "ja", "and", "co", "company", "group", "konsultointi", "consulting", "services",
}


def norm_tokens(name):
    s = re.sub(r"[^\w\s]", " ", name.lower())
    return [w for w in s.split() if w not in LEGAL_SUFFIXES]


def similarity(a, b):
    ta, tb = norm_tokens(a), norm_tokens(b)
    # Vertaa erottelevia sanoja; jos nimessä ei ole muuta, käytä koko nimeä
    da = [w for w in ta if w not in GENERIC_WORDS] or ta
    db = [w for w in tb if w not in GENERIC_WORDS] or tb
    return difflib.SequenceMatcher(None, " ".join(da), " ".join(db)).ratio()


MOBILE_PREFIX = re.compile(r"^0(4\d|50)")


def is_mobile(phone):
    """Suomalainen matkapuhelinnumero (04x, 050). Pienillä toimistoilla usein yrittäjän oma numero."""
    return bool(MOBILE_PREFIX.match(re.sub(r"\D", "", phone or "")))


def domain_of(url):
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def evaluate(row, place):
    """Palauttaa (laatu, pisteet, selite). Laatu: varma / todennäköinen / epävarma / ei."""
    gname = (place.get("displayName") or {}).get("text", "")
    sim = similarity(row["nimi"], gname)
    d_row, d_pl = domain_of(row.get("verkkosivu", "")), domain_of(place.get("websiteUri", ""))
    same_domain = bool(d_row and d_pl and d_row == d_pl)
    post = row.get("postinumero", "")
    same_post = bool(post and post in (place.get("formattedAddress") or ""))
    score = (2 if same_domain else 0) + sim + (0.15 if same_post else 0)

    if same_domain or sim >= 0.8:
        quality = "varma"
    elif sim >= 0.6 and same_post:
        quality = "todennäköinen"
    elif sim >= 0.5 or (same_post and sim >= 0.4):
        quality = "epävarma"
    else:
        quality = "ei"
    why = f"Googlen nimi '{gname}', nimen samankaltaisuus {sim:.2f}" \
          + (", sama verkkosivu" if same_domain else "") + (", sama postinumero" if same_post else "")
    return quality, score, why


def best_match(row, places):
    best = None
    for p in places or []:
        q, score, why = evaluate(row, p)
        if best is None or score > best[1]:
            best = (q, score, why, p)
    return best  # (laatu, pisteet, selite, place) tai None


class ApiError(Exception):
    pass


def search(session, key, query, retries=4, page_size=3):
    body = {"textQuery": query, "languageCode": "fi", "regionCode": "FI", "pageSize": page_size}
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK,
               "Content-Type": "application/json"}
    last = ""
    for attempt in range(retries):
        r = session.post(URL, json=body, headers=headers, timeout=30)
        if r.status_code == 200:
            return r.json().get("places", [])
        last = f"{r.status_code}: {r.text[:400]}"
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 * (attempt + 1))
            continue
        break
    raise ApiError(last)


def load_cache(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sys.exit(f"Välimuistitiedosto {path} on rikki. Siirrä se pois ja aja uudelleen.")
    return {}


def save_cache(path, cache):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def build_query(row):
    parts = [row["nimi"], row.get("osoite", ""), f"{row.get('postinumero', '')} {row.get('kaupunki', '')}".strip()]
    return ", ".join(p for p in parts if p)


DEFAULT_CHAINS = ["Talenom", "Accountor", "Azets", "Administer"]
# Ketjun ohjelmisto- ym. yksiköt, jotka eivät ole tilitoimistoja
CHAIN_SKIP_WORDS = ["software", "ohjelmisto"]


def run_chains(args):
    """Hakee ketjujen toimistot kaupungista suoraan Googlesta (PRH:ssa ketjulla on vain yksi rivi)."""
    brands = args.chains or DEFAULT_CHAINS
    city = (args.city or "").strip()
    queries = []
    for b in brands:
        queries.append((b, f"{b} tilitoimisto {city}".strip()))
        queries.append((b, f"{b} {city}".strip()))

    cache_path = Path(args.cache)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = load_cache(cache_path)
    todo = [(b, q) for b, q in queries if f"ketju:{q}" not in cache]
    print(f"Ketjut: {', '.join(brands)}. Hakuja yhteensä {len(queries)}, uusia {len(todo)}.")
    if args.dry_run:
        print("Dry run: hakuja ei tehty.")
        return

    todo = todo[:args.max_queries]
    if todo:
        key = os.environ.get("GOOGLE_PLACES_KEY", "").strip()
        if not key:
            sys.exit("GOOGLE_PLACES_KEY puuttuu. Lisää se .env-tiedostoon.")
        session = requests.Session()
        for i, (b, q) in enumerate(todo, 1):
            try:
                places = search(session, key, q, page_size=20)
            except ApiError as e:
                save_cache(cache_path, cache)
                sys.exit(f"Haku keskeytyi: {e}")
            cache[f"ketju:{q}"] = {"query": q, "places": places}
            save_cache(cache_path, cache)
            time.sleep(0.15)

    rows, seen, no_phone = [], set(), 0
    per_brand = Counter()
    for b, q in queries:
        entry = cache.get(f"ketju:{q}")
        if not entry:
            continue
        for p in entry.get("places") or []:
            name = (p.get("displayName") or {}).get("text", "")
            addr = p.get("formattedAddress") or ""
            pid = p.get("id", "")
            if b.lower() not in name.lower() or pid in seen:
                continue
            if any(w in name.lower() for w in CHAIN_SKIP_WORDS):  # esim. "Accountor Software"
                continue
            if city and city.lower() not in addr.lower():
                continue
            if p.get("businessStatus") == "CLOSED_PERMANENTLY":
                continue
            seen.add(pid)
            phone = p.get("nationalPhoneNumber") or ""
            if not phone:
                no_phone += 1
                continue
            street = addr.split(",")[0].strip()
            rows.append({
                "nimi": name,
                "puhelin": phone,
                "verkkosivu": p.get("websiteUri", ""),
                "osoite": street,
                "kaupunki": city.title(),
                "arvostelut": p.get("userRatingCount", ""),
                "arvosana": p.get("rating", ""),
                "google_maps": p.get("googleMapsUri", ""),
                "placeId": pid,
                "muistiinpanot": f"Ketju: {b}. Todennäköisesti keskusnumero: kysy IT-vastaavaa / päättäjää",
            })
            per_brand[b] += 1

    rows.sort(key=lambda r: (r["nimi"].lower(), r["osoite"]))
    out = Path(args.chains_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["nimi", "puhelin", "verkkosivu", "osoite", "kaupunki", "arvostelut", "arvosana",
              "google_maps", "placeId", "muistiinpanot"]
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print("\n=== Yhteenveto (ketjut) ===")
    for b in brands:
        print(f"  {b}: {per_brand[b]} toimistoa")
    print(f"Yhteensä {len(rows)} -> {out}  (ilman numeroa ohitettu: {no_phone})")
    phones = Counter(re.sub(r"\D", "", r["puhelin"]) for r in rows)
    shared = sum(1 for n in phones.values() if n > 1)
    if shared:
        print(f"Huom: {shared} numeroa on usealla toimistolla. Soittosovellus yhdistää samat numerot yhdeksi.")


def main():
    ap = argparse.ArgumentParser(description="Hae puhelinnumerot Google Placesilla")
    ap.add_argument("--chains", nargs="*",
                    help="hae ketjujen toimistot kaupungista, esim. --chains tai --chains Talenom Azets")
    ap.add_argument("--chains-out", default="data/ketjut_rikastettu.csv")
    ap.add_argument("--input", default="data/prh_pohja.csv")
    ap.add_argument("--out", default="data/prh_rikastettu.csv")
    ap.add_argument("--review", default="data/prh_tarkista.csv")
    ap.add_argument("--cache", default="data/places_cache.json")
    ap.add_argument("--all", action="store_true", help="myös yritykset ilman verkkosivua")
    ap.add_argument("--city", default="Helsinki",
                    help="käsittele vain tämän kaupungin osoitteet (oletus Helsinki, tyhjä '' = kaikki)")
    ap.add_argument("--limit", type=int, help="käsittele vain N ensimmäistä riviä")
    ap.add_argument("--max-queries", type=int, default=250, help="enintään N uutta hakua (oletus 250)")
    ap.add_argument("--dry-run", action="store_true", help="älä tee hakuja, näytä vain määrät")
    args = ap.parse_args()

    if args.chains is not None:
        return run_chains(args)

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"Syötetiedostoa {src} ei löydy. Aja ensin 01_fetch_prh.py.")
    with open(src, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    total_in = len(rows)
    if args.city:
        # PRH:n kotipaikkahaku palauttaa myös yrityksiä, joiden osoite on muualla (esim. Tampere, Jämsä)
        want = args.city.strip().lower()
        other = Counter()
        kept = []
        for r in rows:
            city = (r.get("kaupunki") or "").strip()
            if city.lower() == want:
                kept.append(r)
            else:
                other[city.title() or "?"] += 1
        if other:
            top = ", ".join(f"{c} {n}" for c, n in other.most_common(5))
            print(f"Rajattu pois osoitteen kaupungin perusteella: {sum(other.values())} ({top}).")
        rows = kept
    if not args.all:
        rows = [r for r in rows if (r.get("verkkosivu") or "").strip()]
    if args.limit:
        rows = rows[:args.limit]

    cache_path = Path(args.cache)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = load_cache(cache_path)
    todo = [r for r in rows if r["y_tunnus"] not in cache]
    print(f"Syötteessä {total_in} yritystä, käsiteltävänä {len(rows)}"
          f"{'' if args.all else ' (vain verkkosivulliset)'}.")
    print(f"Välimuistissa {len(rows) - len(todo)}, uusia hakuja tarvitaan {len(todo)}.")

    if args.dry_run:
        print("Dry run: hakuja ei tehty.")
        return

    todo_run = todo[:args.max_queries]
    if len(todo) > len(todo_run):
        print(f"Turvaraja: haetaan vain {len(todo_run)} / {len(todo)}. "
              f"Nosta rajaa valitsimella --max-queries, kun olet tarkistanut hinnat.")

    if todo_run:
        key = os.environ.get("GOOGLE_PLACES_KEY", "").strip()
        if not key:
            sys.exit("GOOGLE_PLACES_KEY puuttuu. Lisää se .env-tiedostoon.")
        session = requests.Session()
        for i, row in enumerate(todo_run, 1):
            try:
                places = search(session, key, build_query(row))
            except ApiError as e:
                save_cache(cache_path, cache)
                hint = ""
                if "403" in str(e) or "PERMISSION" in str(e) or "API_KEY" in str(e):
                    hint = ("\nTarkista: Places API (New) on otettu käyttöön, laskutus on päällä ja "
                            "avaimen rajaus sallii Places API (New):n.")
                sys.exit(f"Haku keskeytyi (haettu {i - 1} / {len(todo_run)}): {e}{hint}")
            cache[row["y_tunnus"]] = {"query": build_query(row), "places": places}
            if i % 10 == 0 or i == len(todo_run):
                save_cache(cache_path, cache)
            if i % 25 == 0 or i == len(todo_run):
                print(f"  {i} / {len(todo_run)} haettu")
            time.sleep(0.15)

    # Tulokset välimuistin pohjalta
    ok, review, skipped = [], [], 0
    for row in rows:
        entry = cache.get(row["y_tunnus"])
        if entry is None:
            skipped += 1
            continue
        match = best_match(row, entry.get("places"))
        if match is None or match[0] == "ei":
            review.append({**row, "syy": "Googlesta ei löytynyt vastaavaa yritystä", "google_nimi": "",
                           "puhelin": "", "google_maps": ""})
            continue
        quality, _, why, place = match
        gname = (place.get("displayName") or {}).get("text", "")
        phone = place.get("nationalPhoneNumber") or ""
        closed = place.get("businessStatus") == "CLOSED_PERMANENTLY"
        base = {**row, "google_nimi": gname, "puhelin": phone,
                "google_maps": place.get("googleMapsUri", "")}
        if closed:
            review.append({**base, "syy": "Google: pysyvästi suljettu"})
        elif quality == "epävarma":
            review.append({**base, "syy": "Epävarma osuma. " + why})
        elif not phone:
            review.append({**base, "syy": "Osuma löytyi, mutta Googlessa ei puhelinnumeroa"})
        else:
            ok.append({
                "nimi": row["nimi"],
                "puhelin": phone,
                "verkkosivu": place.get("websiteUri") or row.get("verkkosivu", ""),
                "osoite": row.get("osoite", ""),
                "kaupunki": (row.get("kaupunki") or "").title(),
                "arvostelut": place.get("userRatingCount", ""),
                "arvosana": place.get("rating", ""),
                "google_maps": place.get("googleMapsUri", ""),
                "placeId": place.get("id", ""),
                "muistiinpanot": f"Y-tunnus {row['y_tunnus']}"
                                 + (" · matkapuhelinnumero, usein yksinyrittäjä" if is_mobile(phone) else "")
                                 + ("" if quality == "varma" else " · tarkista, että numero on oikean yrityksen"),
            })

    ok_fields = ["nimi", "puhelin", "verkkosivu", "osoite", "kaupunki", "arvostelut", "arvosana",
                 "google_maps", "placeId", "muistiinpanot"]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=ok_fields)
        w.writeheader()
        w.writerows(ok)
    review_fields = ["syy", "y_tunnus", "nimi", "google_nimi", "puhelin", "verkkosivu", "osoite",
                     "postinumero", "kaupunki", "google_maps"]
    with open(args.review, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=review_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(review)

    print("\n=== Yhteenveto ===")
    print(f"Puhelinnumerolliset osumat: {len(ok)} -> {args.out}")
    print(f"Tarkistettavat: {len(review)} -> {args.review}")
    if skipped:
        print(f"Ei vielä haettu (turvaraja tai --limit): {skipped}")


if __name__ == "__main__":
    main()