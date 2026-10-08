"""
Siivoaa Apifyn Google Maps -scraperin CSV:n ja muodostaa soittolistan.

Käyttö (projektin juuresta):
    python 03_clean_leads.py data/apify_export.csv
    python 03_clean_leads.py data/apify_export.csv data/soittolista.csv

Tekee:
  - poistaa pysyvästi suljetut yritykset
  - poistaa duplikaatit (placeId, puhelin, verkkosivun domain)
  - merkitsee ketjut (muokkaa CHAIN_KEYWORDS-listaa)
  - antaa jokaiselle prioriteetin A/B/C verkkosivun ja arvostelumäärän perusteella
  - lisää tyhjät sarakkeet käsin täytettäväksi (koko_arvio, tulos, muistiinpanot)
"""
import csv
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# Ketjut ja isot toimijat: paikallistoimiston päättäjä ei yleensä ole sama kuin ostaja.
# Muokkaa vapaasti.
CHAIN_KEYWORDS = [
    "talenom", "accountor", "azets", "administer", "visma", "kpmg", "pwc",
    "ernst", "deloitte", "bdo", "grant thornton", "crowe", "baker tilly",
    "tilitoimisto aaltio", "ifirma",
]

# Verkkosivuksi ei lasketa somesivuja
SOCIAL_DOMAINS = ("facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com")

# Kenttien mahdolliset nimet Apifyn tulosteessa
FIELD_ALIASES = {
    "name": ["title", "name", "placeName"],
    "phone": ["phone", "phoneUnformatted"],
    "website": ["website", "web"],
    "reviews": ["reviewsCount", "reviews"],
    "score": ["totalScore", "rating"],
    "street": ["street"],
    "address": ["address"],
    "city": ["city"],
    "category": ["categoryName", "category"],
    "place_id": ["placeId", "place_id"],
    "closed": ["permanentlyClosed"],
    "url": ["url", "googleMapsUrl"],
}


def pick(row, key):
    """Hae kentän arvo aliaslistan mukaan; tyhjät ja 'undefined'/'null' -> ''."""
    for col in FIELD_ALIASES[key]:
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


def norm_phone(s):
    """Normalisoi puhelinnumero vertailua varten (+358 / 0 -> pelkät numerot)."""
    digits = re.sub(r"\D", "", s)
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


def priority(has_site, reviews, chain, has_phone):
    """A = soita ensin, B = sitten, C = viimeisenä / harkitse."""
    if not has_phone:
        return "C"
    if chain:
        return "C"
    if has_site and reviews >= 10:
        return "A"
    if has_site:
        return "B"
    return "C"


def main():
    if len(sys.argv) < 2:
        sys.exit("Käyttö: python 03_clean_leads.py <syöte.csv> [tuloste.csv]")

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name("soittolista.csv")

    with open(src, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(f"Luettu {len(rows)} riviä tiedostosta {src}")

    seen_ids, seen_phones, seen_domains = set(), set(), set()
    out = []
    stats = {"suljettu": 0, "duplikaatti": 0}

    for r in rows:
        if pick(r, "closed").lower() in ("true", "1", "yes"):
            stats["suljettu"] += 1
            continue

        name = pick(r, "name")
        phone = pick(r, "phone")
        site = pick(r, "website")
        domain = domain_of(site)
        real_site = is_real_website(domain)
        pid = pick(r, "place_id")
        pnorm = norm_phone(phone)

        # Duplikaattitarkistus: sama paikka, sama numero tai sama verkkosivu
        if (pid and pid in seen_ids) or (pnorm and pnorm in seen_phones) \
                or (real_site and domain in seen_domains):
            stats["duplikaatti"] += 1
            continue
        if pid:
            seen_ids.add(pid)
        if pnorm:
            seen_phones.add(pnorm)
        if real_site:
            seen_domains.add(domain)

        reviews = to_int(pick(r, "reviews"))
        chain = is_chain(name, domain)
        prio = priority(real_site, reviews, chain, bool(phone))

        street = pick(r, "street") or pick(r, "address")
        out.append({
            "prioriteetti": prio,
            "nimi": name,
            "puhelin": phone,
            "verkkosivu": site,
            "osoite": street,
            "kaupunki": pick(r, "city"),
            "arvostelut": reviews,
            "arvosana": pick(r, "score"),
            "ketju": "kyllä" if chain else "",
            "kategoria": pick(r, "category"),
            "google_maps": pick(r, "url"),
            # Täytetään käsin:
            "koko_arvio": "",      # 1-2 / 3-9 / 10+
            "soitettu_pvm": "",
            "tulos": "",
            "muistiinpanot": "",
        })

    # Järjestys: prioriteetti A->C, sitten arvostelujen määrä laskevasti
    out.sort(key=lambda x: (x["prioriteetti"], -x["arvostelut"], x["nimi"].lower()))

    fields = list(out[0].keys()) if out else []
    with open(dst, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)

    counts = {p: sum(1 for x in out if x["prioriteetti"] == p) for p in "ABC"}
    print(f"Poistettu: {stats['suljettu']} suljettua, {stats['duplikaatti']} duplikaattia")
    print(f"Tallennettu {len(out)} riviä -> {dst}")
    print(f"Prioriteetit: A={counts['A']}, B={counts['B']}, C={counts['C']}")
    print(f"Ilman puhelinta: {sum(1 for x in out if not x['puhelin'])}")


if __name__ == "__main__":
    main()
