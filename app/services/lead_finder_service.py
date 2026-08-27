"""
Business discovery via BizData API, Overpass (OSM), and optional Google Places.

Priority:
  1. BizData for niches with category mapping (no key required)
  2. Overpass for niches that only exist as OSM tags (multiple mirrors)
  3. Google Places when GOOGLE_PLACES_API_KEY is set — primary fallback if
     Overpass/BizData fail or return too few results
"""
import asyncio
import httpx
from sqlalchemy.orm import Session

from app.core.security import get_setting

BIZDATA_URL = "https://bizdata-web.vercel.app/api/businesses"

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

NICHE_BIZDATA_CATEGORIES = {
    "gym_fitness":               ["gym"],
    "salon_spa":                 ["hairdresser", "beauty"],
    "makeup_studio":             ["beauty"],
    "real_estate_agency":        ["real_estate"],
    "dental_clinic":             ["dentist"],
    "construction_company":      [],
    "car_dealership":            ["car_dealer"],
    "car_rental":                [],
    "hotel_guest_house":         ["hotel", "guest_house"],
    "furniture_interior_design": ["furniture"],
    "cleaning_company":          [],
    "bakery_cafe":               ["bakery", "cafe"],
    "law_firm":                  ["lawyer"],
    "photography_studio":        [],
    "event_planning":            [],
    "auto_repair_garage":        ["car_repair"],
}

NICHE_OSM_TAGS = {
    "construction_company": ['["craft"="builder"]', '["office"="construction_company"]'],
    "car_rental":           ['["amenity"="car_rental"]'],
    "cleaning_company":     ['["office"="cleaning"]'],
    "photography_studio":   ['["shop"="photo"]', '["craft"="photographer"]'],
    "event_planning":       ['["office"="event_management"]'],
}

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


class PlacesApiError(Exception):
    pass


async def find_businesses(
    db: Session, niche: str, country: str, city: str | None, max_leads: int
) -> list[dict]:
    city = (city or "").strip()
    location = f"{city}, {country}" if city else country

    bizdata_categories = NICHE_BIZDATA_CATEGORIES.get(niche, [])
    results: list[dict] = []
    overpass_error: Exception | None = None

    if bizdata_categories:
        try:
            results = await _find_via_bizdata(bizdata_categories, location, city, country, max_leads)
        except Exception:
            results = []
    elif niche in NICHE_OSM_TAGS:
        try:
            results = await _find_via_overpass(niche, country, city, max_leads)
        except Exception as exc:
            overpass_error = exc
            results = []

    api_key = get_setting(db, "GOOGLE_PLACES_API_KEY")
    if api_key and len(results) < max_leads:
        try:
            google_results = await _find_via_google_places(api_key, niche, country, city, max_leads)
            existing_names = {r["name"].lower() for r in results if r.get("name")}
            new_from_google = [g for g in google_results if (g.get("name") or "").lower() not in existing_names]
            results.extend(new_from_google[: max_leads - len(results)])
        except Exception:
            pass

    # Only hard-fail if we have nothing and Overpass was the primary path with no Places key
    if not results and overpass_error and not api_key:
        raise overpass_error

    if not results and overpass_error and api_key:
        # Places was tried but also returned nothing — surface a clearer message
        raise PlacesApiError(
            f"Overpass mirrors are down and Google Places returned no results for this query. "
            f"Try a specific city, or retry in a few minutes. Overpass error: {overpass_error}"
        )

    await _check_reachability(results)
    return results[:max_leads]


async def _find_via_bizdata(
    categories: list[str], location: str, city: str, country: str, max_leads: int
) -> list[dict]:
    all_results = []
    seen_ids = set()

    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [
            _bizdata_fetch(client, category, location, max_leads * 3)
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


async def _bizdata_fetch(
    client: httpx.AsyncClient, category: str, location: str, limit: int
) -> list[dict]:
    resp = await client.get(
        BIZDATA_URL,
        params={
            "location": location,
            "category": category,
            "limit": min(limit, 500),
            "radius_km": 25,
        },
    )
    resp.raise_for_status()
    return resp.json().get("businesses", [])


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


async def _fetch_place_details(
    client: httpx.AsyncClient, place_id: str, api_key: str
) -> dict:
    resp = await client.get(
        "https://maps.googleapis.com/maps/api/place/details/json",
        params={
            "place_id": place_id,
            "fields": "website,formatted_phone_number,international_phone_number",
            "key": api_key,
        },
    )
    data = resp.json()
    return data.get("result", {}) if data.get("status") == "OK" else {}


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


async def _find_via_overpass(
    niche: str, country: str, city: str, max_leads: int
) -> list[dict]:
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
            tag_filters = "".join(f'node{t}(around:25000,{lat},{lon});' for t in tags)
            query = f"[out:json][timeout:{overpass_timeout}];({tag_filters});out center {max_leads * 3};"
        else:
            area_id = await _resolve_country_area(client, country)
            if not area_id:
                return []
            tag_filters = "".join(f'node{t}(area:{area_id});' for t in tags)
            query = f"[out:json][timeout:{overpass_timeout}];({tag_filters});out center {max_leads * 3};"

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
            raise last_error or RuntimeError("All Overpass mirrors failed")

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
            "address": ", ".join(filter(None, [
                t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:city")
            ])) or None,
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


async def _check_reachability(businesses: list[dict]) -> None:
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
