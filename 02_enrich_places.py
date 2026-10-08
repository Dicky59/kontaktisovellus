import os, requests

API_KEY = os.environ["GOOGLE_PLACES_KEY"]

def lookup(name):
    r = requests.post(
        "https://places.googleapis.com/v1/places:searchText",
        headers={
            "X-Goog-Api-Key": API_KEY,
            "X-Goog-FieldMask": "places.displayName,places.nationalPhoneNumber,"
                                "places.websiteUri,places.formattedAddress",
        },
        json={"textQuery": f"{name} Helsinki", "languageCode": "fi"},
        timeout=30,
    )
    r.raise_for_status()
    places = r.json().get("places", [])
    return places[0] if places else None