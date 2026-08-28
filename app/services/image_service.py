"""
Image sourcing for generated demo sites, in strict priority order:

  0. Brand assets scraped from the lead's website / social
  1. REAL business photos — Google Places Photos
  2. GENERIC NICHE STOCK — Unsplash (needs UNSPLASH_ACCESS_KEY)
  3. AI-GENERATED CONCEPT ART — Stability (IMAGE_GEN_API_KEY), then Gemini Imagen, then Grok image
  4. FREE DETERMINISTIC FALLBACK — picsum.photos seeded by niche+slot
     so demos never ship with empty hero/gallery slots.

Every returned image carries a `source` tag so the frontend can label
non-real images. Nothing here silently claims to be the business's premises.
"""
import base64
import os
import httpx
from sqlalchemy.orm import Session

from app.core.security import get_setting
from app.services import brand_asset_service

UNSPLASH_ENDPOINT = "https://api.unsplash.com/search/photos"

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


class ImageSourcingError(RuntimeError):
    pass


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
    """Always-available images so demos never have empty heroes."""
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
    """xAI image API only — a Groq chat key will fail and return None."""
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
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            files={"none": (None, "")},
            data={
                "prompt": prompt[:2000],
                "output_format": "png",
                "aspect_ratio": "16:9",
            },
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
            b64 = base64.b64encode(resp.content).decode("ascii")
            return f"data:image/png;base64,{b64}"
    except Exception:
        return None
    return None


async def _generate_ai_images(db: Session, niche: str, count: int) -> list[dict]:
    """Stability first (typical image key), then Gemini Imagen, then xAI Grok image."""
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
                results.append({
                    "url": url,
                    "source": "ai_generated",
                    "alt": f"Concept image: {kw}",
                })
    return results


async def get_business_images(
    db: Session,
    niche: str,
    place_id: str | None = None,
    website: str | None = None,
    facebook: str | None = None,
    instagram: str | None = None,
) -> dict:
    needed = len(SECTION_SLOTS)
    images: list[dict] = []
    logo: dict | None = None

    try:
        brand_assets = await brand_asset_service.fetch_brand_assets(website, facebook, instagram)
        if brand_assets.get("hero"):
            images.append(brand_assets["hero"])
        if brand_assets.get("logo"):
            logo = brand_assets["logo"]
    except Exception:
        pass

    if place_id and len(images) < needed:
        images += await _fetch_places_photos(db, place_id, needed - len(images))
    if len(images) < needed:
        images += await _fetch_unsplash_stock(db, niche, needed - len(images))
    if len(images) < needed:
        images += await _generate_ai_images(db, niche, needed - len(images))
    if len(images) < needed:
        images += _picsum_fallback(niche, needed - len(images))

    slots = {slot: images[i] for i, slot in enumerate(SECTION_SLOTS) if i < len(images)}
    if logo:
        slots["logo"] = logo
    return slots
