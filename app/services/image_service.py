"""
Image sourcing for generated demo sites, in strict priority order:

  1. REAL business photos — via Google Places Photos, if the lead has a
     matched Place ID and GOOGLE_PLACES_API_KEY is configured. These are
     genuinely that business's storefront/interior/logo photos.
  2. GENERIC NICHE STOCK — Unsplash, keyed by niche (e.g. "gym interior").
     Clearly not the real business — used only as a plausible, professional
     placeholder so the demo doesn't look broken while communicating the
     *idea* of the site, not asserting it depicts their premises.
  3. AI-GENERATED CONCEPT ART — only if the admin has configured
     IMAGE_GEN_API_KEY. Prompted explicitly as generic concept visuals.

Every returned image carries a `source` tag so the frontend can label
non-real images ("Concept image" / stock attribution) and so nothing here
ever silently claims to be the business's actual premises.
"""
import os
import httpx
from sqlalchemy.orm import Session

from app.core.security import get_setting
from app.services import brand_asset_service

UNSPLASH_ENDPOINT = "https://api.unsplash.com/search/photos"

# Niche -> search terms used for both Places-photo category hints and Unsplash fallback.
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
    """Tier 1: real photos of the actual business, via Google Places."""
    api_key = get_setting(db, "GOOGLE_PLACES_API_KEY")
    if not api_key or not place_id:
        return []

    async with httpx.AsyncClient(timeout=15) as client:
        detail_resp = await client.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id": place_id, "fields": "photos", "key": api_key},
        )
        photos = detail_resp.json().get("result", {}).get("photos", [])[:max_photos]

        results = []
        for p in photos:
            photo_resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/photo",
                params={"photoreference": p["photo_reference"], "maxwidth": 1200, "key": api_key},
                follow_redirects=True,
            )
            results.append({"url": str(photo_resp.url), "source": "real", "alt": "Business photo"})
        return results


async def _fetch_unsplash_stock(db: Session, niche: str, count: int) -> list[dict]:
    """Tier 2: generic, professional, clearly-not-this-business stock photos."""
    access_key = get_setting(db, "UNSPLASH_ACCESS_KEY") or os.getenv("UNSPLASH_ACCESS_KEY")
    if not access_key:
        return []

    keywords = NICHE_IMAGE_KEYWORDS.get(niche, ["professional business"])
    results = []
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
    return results


GEMINI_IMAGE_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-002:predict"
GROK_IMAGE_ENDPOINT = "https://api.x.ai/v1/images/generations"
STABILITY_ENDPOINT = "https://api.stability.ai/v2beta/stable-image/generate/core"


async def _generate_with_gemini(client: httpx.AsyncClient, api_key: str, prompt: str) -> str | None:
    resp = await client.post(
        f"{GEMINI_IMAGE_ENDPOINT}?key={api_key}",
        json={"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1}},
    )
    if resp.status_code != 200:
        return None
    predictions = resp.json().get("predictions", [])
    if predictions and predictions[0].get("bytesBase64Encoded"):
        return f"data:image/png;base64,{predictions[0]['bytesBase64Encoded']}"
    return None


async def _generate_with_grok(client: httpx.AsyncClient, api_key: str, prompt: str) -> str | None:
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
    return None


async def _generate_with_stability(client: httpx.AsyncClient, api_key: str, prompt: str) -> str | None:
    resp = await client.post(
        STABILITY_ENDPOINT,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        files={"none": (None, "")},
        data={"prompt": prompt, "output_format": "png"},
    )
    if resp.status_code != 200:
        return None
    image_b64 = resp.json().get("image")
    return f"data:image/png;base64,{image_b64}" if image_b64 else None


async def _generate_ai_images(db: Session, niche: str, count: int) -> list[dict]:
    """
    Tier 3 (last resort): AI-generated generic concept art. Tries the admin's
    configured providers in order — Gemini (Imagen) first, then Grok, then
    Stability — using whichever keys are actually set. Explicitly prompted as
    generic concept visuals, never claiming to depict the real business.
    """
    gemini_key = get_setting(db, "GEMINI_API_KEY")
    grok_key = get_setting(db, "GROK_API_KEY")
    stability_key = get_setting(db, "IMAGE_GEN_API_KEY")
    if not (gemini_key or grok_key or stability_key):
        return []

    keywords = NICHE_IMAGE_KEYWORDS.get(niche, ["professional business"])
    results = []
    async with httpx.AsyncClient(timeout=60) as client:
        for kw in keywords[:count]:
            prompt = (
                f"A generic, professional concept photo representing a {kw}. "
                f"Clean, modern, editorial commercial photography style. "
                f"Must not include any readable brand names, logos, or signage."
            )
            url = None
            if gemini_key:
                url = await _generate_with_gemini(client, gemini_key, prompt)
            if not url and grok_key:
                url = await _generate_with_grok(client, grok_key, prompt)
            if not url and stability_key:
                url = await _generate_with_stability(client, stability_key, prompt)
            if url:
                results.append({"url": url, "source": "ai_generated", "alt": f"Concept image: {kw}"})
    return results


async def get_business_images(
    db: Session,
    niche: str,
    place_id: str | None = None,
    website: str | None = None,
    facebook: str | None = None,
    instagram: str | None = None,
) -> dict:
    """
    Returns {slot_name: {url, source, alt, attribution?}} for hero/about/gallery
    slots (plus a "logo" slot when discoverable), trying tiers in order:
      0. Brand assets scraped from the lead's own website/Facebook/Instagram
         (their real og:image + favicon/logo) — the highest-trust source.
      1. Google Places photos, if a matched Place ID is available.
      2. Generic niche stock (Unsplash).
      3. AI-generated concept art (Gemini, then Grok, then Stability).
    Slots that can't be filled by any tier are omitted — the frontend renders
    a subtle placeholder rather than a broken image.
    """
    needed = len(SECTION_SLOTS)
    images: list[dict] = []
    logo: dict | None = None

    brand_assets = await brand_asset_service.fetch_brand_assets(website, facebook, instagram)
    if brand_assets.get("hero"):
        images.append(brand_assets["hero"])
    if brand_assets.get("logo"):
        logo = brand_assets["logo"]

    if place_id:
        images += await _fetch_places_photos(db, place_id, needed - len(images))
    if len(images) < needed:
        images += await _fetch_unsplash_stock(db, niche, needed - len(images))
    if len(images) < needed:
        images += await _generate_ai_images(db, niche, needed - len(images))

    slots = {slot: images[i] for i, slot in enumerate(SECTION_SLOTS) if i < len(images)}
    if logo:
        slots["logo"] = logo
    return slots
