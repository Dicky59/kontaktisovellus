import csv, time, requests

BASE = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies"
# Tarkista kirjanpitoalan koodit TOL 2008 -luettelosta (todennäköisesti 69201-69203)
CODES = ["69201", "69202", "69203"]

def fetch(code):
    page = 1
    while True:
        r = requests.get(BASE, params={
            "location": "Helsinki",
            "mainBusinessLine": code,
            "page": page,
        }, timeout=30)
        r.raise_for_status()
        data = r.json()
        companies = data.get("companies", [])
        if page == 1:
            print(code, "esimerkkivastaus:", companies[:1])  # tarkista rakenne
        if not companies:
            break
        yield from companies
        page += 1
        time.sleep(0.5)

rows = {}
for code in CODES:
    for c in fetch(code):
        bid = (c.get("businessId") or {}).get("value") or c.get("businessId")
        name = (c.get("names") or [{}])[0].get("name", "")
        # Suodata päättyneet pois, jos kenttä löytyy
        if c.get("endDate"):
            continue
        rows[bid] = {"y_tunnus": bid, "nimi": name}

with open("tilitoimistot_pohja.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=["y_tunnus", "nimi"])
    w.writeheader()
    w.writerows(rows.values())
print(len(rows), "yritystä")