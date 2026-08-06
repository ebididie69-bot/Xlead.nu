"""
Business discovery.

Primary source: OpenStreetMap Overpass API (free, no key required) — same
approach as the earlier Termux lead-gen tool, mapping niche -> OSM tags.
Optional enrichment: Google Places API (if a key is present in Settings) for
ratings/reviews/hours, since Overpass rarely has those.

This module returns plain dicts; app/routers/leads.py handles scoring,
disqualification and persistence, so this stays swappable (e.g. add Yelp,
Bing Places) without touching scoring logic.
"""
import httpx
from sqlalchemy.orm import Session

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]

# Niche -> OSM tag queries. Extend this map as more niches are added.
NICHE_OSM_TAGS = {
    "gym_fitness": ['["leisure"="fitness_centre"]', '["sport"="fitness"]'],
    "salon_spa": ['["shop"="hairdresser"]', '["shop"="beauty"]', '["leisure"="spa"]'],
    "makeup_studio": ['["shop"="cosmetics"]', '["beauty"="makeup"]'],
    "real_estate_agency": ['["office"="estate_agent"]'],
    "dental_clinic": ['["healthcare"="dentist"]', '["amenity"="dentist"]'],
    "construction_company": ['["office"="construction_company"]', '["craft"="builder"]'],
    "car_dealership": ['["shop"="car"]'],
    "car_rental": ['["amenity"="car_rental"]'],
    "hotel_guest_house": ['["tourism"="hotel"]', '["tourism"="guest_house"]'],
    "furniture_interior_design": ['["shop"="furniture"]', '["office"="interior_design"]'],
    "cleaning_company": ['["office"="cleaning"]', '["service"="cleaning"]'],
    "bakery_cafe": ['["shop"="bakery"]', '["amenity"="cafe"]'],
    "law_firm": ['["office"="lawyer"]'],
    "photography_studio": ['["shop"="photo"]', '["craft"="photographer"]'],
    "event_planning": ['["office"="event_management"]', '["shop"="event_planning"]'],
    "auto_repair_garage": ['["shop"="car_repair"]', '["craft"="car_repair"]'],
}


async def _geocode_city(client: httpx.AsyncClient, city: str, country: str) -> tuple[float, float] | None:
    """Free geocoding via Nominatim to get a bounding area for Overpass."""
    resp = await client.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": f"{city}, {country}", "format": "json", "limit": 1},
        headers={"User-Agent": "LeadForgeAI/1.0"},
    )
    results = resp.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])


async def _resolve_country_area(client: httpx.AsyncClient, country: str) -> str | None:
    """
    Resolve a country name to an Overpass area reference, used for
    country-wide searches when no city is given. Returns the Overpass
    QL area-id expression (as a ready-to-use 'area(id)' string) or None
    if the country can't be resolved.
    """
    resp = await client.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": country, "format": "json", "limit": 1, "featureType": "country"},
        headers={"User-Agent": "LeadForgeAI/1.0"},
    )
    results = resp.json()
    if not results:
        return None
    osm_id = results[0].get("osm_id")
    if not osm_id:
        return None
    # Overpass area IDs for relations are offset by 3600000000
    area_id = 3600000000 + int(osm_id)
    return str(area_id)


async def find_businesses(niche: str, country: str, city: str | None, max_leads: int) -> list[dict]:
    tags = NICHE_OSM_TAGS.get(niche, [])
    if not tags:
        return []

    city = (city or "").strip()

    # Country-wide queries hit a much bigger dataset and Overpass's public
    # instance is slower for these, so give them more headroom than a
    # single-city radius search.
    client_timeout = 30 if city else 90
    overpass_timeout = 25 if city else 75

    async with httpx.AsyncClient(timeout=client_timeout, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
        if city:
            coords = await _geocode_city(client, city, country)
            if not coords:
                return []
            lat, lon = coords
            radius_m = 15000  # 15km around the city center
            tag_filters = "".join(f'node{tag}(around:{radius_m},{lat},{lon});' for tag in tags)
            query = f"[out:json][timeout:{overpass_timeout}];({tag_filters});out center {max_leads * 2};"
        else:
            area_id = await _resolve_country_area(client, country)
            if not area_id:
                return []
            tag_filters = "".join(f'node{tag}(area:{area_id});' for tag in tags)
            query = f"[out:json][timeout:{overpass_timeout}];({tag_filters});out center {max_leads * 2};"

        # Overpass's main server has occasional outages/connectivity blips,
        # so try each known mirror in turn before giving up.
        resp = None
        last_error = None
        for url in OVERPASS_URLS:
            try:
                resp = await client.post(
                    url,
                    data={"data": query},
                    headers={"User-Agent": "LeadForgeAI/1.0", "Accept": "application/json"},
                )
                resp.raise_for_status()
                last_error = None
                break
            except httpx.HTTPError as exc:
                last_error = exc
                resp = None
                continue
        if resp is None:
            raise last_error
        elements = resp.json().get("elements", [])

    results = []
    for el in elements[: max_leads * 2]:
        tags_data = el.get("tags", {})
        name = tags_data.get("name")
        if not name:
            continue  # skip unnamed nodes, not useful leads

        website = tags_data.get("website") or tags_data.get("contact:website")
        facebook = tags_data.get("contact:facebook")
        instagram = tags_data.get("contact:instagram")

        results.append({
            "osm_id": f'{el.get("type", "node")}/{el.get("id")}',  # stable identity for dedup across re-searches
            "name": name,
            "description": tags_data.get("description"),
            "address": ", ".join(filter(None, [
                tags_data.get("addr:housenumber"), tags_data.get("addr:street"),
                tags_data.get("addr:city"),
            ])) or None,
            "phone": tags_data.get("phone") or tags_data.get("contact:phone"),
            "email": tags_data.get("email") or tags_data.get("contact:email"),
            "website": website,
            "website_reachable": None,  # checked in a follow-up pass, see check_reachability()
            "facebook": facebook,
            "instagram": instagram,
            "google_rating": None,      # filled by enrich_with_google_places() if key configured
            "review_count": None,
            "opening_hours": tags_data.get("opening_hours"),
            "category": tags_data.get("shop") or tags_data.get("office") or tags_data.get("amenity") or niche,
        })
        if len(results) >= max_leads:
            break

    await _check_reachability(results)
    return results


async def _check_reachability(businesses: list[dict]) -> None:
    """Mutates each business dict's website_reachable: True/False/None."""
    async with httpx.AsyncClient(timeout=8, follow_redirects=True, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
        for biz in businesses:
            url = biz.get("website")
            if not url:
                continue
            try:
                resp = await client.get(url)
                biz["website_reachable"] = resp.status_code < 400
            except httpx.HTTPError:
                biz["website_reachable"] = False


async def enrich_with_google_places(db: Session, businesses: list[dict], city: str, country: str) -> None:
    """
    Optional: adds google_rating/review_count/opening_hours via Places API,
    only if GOOGLE_OAUTH_CLIENT_ID-adjacent Places key is configured. Kept
    separate so the free Overpass path always works with zero API keys.
    """
    from app.core.security import get_setting
    api_key = get_setting(db, "GOOGLE_PLACES_API_KEY")
    if not api_key:
        return  # silently skip enrichment; core lead data still usable

    async with httpx.AsyncClient(timeout=15, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")) as client:
        for biz in businesses:
            params = {
                "query": f"{biz['name']} {city} {country}",
                "key": api_key,
            }
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json", params=params
            )
            candidates = resp.json().get("results", [])
            if candidates:
                top = candidates[0]
                biz["google_rating"] = top.get("rating")
                biz["review_count"] = top.get("user_ratings_total")
