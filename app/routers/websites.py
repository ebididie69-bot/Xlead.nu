import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_db
from app.core.security import require_admin
from app.models import Lead, GeneratedWebsite
from app.services.gemini_service import analyze_business, GeminiError
from app.services.ai_service import AIQuotaError, AINotConfiguredError
from app.services.screenshot_service import capture_homepage_screenshot
from app.services.image_service import get_business_images

router = APIRouter(prefix="/api/websites", tags=["websites"])

NICHE_TEMPLATE_MAP = {
    "gym_fitness": "gym_fitness",
    "salon_spa": "salon_spa",
    "makeup_studio": "makeup_studio",
    "real_estate_agency": "real_estate_agency",
    "dental_clinic": "dental_clinic",
    "construction_company": "construction_company",
    "car_dealership": "car_dealership",
    "car_rental": "car_rental",
    "hotel_guest_house": "hotel_guest_house",
    "furniture_interior_design": "furniture_interior_design",
    "cleaning_company": "cleaning_company",
    "bakery_cafe": "bakery_cafe",
    "law_firm": "law_firm",
    "photography_studio": "photography_studio",
    "event_planning": "event_planning",
    "auto_repair_garage": "auto_repair_garage",
}

BUILT_TEMPLATES = set(NICHE_TEMPLATE_MAP.values())

DEMO_EXPIRY_DAYS = 30


def _new_demo_token() -> str:
    return secrets.token_urlsafe(9)


class GenerateWebsiteRequest(BaseModel):
    lead_id: str


@router.post("/generate")
async def generate_website(req: GenerateWebsiteRequest, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    lead = db.get(Lead, req.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    template_key = NICHE_TEMPLATE_MAP.get(lead.niche)
    if not template_key:
        raise HTTPException(400, f"No template configured for niche '{lead.niche}'")
    if template_key not in BUILT_TEMPLATES:
        raise HTTPException(
            409,
            f"The '{lead.niche}' template isn't built yet — currently available: "
            f"{', '.join(sorted(BUILT_TEMPLATES))}.",
        )

    lead_dict = {
        "name": lead.business_name, "description": lead.description,
        "address": lead.address, "phone": lead.phone, "email": lead.email,
        "category": lead.category, "google_rating": lead.google_rating,
        "review_count": lead.review_count, "opening_hours": lead.opening_hours,
    }

    try:
        analysis = await analyze_business(db, lead_dict, lead.niche)
    except AIQuotaError as e:
        raise HTTPException(429, f"AI quota exceeded: {e}. Both Grok and Gemini rate limits hit — wait a few minutes and try again.")
    except AINotConfiguredError as e:
        raise HTTPException(422, str(e))
    except GeminiError as e:
        raise HTTPException(502, str(e))

    token = _new_demo_token()
    while db.query(GeneratedWebsite).filter_by(demo_token=token).first():
        token = _new_demo_token()

    place_id = (lead.raw_source_data or {}).get("place_id")
    images = await get_business_images(
        db, niche=lead.niche, place_id=place_id,
        website=lead.website, facebook=lead.facebook, instagram=lead.instagram,
    )

    site = GeneratedWebsite(
        lead_id=lead.id,
        business_name=lead.business_name,
        demo_token=token,
        template_key=template_key,
        theme={},
        generated_json=analysis,
        enabled_sections=analysis.get("enabled_sections", []),
        images=images,
        status="draft",
        expiry_date=datetime.utcnow() + timedelta(days=DEMO_EXPIRY_DAYS),
    )
    db.add(site)
    db.commit()
    db.refresh(site)

    return {
        "id": site.id,
        "demo_token": site.demo_token,
        "status": site.status,
        "image_slots": list((images or {}).keys()),
    }


@router.post("/{website_id}/regenerate-content")
async def regenerate_content(website_id: str, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    site = db.get(GeneratedWebsite, website_id)
    if not site:
        raise HTTPException(404, "Website not found")
    lead = db.get(Lead, site.lead_id)

    lead_dict = {
        "name": lead.business_name, "description": lead.description,
        "address": lead.address, "phone": lead.phone, "email": lead.email,
        "category": lead.category, "google_rating": lead.google_rating,
        "review_count": lead.review_count, "opening_hours": lead.opening_hours,
    }
    try:
        analysis = await analyze_business(db, lead_dict, lead.niche)
    except AIQuotaError as e:
        raise HTTPException(429, f"AI quota exceeded: {e}. Both Grok and Gemini rate limits hit — wait a few minutes and try again.")
    except AINotConfiguredError as e:
        raise HTTPException(422, str(e))
    except GeminiError as e:
        raise HTTPException(502, str(e))

    site.generated_json = analysis
    site.enabled_sections = analysis.get("enabled_sections", [])
    site.theme = {}
    flag_modified(site, "generated_json")
    flag_modified(site, "enabled_sections")
    db.commit()
    return {"ok": True}


@router.post("/{website_id}/regenerate-images")
async def regenerate_images(website_id: str, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    site = db.get(GeneratedWebsite, website_id)
    if not site:
        raise HTTPException(404, "Website not found")
    lead = db.get(Lead, site.lead_id)
    place_id = (lead.raw_source_data or {}).get("place_id")
    images = await get_business_images(
        db,
        niche=lead.niche,
        place_id=place_id,
        website=lead.website,
        facebook=lead.facebook,
        instagram=lead.instagram,
        prefer_pack=True,  # curated unique slots first — no hero repeats
    )
    site.images = images
    flag_modified(site, "images")
    db.commit()
    urls = [((images or {}).get(s) or {}).get("url") for s in ("hero", "about", "gallery_1", "gallery_2", "gallery_3", "gallery_4")]
    uniq = len({(u or "").split("?")[0] for u in urls if u})
    return {
        "ok": True,
        "slot_count": len(images or {}),
        "unique_urls": uniq,
        "slots": list((images or {}).keys()),
        "images": images,
    }


class ContentEditRequest(BaseModel):
    generated_json: dict


@router.patch("/{website_id}/content")
def edit_content(website_id: str, req: ContentEditRequest, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    site = db.get(GeneratedWebsite, website_id)
    if not site:
        raise HTTPException(404, "Website not found")
    site.generated_json = req.generated_json
    flag_modified(site, "generated_json")
    db.commit()
    return {"ok": True}


@router.post("/{website_id}/screenshot")
async def generate_screenshot(website_id: str, db: Session = Depends(get_db), admin=Depends(require_admin)):
    site = db.get(GeneratedWebsite, website_id)
    if not site:
        raise HTTPException(404, "Website not found")
    path = await capture_homepage_screenshot(site.demo_token, admin=admin, db=db)
    site.screenshot_path = path
    db.commit()
    return {"screenshot_path": path}


@router.get("")
def list_websites(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    sites = db.query(GeneratedWebsite).order_by(GeneratedWebsite.created_at.desc()).all()
    return [
        {
            "id": s.id, "business_name": s.business_name, "template_key": s.template_key,
            "status": s.status, "demo_token": s.demo_token, "screenshot_path": s.screenshot_path,
            "created_at": s.created_at, "expiry_date": s.expiry_date,
            "generated_json": s.generated_json, "images": s.images, "theme": s.theme,
        }
        for s in sites
    ]


@router.patch("/{website_id}/status")
def update_status(website_id: str, status: str, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    if status not in ("draft", "published", "expired"):
        raise HTTPException(400, "Invalid status")
    site = db.get(GeneratedWebsite, website_id)
    if not site:
        raise HTTPException(404, "Website not found")
    site.status = status
    db.commit()
    return {"ok": True}
