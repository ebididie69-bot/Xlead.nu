"""
Image sourcing for generated demo sites.

Priority:
  0. Brand assets from lead website / social
  1. Real Google Places photos
  2. CURATED NICHE PACK (stable Unsplash CDN URLs for premium demo look)
  3. Live Unsplash search (UNSPLASH_ACCESS_KEY)
  4. AI (Stability → Gemini Imagen → Grok image)
  5. Picsum seed fallback — never empty heroes
"""
import base64
import os
import httpx
from sqlalchemy.orm import Session

from app.core.security import get_setting
from app.services import brand_asset_service

UNSPLASH_ENDPOINT = "https://api.unsplash.com/search/photos"


def _u(photo_id: str, w: int = 1600) -> str:
    return f"https://images.unsplash.com/{photo_id}?auto=format&fit=crop&w={w}&q=80"


NICHE_IMAGE_PACKS: dict[str, dict[str, dict]] = {
    "cleaning_company": {
        "hero": {"url": _u("photo-1581578731548-c64695cc6952"), "source": "stock", "alt": "Professional cleaner"},
        "about": {"url": _u("photo-1628177142898-93e36e4e3a71"), "source": "stock", "alt": "Clean modern home"},
        "gallery_1": {"url": _u("photo-1563453392212-326f5e854473"), "source": "stock", "alt": "Window cleaning"},
        "gallery_2": {"url": _u("photo-1585421514738-01731321671b"), "source": "stock", "alt": "Cleaning supplies"},
        "gallery_3": {"url": _u("photo-1527515637462-cff94eecc1ac"), "source": "stock", "alt": "Team at work"},
        "gallery_4": {"url": _u("photo-1600585152220-90363fe7e115"), "source": "stock", "alt": "Sparkling interior"},
    },
    "dental_clinic": {
        "hero": {"url": _u("photo-1606811841689-23dfddce3e95"), "source": "stock", "alt": "Dental care smile"},
        "about": {"url": _u("photo-1629909613654-28e377c37b09"), "source": "stock", "alt": "Modern dental clinic"},
        "gallery_1": {"url": _u("photo-1598256989800-fe5f95da9787"), "source": "stock", "alt": "Dental treatment"},
        "gallery_2": {"url": _u("photo-1609840114035-3c981b782dfe"), "source": "stock", "alt": "Dentist office"},
        "gallery_3": {"url": _u("photo-1588776814546-1ffcf47267a5"), "source": "stock", "alt": "Dental equipment"},
        "gallery_4": {"url": _u("photo-1579684385127-1ef15d508118"), "source": "stock", "alt": "Patient care"},
    },
    "furniture_interior_design": {
        "hero": {"url": _u("photo-1618221195710-dd6b41faaea6"), "source": "stock", "alt": "Elegant living room"},
        "about": {"url": _u("photo-1616486338812-3dadae4b4ace"), "source": "stock", "alt": "Interior design space"},
        "gallery_1": {"url": _u("photo-1555041469-a586c61ea9bc"), "source": "stock", "alt": "Modern sofa"},
        "gallery_2": {"url": _u("photo-1538688525198-9b88f6f83324"), "source": "stock", "alt": "Dining set"},
        "gallery_3": {"url": _u("photo-1506439773649-6e0eb8cfb237"), "source": "stock", "alt": "Accent chair"},
        "gallery_4": {"url": _u("photo-1595428774223-ef52624120d2"), "source": "stock", "alt": "Storage furniture"},
    },
    "salon_spa": {
        "hero": {"url": _u("photo-1503951914875-452162b0f3f1"), "source": "stock", "alt": "Barber shop cut"},
        "about": {"url": _u("photo-1585747860715-2ba37e789b2b"), "source": "stock", "alt": "Salon interior"},
        "gallery_1": {"url": _u("photo-1622286342621-4bd786c2447c"), "source": "stock", "alt": "Hair styling"},
        "gallery_2": {"url": _u("photo-1599351431202-1e0f0137899a"), "source": "stock", "alt": "Beard trim"},
        "gallery_3": {"url": _u("photo-1560066984-138dadb4c035"), "source": "stock", "alt": "Spa treatment"},
        "gallery_4": {"url": _u("photo-1522337360788-8b13dee7a37e"), "source": "stock", "alt": "Beauty services"},
    },
    "gym_fitness": {
        "hero": {"url": _u("photo-1534438327276-14e5300c3a48"), "source": "stock", "alt": "Gym training"},
        "about": {"url": _u("photo-1517836357463-d25dfeac3438"), "source": "stock", "alt": "Weight training"},
        "gallery_1": {"url": _u("photo-1571902943202-507ec2618e8f"), "source": "stock", "alt": "Fitness class"},
        "gallery_2": {"url": _u("photo-1540497077202-7c8a3999166f"), "source": "stock", "alt": "Gym equipment"},
        "gallery_3": {"url": _u("photo-1581009146145-b5ef439e1539"), "source": "stock", "alt": "Strength training"},
        "gallery_4": {"url": _u("photo-1576678927484-cc907957088c"), "source": "stock", "alt": "Cardio workout"},
    },
    "construction_company": {
        "hero": {"url": _u("photo-1503387762-592deb58ef4e"), "source": "stock", "alt": "Modern architecture build"},
        "about": {"url": _u("photo-1541888946425-d81bb19240f5"), "source": "stock", "alt": "Construction site"},
        "gallery_1": {"url": _u("photo-1486406146926-c627a92ad1ab"), "source": "stock", "alt": "Commercial building"},
        "gallery_2": {"url": _u("photo-1504307651254-35680f356dfd"), "source": "stock", "alt": "Building under construction"},
        "gallery_3": {"url": _u("photo-1590274853856-f22d5ee3dc76"), "source": "stock", "alt": "Luxury residence exterior"},
        "gallery_4": {"url": _u("photo-1600585154340-be6161a56a0c"), "source": "stock", "alt": "Finished modern home"},
    },
    "bakery_cafe": {
        "hero": {"url": _u("photo-1495474472287-4d71bcdd2085"), "source": "stock", "alt": "Latte art coffee"},
        "about": {"url": _u("photo-1509042239860-f550ce710b93"), "source": "stock", "alt": "Fresh coffee pour"},
        "gallery_1": {"url": _u("photo-1555507036-ab1f4038808a"), "source": "stock", "alt": "Artisan bakery"},
        "gallery_2": {"url": _u("photo-1511920170033-f8396924c348"), "source": "stock", "alt": "Cafe interior"},
        "gallery_3": {"url": _u("photo-1554118811-1e0d58224f24"), "source": "stock", "alt": "Coffee and pastry"},
        "gallery_4": {"url": _u("photo-1445116572660-236099ec97a0"), "source": "stock", "alt": "Cozy cafe seating"},
    },
    "hotel_guest_house": {
        "hero": {"url": _u("photo-1618773928121-c32242e63f39"), "source": "stock", "alt": "Luxury hotel bedroom"},
        "about": {"url": _u("photo-1566073771259-6a8506099945"), "source": "stock", "alt": "Hotel lobby"},
        "gallery_1": {"url": _u("photo-1582719478250-c89cae4dc85b"), "source": "stock", "alt": "Hotel suite"},
        "gallery_2": {"url": _u("photo-1571896349842-33c89424de2d"), "source": "stock", "alt": "Resort pool"},
        "gallery_3": {"url": _u("photo-1520250497591-112f2f40a3f4"), "source": "stock", "alt": "Boutique hotel room"},
        "gallery_4": {"url": _u("photo-1445019980597-93fa8acb246c"), "source": "stock", "alt": "Hotel exterior night"},
    },
    "auto_repair_garage": {
        "hero": {"url": _u("photo-1486262715619-577d19e02654"), "source": "stock", "alt": "Auto mechanic at work"},
        "about": {"url": _u("photo-1619642751034-765dfdf7c58e"), "source": "stock", "alt": "Car service garage"},
        "gallery_1": {"url": _u("photo-1492144534655-ae79c964c9d7"), "source": "stock", "alt": "Premium vehicle"},
        "gallery_2": {"url": _u("photo-1632829882891-5047ccc374e1"), "source": "stock", "alt": "Engine diagnostics"},
        "gallery_3": {"url": _u("photo-1625047509248-ec889cbff17f"), "source": "stock", "alt": "Car detailing"},
        "gallery_4": {"url": _u("photo-1503376780353-7e6692767b70"), "source": "stock", "alt": "Luxury car exterior"},
    },
    "car_rental": {
        "hero": {"url": _u("photo-1494976388531-d1058494cdd8"), "source": "stock", "alt": "Premium rental car"},
        "about": {"url": _u("photo-1502877338535-766e1452684b"), "source": "stock", "alt": "Sports car"},
        "gallery_1": {"url": _u("photo-1552519507-da3b142c6e3d"), "source": "stock", "alt": "Red sports coupe"},
        "gallery_2": {"url": _u("photo-1549317661-bd32c8ce0db2"), "source": "stock", "alt": "SUV on road"},
        "gallery_3": {"url": _u("photo-1519641471654-76ce0107ad1b"), "source": "stock", "alt": "Luxury sedan"},
        "gallery_4": {"url": _u("photo-1563720223185-11003d516935"), "source": "stock", "alt": "Black luxury car"},
    },
    "event_planning": {
        "hero": {"url": _u("photo-1519167758481-83f550bb49b3"), "source": "stock", "alt": "Elegant event ballroom"},
        "about": {"url": _u("photo-1464366400600-7168b8af9bc3"), "source": "stock", "alt": "Wedding reception"},
        "gallery_1": {"url": _u("photo-1478144592103-25e304f24786"), "source": "stock", "alt": "Table setting"},
        "gallery_2": {"url": _u("photo-1511795409834-ef04bbd61622"), "source": "stock", "alt": "Celebration dinner"},
        "gallery_3": {"url": _u("photo-1505236858219-8359eb29e329"), "source": "stock", "alt": "Event decor lights"},
        "gallery_4": {"url": _u("photo-1530103862676-de8c9debad1d"), "source": "stock", "alt": "Party balloons"},
    },
    "makeup_studio": {
        "hero": {"url": _u("photo-1522337360788-8b13dee7a37e"), "source": "stock", "alt": "Beauty makeup artist"},
        "about": {"url": _u("photo-1487412947147-5cebf100ffc2"), "source": "stock", "alt": "Makeup brushes"},
        "gallery_1": {"url": _u("photo-1512496015851-a90fb38ba796"), "source": "stock", "alt": "Glamour makeup"},
        "gallery_2": {"url": _u("photo-1596462502278-27bfdc403348"), "source": "stock", "alt": "Cosmetic products"},
        "gallery_3": {"url": _u("photo-1560066984-138dadb4c035"), "source": "stock", "alt": "Salon vanity"},
        "gallery_4": {"url": _u("photo-1522335789203-aabd1fc54bc9"), "source": "stock", "alt": "Hair and beauty"},
    },
}

NICHE_IMAGE_KEYWORDS = {
    "gym_fitness": ["modern gym interior", "fitness training", "gym equipment"],
    "salon_spa": ["hair salon interior", "spa treatment room", "beauty salon"],
    "makeup_studio": ["makeup studio", "beauty vanity", "cosmetics flatlay"],
    "real_estate_agency": ["modern living room", "real estate office", "house exterior"],
    "dental_clinic": ["dental clinic interior", "dentist office", "dental equipment"],
    "construction_company": ["construction site", "building construction", "architecture blueprint"],
    "car_dealership": ["car showroom", "new cars dealership", "luxury car lot"],
    "car_rental": ["rental car fleet", "car keys handover", "airport car rental"],
    "hotel_guest_house": ["boutique hotel room", "hotel lobby", "guest house courtyard"],
    "furniture_interior_design": ["modern furniture showroom", "interior design living room", "custom furniture workshop"],
    "cleaning_company": ["professional cleaning service", "clean modern office", "cleaning supplies"],
    "bakery_cafe": ["artisan bakery interior", "fresh pastries display", "cozy cafe counter"],
    "law_firm": ["law office interior", "modern legal office", "law books shelf"],
    "photography_studio": ["photography studio setup", "camera equipment", "portrait lighting studio"],
    "event_planning": ["elegant event setup", "wedding reception decor", "catering table display"],
    "auto_repair_garage": ["auto repair garage", "mechanic workshop", "car service bay"],
}

SECTION_SLOTS = ["hero", "about", "gallery_1", "gallery_2", "gallery_3", "gallery_4"]


def _niche_pack(niche: str) -> list[dict]:
    pack = NICHE_IMAGE_PACKS.get(niche) or {}
    return [pack[s] for s in SECTION_SLOTS if s in pack]


async def _fetch_places_photos(db: Session, place_id: str, max_photos: int) -> list[dict]:
    api_key = get_setting(db, "GOOGLE_PLACES_API_KEY")
    if not api_key or not place_id:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            detail_resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={"place_id": place_id, "fields": "photos", "key": api_key},
            )
            if detail_resp.status_code != 200:
                return []
            photos = detail_resp.json().get("result", {}).get("photos", [])[:max_photos]
            results = []
            for p in photos:
                photo_resp = await client.get(
                    "https://maps.googleapis.com/maps/api/place/photo",
                    params={"photoreference": p["photo_reference"], "maxwidth": 1200, "key": api_key},
                    follow_redirects=True,
                )
                if photo_resp.status_code == 200 and photo_resp.url:
                    results.append({"url": str(photo_resp.url), "source": "real", "alt": "Business photo"})
            return results
    except Exception:
        return []


async def _fetch_unsplash_stock(db: Session, niche: str, count: int) -> list[dict]:
    access_key = get_setting(db, "UNSPLASH_ACCESS_KEY") or os.getenv("UNSPLASH_ACCESS_KEY")
    if not access_key:
        return []
    keywords = NICHE_IMAGE_KEYWORDS.get(niche, ["professional business"])
    results = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for kw in keywords:
                if len(results) >= count:
                    break
                resp = await client.get(
                    UNSPLASH_ENDPOINT,
                    params={"query": kw, "per_page": 3, "orientation": "landscape"},
                    headers={"Authorization": f"Client-ID {access_key}"},
                )
                if resp.status_code != 200:
                    continue
                for photo in resp.json().get("results", []):
                    results.append({
                        "url": photo["urls"]["regular"],
                        "source": "stock",
                        "alt": photo.get("alt_description") or kw,
                        "attribution": f'Photo by {photo["user"]["name"]} on Unsplash',
                    })
                    if len(results) >= count:
                        break
    except Exception:
        return results
    return results


def _picsum_fallback(niche: str, count: int) -> list[dict]:
    results = []
    for i in range(count):
        seed = f"leadforge-{niche}-{i}".replace("_", "-")
        results.append({
            "url": f"https://picsum.photos/seed/{seed}/1600/900",
            "source": "stock",
            "alt": f"Professional placeholder for {niche.replace('_', ' ')}",
        })
    return results


GEMINI_IMAGE_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-002:predict"
GROK_IMAGE_ENDPOINT = "https://api.x.ai/v1/images/generations"
STABILITY_ENDPOINT = "https://api.stability.ai/v2beta/stable-image/generate/core"


async def _generate_with_gemini(client: httpx.AsyncClient, api_key: str, prompt: str) -> str | None:
    try:
        resp = await client.post(
            f"{GEMINI_IMAGE_ENDPOINT}?key={api_key}",
            json={"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1}},
        )
        if resp.status_code != 200:
            return None
        predictions = resp.json().get("predictions", [])
        if predictions and predictions[0].get("bytesBase64Encoded"):
            return f"data:image/png;base64,{predictions[0]['bytesBase64Encoded']}"
    except Exception:
        return None
    return None


async def _generate_with_grok(client: httpx.AsyncClient, api_key: str, prompt: str) -> str | None:
    try:
        resp = await client.post(
            GROK_IMAGE_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "grok-2-image", "prompt": prompt, "n": 1},
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", [])
        if data and data[0].get("url"):
            return data[0]["url"]
    except Exception:
        return None
    return None


async def _generate_with_stability(client: httpx.AsyncClient, api_key: str, prompt: str) -> str | None:
    try:
        resp = await client.post(
            STABILITY_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            files={"none": (None, "")},
            data={"prompt": prompt[:2000], "output_format": "png", "aspect_ratio": "16:9"},
        )
        if resp.status_code != 200:
            return None
        content_type = (resp.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            body = resp.json()
            image_b64 = body.get("image") or (body.get("images") or [None])[0]
            if image_b64:
                return f"data:image/png;base64,{image_b64}"
            return None
        if resp.content and len(resp.content) > 100:
            return f"data:image/png;base64,{base64.b64encode(resp.content).decode('ascii')}"
    except Exception:
        return None
    return None


async def _generate_ai_images(db: Session, niche: str, count: int) -> list[dict]:
    stability_key = get_setting(db, "IMAGE_GEN_API_KEY")
    gemini_key = get_setting(db, "GEMINI_API_KEY")
    grok_key = get_setting(db, "GROK_API_KEY")
    if not (stability_key or gemini_key or grok_key):
        return []
    keywords = NICHE_IMAGE_KEYWORDS.get(niche, ["professional business"])
    results = []
    async with httpx.AsyncClient(timeout=90) as client:
        for kw in keywords[:count]:
            prompt = (
                f"A generic, professional concept photo representing a {kw}. "
                f"Clean, modern, editorial commercial photography style. "
                f"Must not include any readable brand names, logos, or signage."
            )
            url = None
            if stability_key:
                url = await _generate_with_stability(client, stability_key, prompt)
            if not url and gemini_key:
                url = await _generate_with_gemini(client, gemini_key, prompt)
            if not url and grok_key:
                url = await _generate_with_grok(client, grok_key, prompt)
            if url:
                results.append({"url": url, "source": "ai_generated", "alt": f"Concept image: {kw}"})
    return results


def _url_key(item: dict | None) -> str:
    if not item:
        return ""
    return (item.get("url") or "").strip().split("?")[0].rstrip("/")


def _niche_pack_dict(niche: str) -> dict[str, dict]:
    pack = NICHE_IMAGE_PACKS.get(niche) or {}
    return {s: dict(pack[s]) for s in SECTION_SLOTS if s in pack}


def _picsum_for_slot(niche: str, slot: str, index: int) -> dict:
    seed = f"leadforge-{niche}-{slot}-{index}".replace("_", "-")
    return {
        "url": f"https://picsum.photos/seed/{seed}/1600/900",
        "source": "stock",
        "alt": f"Professional placeholder for {niche.replace('_', ' ')} — {slot}",
    }


async def get_business_images(
    db: Session,
    niche: str,
    place_id: str | None = None,
    website: str | None = None,
    facebook: str | None = None,
    instagram: str | None = None,
    prefer_pack: bool = False,
) -> dict:
    """Fill hero/about/gallery_1..4 with UNIQUE images. prefer_pack skips brand/places."""
    slots: dict[str, dict] = {}
    used_urls: set[str] = set()
    logo: dict | None = None

    def assign(slot: str, item: dict | None) -> bool:
        if slot in slots or not item or not item.get("url"):
            return False
        key = _url_key(item)
        if not key or key in used_urls:
            return False
        slots[slot] = item
        used_urls.add(key)
        return True

    if not prefer_pack:
        try:
            brand_assets = await brand_asset_service.fetch_brand_assets(website, facebook, instagram)
            if brand_assets.get("hero"):
                assign("hero", brand_assets["hero"])
            if brand_assets.get("logo"):
                logo = brand_assets["logo"]
        except Exception:
            pass
        if place_id:
            missing = [s for s in SECTION_SLOTS if s not in slots]
            if missing:
                photos = await _fetch_places_photos(db, place_id, len(missing) + 2)
                for slot in missing:
                    while photos:
                        if assign(slot, photos.pop(0)):
                            break

    pack = _niche_pack_dict(niche)
    for slot in SECTION_SLOTS:
        if slot not in slots and slot in pack:
            assign(slot, pack[slot])

    missing = [s for s in SECTION_SLOTS if s not in slots]
    if missing:
        stock = await _fetch_unsplash_stock(db, niche, len(missing) + 3)
        for slot in missing:
            while stock:
                if assign(slot, stock.pop(0)):
                    break

    missing = [s for s in SECTION_SLOTS if s not in slots]
    if missing:
        ai_imgs = await _generate_ai_images(db, niche, len(missing))
        for slot in missing:
            while ai_imgs:
                if assign(slot, ai_imgs.pop(0)):
                    break

    for i, slot in enumerate(SECTION_SLOTS):
        if slot not in slots:
            assign(slot, _picsum_for_slot(niche, slot, i))

    if logo:
        slots["logo"] = logo
    return slots
