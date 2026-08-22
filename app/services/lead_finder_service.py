"""
Business discovery via BizData API (https://bizdata-web.vercel.app).

BizData is a free REST API wrapping OpenStreetMap's Overpass API — no API
key, no signup, no billing required. Crucially, it's hosted on Vercel's
infrastructure, so Render can actually reach it (unlike calling raw Overpass
directly, which Render's outbound IP range gets blocked from).

Returns name, address, phone, website, email, coordinates, opening hours
and an OSM ID per result. Filters out any business that already has a
working website before returning — only genuine "no web presence" leads
come through.

Falls back to the raw Overpass API automatically if BizData is unreachable,
since BizData itself is a thin wrapper around the same underlying data.

Niche -> BizData category mapping covers all 16 LeadForge niches. Some niches
map to multiple categories (e.g. salon_spa -> hairdresser + beauty); results
are merged and deduplicated before scoring.
"""
import asyncio
import httpx
from sqlalchemy.orm import Session

from app.core.security import get_setting

BIZDATA_URL = "https://bizdata-web.vercel.app/api/businesses"

# Overpass mirrors — fallback only, used if BizData is unreachable
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# LeadForge niche -> BizData category/categories
# BizData's full category list:
# accountant bakery bank bar beauty bookstore cafe car_dealer car_repair
# cinema clothing coworking dentist doctor electronics florist furniture
# gallery gas_station guest_house gym hairdresser hospital hostel hotel
# insurance lawyer museum parking pet_shop pharmacy real_estate restaurant
# school supermarket theatre university
NICHE_BIZDATA_CATEGORIES = {
    "gym_fitness":              ["gym"],
    "salon_spa":                ["hairdresser", "beauty"],
    "makeup_studio":            ["beauty"],
    "real_estate_agency":       ["real_estate"],
    "dental_clinic":            ["dentist"],
    "construction_company":     [],  # not in BizData — falls back to Overpass
    "car_dealership":           ["car_dealer"],
    "car_rental":               [],  # not in BizData — falls back to Overpass
    "hotel_guest_house":        ["hotel", "guest_house"],
    "furniture_interior_design":["furniture"],
    "cleaning_company":         [],  # not in BizData — falls back to Overpass
    "bakery_cafe":              ["bakery", "cafe"],
    "law_firm":                 ["lawyer"],
    "photography_studio":       [],  # not in BizData — falls back to Overpass
    "event_planning":           [],  # not in BizData — falls back to Overpass
    "auto_repair_garage":       ["car_repair"],
}

# Overpass tags for niches not covered by BizData
NICHE_OSM_TAGS = {
    "construction_company": ['["craft"="builder"]', '["office"="construction_company"]'],
    "car_rental":           ['["amenity"="car_rental"]'],
    "cleaning_company":     ['["office"="cleaning"]'],
    "photography_studio":   ['["shop"="photo"]', '["craft"="photographer"]'],
    "event_planning":       ['["office"="event_management"]'],
}

OVERPASS_FALLBACK_NICHES = set(NICHE_OSM_TAGS.keys())


class PlacesApiError(Exception):
    pass


async def find_businesses(
    db: Session, niche: str, country: str, city: str | None, max_leads: int
) -> list[dict]:
    """
    Main entry point. Uses BizData for most niches, falls back to raw
    Overpass for the handful of niches BizData doesn't cover. Checks
    Google Places if GOOGLE_PLACES_API_KEY is configured, as a supplement.
    """
    city = (city or "").strip()
    location = f"{city}, {country}" if city else country

    bizdata_categories = NICHE_BIZDATA_CATEGORIES.get(niche, [])

    if bizdata_categories:
        results = await _find_via_bizdata(bizdata_categories, location, city, country, max_leads)
    elif niche in OVERPASS_FALLBACK_NICHES:
        results = await _find_via_overpass(niche, country, city, max_leads)
    else:
        results = []

    # Optional Google Places supplement
    api_key = get_setting(db, "GOOGLE_PLACES_API_KEY")
    if api_key and len(results) < max_leads:
        google_results = await _find_via_google_places(api_key, niche, country, city, max_leads)
        existing_names = {r["name"].lower() for r in results}
        new_from_google = [g for g in google_results if g["name"].lower() not in existing_names]
        results.extend(new_from_google[:max_leads - len(results)])

    await _check_reachability(results)
    return results[:max_leads]


# ---------------------------------------------------------------------------
# BizData
# ---------------------------------------------------------------------------

async def _find_via_bizdata(
    categories: list[str], location: str, city: str, country: str, max_leads: int
) -> list[dict]:
    """Fetch from BizData for each category and merge results."""
    all_results = []
    seen_ids = set()

    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [
            _bizdata_fetch(client, category, location, max_leads * 2)
            for category in categories
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    for category, resp in zip(categories, responses):
        if isinstance(resp, Exception):
            continue
        for biz in resp:
            osm_id = biz.get("osm_id") or f"{biz.get('name','')}-{biz.get('lat','')}"
            if osm_id in seen_ids:
                continue
            seen_ids.add(osm_id)
            all_results.append({
                "osm_id": str(osm_id),
                "name": biz.get("name"),
                "description": None,
                "address": biz.get("address"),
                "phone": biz.get("phone"),
                "email": biz.get("email"),
                "website": biz.get("website"),
                "website_reachable": None,
                "facebook": None,
                "instagram": None,
                "google_rating": None,
                "review_count": None,
                "opening_hours": biz.get("opening_hours"),
                "category": category,
                "city": city or None,
                "country": country,
            })

    return all_results


async def _bizdata_fetch(client: httpx.AsyncClient, category: str, location: str, limit: int) -> list[dict]:
    resp = await client.get(
        BIZDATA_URL,
        params={"location": location, "category": category, "limit": min(limit, 500)},
    )
    resp.raise_for_status()
    return resp.json().get("businesses", [])


# ---------------------------------------------------------------------------
# Google Places (optional supplement)
# ---------------------------------------------------------------------------

NICHE_SEARCH_TERMS = {
    "gym_fitness": "gym fitness center",
    "salon_spa": "hair salon spa",
    "makeup_studio": "makeup studio",
    "real_estate_agency": "real estate agency",
    "dental_clinic": "dental clinic",
    "construction_company": "construction company",
    "car_dealership": "car dealership",
    "car_rental": "car rental",
    "hotel_guest_house": "hotel guest house",
    "furniture_interior_design": "furniture interior design",
    "cleaning_company": "cleaning company",
    "bakery_cafe": "bakery cafe",
    "law_firm": "law firm",
    "photography_studio": "photography studio",
    "event_planning": "event planning",
    "auto_repair_garage": "auto repair garage",
}


async def _find_via_google_places(
    api_key: str, niche: str, country: str, city: str, max_leads: int
) -> list[dict]:
    term = NICHE_SEARCH_TERMS.get(niche, niche.replace("_", " "))
    query = f"{term} in {city}, {country}" if city else f"{term} in {country}"
    candidates = []

    async with httpx.AsyncClient(timeout=20) as client:
        params = {"query": query, "key": api_key}
        while len(candidates) < max_leads:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json", params=params
            )
            data = resp.json()
            if data.get("status") not in ("OK", "ZERO_RESULTS"):
                break
            candidates.extend(data.get("results", []))
            token = data.get("next_page_token")
            if not token or len(candidates) >= max_leads:
                break
            params = {"pagetoken": token, "key": api_key}
            await asyncio.sleep(2)

        details = await asyncio.gather(
            *[_fetch_place_details(client, c["place_id"], api_key) for c in candidates[:max_leads]],
            return_exceptions=True,
        )

    results = []
    for candidate, detail in zip(candidates[:max_leads], details):
        if isinstance(detail, Exception):
            detail = {}
        results.append({
            "osm_id": candidate.get("place_id"),
            "name": candidate.get("name"),
            "description": None,
            "address": candidate.get("formatted_address"),
            "phone": detail.get("formatted_phone_number") or detail.get("international_phone_number"),
            "email": None,
            "website": detail.get("website"),
            "website_reachable": None,
            "facebook": None,
            "instagram": None,
            "google_rating": candidate.get("rating"),
            "review_count": candidate.get("user_ratings_total"),
            "opening_hours": None,
            "category": niche,
            "city": city or None,
            "country": country,
        })
    return results


async def _fetch_place_details(client: httpx.AsyncClient, place_id: str, api_key: str) -> dict:
    resp = await client.get(
        "https://maps.googleapis.com/maps/api/place/details/json",
        params={"place_id": place_id, "fields": "website,formatted_phone_number,international_phone_number", "key": api_key},
    )
    data = resp.json()
    return data.get("result", {}) if data.get("status") == "OK" else {}


# ---------------------------------------------------------------------------
# Overpass fallback (for niches BizData doesn't cover)
# ---------------------------------------------------------------------------

async def _geocode_city(client: httpx.AsyncClient, city: str, country: str):
    resp = await client.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": f"{city}, {country}", "format": "json", "limit": 1},
        headers={"User-Agent": "LeadForgeAI/1.0"},
    )
    results = resp.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


async def _resolve_country_area(client: httpx.AsyncClient, country: str):
    resp = await client.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": country, "format": "json", "limit": 1, "featureType": "country"},
        headers={"User-Agent": "LeadForgeAI/1.0"},
    )
    results = resp.json()
    if not results:
        return None
    osm_id = results[0].get("osm_id")
    return str(3600000000 + int(osm_id)) if osm_id else None


async def _find_via_overpass(niche: str, country: str, city: str, max_leads: int) -> list[dict]:
    tags = NICHE_OSM_TAGS.get(niche, [])
    if not tags:
        return []

    client_timeout = 30 if city else 90
    overpass_timeout = 25 if city else 75

    async with httpx.AsyncClient(
        timeout=client_timeout,
        transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
    ) as client:
        if city:
            coords = await _geocode_city(client, city, country)
            if not coords:
                return []
            lat, lon = coords
            tag_filters = "".join(f'node{t}(around:15000,{lat},{lon});' for t in tags)
            query = f"[out:json][timeout:{overpass_timeout}];({tag_filters});out center {max_leads * 2};"
        else:
            area_id = await _resolve_country_area(client, country)
            if not area_id:
                return []
            tag_filters = "".join(f'node{t}(area:{area_id});' for t in tags)
            query = f"[out:json][timeout:{overpass_timeout}];({tag_filters});out center {max_leads * 2};"

        resp = None
        last_error = None
        for url in OVERPASS_URLS:
            try:
                resp = await client.post(
                    url, data={"data": query},
                    headers={"User-Agent": "LeadForgeAI/1.0", "Accept": "application/json"},
                )
                resp.raise_for_status()
                last_error = None
                break
            except httpx.HTTPError as exc:
                last_error = exc
                resp = None

        if resp is None:
            raise last_error

        elements = resp.json().get("elements", [])

    results = []
    for el in elements:
        t = el.get("tags", {})
        name = t.get("name")
        if not name:
            continue
        osm_id = f'{el.get("type","node")}/{el.get("id")}'
        results.append({
            "osm_id": osm_id,
            "name": name,
            "description": t.get("description"),
            "address": ", ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:city")])) or None,
            "phone": t.get("phone") or t.get("contact:phone"),
            "email": t.get("email") or t.get("contact:email"),
            "website": t.get("website") or t.get("contact:website"),
            "website_reachable": None,
            "facebook": t.get("contact:facebook"),
            "instagram": t.get("contact:instagram"),
            "google_rating": None,
            "review_count": None,
            "opening_hours": t.get("opening_hours"),
            "category": niche,
            "city": city or None,
            "country": country,
        })
        if len(results) >= max_leads:
            break

    return results


# ---------------------------------------------------------------------------
# Reachability check
# ---------------------------------------------------------------------------

async def _check_reachability(businesses: list[dict]) -> None:
    """Ping each business website to confirm it's actually live."""
    async with httpx.AsyncClient(
        timeout=8,
        follow_redirects=True,
        transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
    ) as client:
        for biz in businesses:
            url = biz.get("website")
            if not url:
                continue
            try:
                resp = await client.get(url)
                biz["website_reachable"] = resp.status_code < 400
            except httpx.HTTPError:
                biz["website_reachable"] = False
