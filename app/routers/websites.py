import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.security import require_admin
from app.models import Lead, GeneratedWebsite
from app.services.gemini_service import analyze_business, GeminiError
from app.services.ai_service import AIQuotaError, AINotConfiguredError
from app.services.screenshot_service import capture_homepage_screenshot
from app.services.image_service import get_business_images

router = APIRouter(prefix="/api/websites", tags=["websites"])

# Niche -> which React template component renders it (see frontend/src/templates)
NICHE_TEMPLATE_MAP = {
    "gym_fitness": "GymFitnessTemplate",
    "salon_spa": "SalonSpaTemplate",
    "makeup_studio": "MakeupStudioTemplate",
    "real_estate_agency": "RealEstateTemplate",
    "dental_clinic": "DentalClinicTemplate",
    "construction_company": "ConstructionTemplate",
    "car_dealership": "CarDealershipTemplate",
    "car_rental": "CarRentalTemplate",
    "hotel_guest_house": "HotelGuestHouseTemplate",
    "furniture_interior_design": "FurnitureInteriorTemplate",
    "cleaning_company": "CleaningCompanyTemplate",
    "bakery_cafe": "BakeryCafeTemplate",
    "law_firm": "LawFirmTemplate",
    "photography_studio": "PhotographyStudioTemplate",
    "event_planning": "EventPlanningTemplate",
    "auto_repair_garage": "AutoRepairTemplate",
}

# Templates with a real, registered multi-page React component (see
# frontend/src/templates/index.js). Generation is blocked for anything not
# in this set so an admin gets a clear error instead of a broken demo link.
BUILT_TEMPLATES = set(NICHE_TEMPLATE_MAP.values())  # all 16 niches now have real templates

DEMO_EXPIRY_DAYS = 30


def _new_demo_token() -> str:
    # 12 random chars from an unambiguous urlsafe alphabet — short enough for
    # a clean /demo/<token> URL, long enough (>= 62^12) to not be guessable.
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
            f"{', '.join(sorted(BUILT_TEMPLATES))}. Choose a lead from one of those niches for now.",
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
        token = _new_demo_token()  # extremely unlikely collision, but be sure

    # Tier 1 (real photos) needs a Google Place ID, which is only present if
    # Places enrichment ran during lead search — see lead_finder_service.py.
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
        theme={},  # Colors fixed per template — never overridden by AI
        generated_json=analysis,
        enabled_sections=analysis.get("enabled_sections", []),
        images=images,
        status="draft",
        expiry_date=datetime.utcnow() + timedelta(days=DEMO_EXPIRY_DAYS),
    )
    db.add(site)
    db.commit()
    db.refresh(site)

    return {"id": site.id, "demo_token": site.demo_token, "status": site.status}


@router.post("/{website_id}/regenerate-content")
async def regenerate_content(website_id: str, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Re-runs Gemini analysis for this lead, replacing the site's copy in place (same token, same images)."""
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
    site.theme = {}  # Colors fixed per template — never overridden by AI
    db.commit()
    return {"ok": True}


@router.post("/{website_id}/regenerate-images")
async def regenerate_images(website_id: str, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Re-runs the tiered image search/generation (see services/image_service.py)."""
    site = db.get(GeneratedWebsite, website_id)
    if not site:
        raise HTTPException(404, "Website not found")
    lead = db.get(Lead, site.lead_id)
    place_id = (lead.raw_source_data or {}).get("place_id")
    site.images = await get_business_images(
        db, niche=lead.niche, place_id=place_id,
        website=lead.website, facebook=lead.facebook, instagram=lead.instagram,
    )
    db.commit()
    return {"ok": True, "images": site.images}


class ContentEditRequest(BaseModel):
    generated_json: dict


@router.patch("/{website_id}/content")
def edit_content(website_id: str, req: ContentEditRequest, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Manual admin edits to the AI-generated copy — same JSON contract Gemini fills, admin can override any field."""
    site = db.get(GeneratedWebsite, website_id)
    if not site:
        raise HTTPException(404, "Website not found")
    site.generated_json = req.generated_json
    db.commit()
    return {"ok": True}


@router.post("/{website_id}/screenshot")
async def generate_screenshot(website_id: str, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    site = db.get(GeneratedWebsite, website_id)
    if not site:
        raise HTTPException(404, "Website not found")
    path = await capture_homepage_screenshot(site.demo_token)
    site.screenshot_path = path
    db.commit()
    return {"screenshot_path": path}


@router.get("")
def list_websites(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """
    Returns full detail (including generated_json/images) for every site, not
    just summary fields. WebsitePreview.jsx reuses this list response to avoid
    a second endpoint — a bit more payload per row, but this list is admin-only
    and realistically stays in the dozens/hundreds, not a scale where that matters.
    """
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
