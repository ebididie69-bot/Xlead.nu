import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.security import require_admin
from app.models import Lead, EmailDraft
from app.services.lead_scoring import (
    classify_website_status, filter_disqualified, compute_lead_score
)
from app.services import lead_finder_service

router = APIRouter(prefix="/api/leads", tags=["leads"])

SUPPORTED_NICHES = [
    "gym_fitness", "salon_spa", "makeup_studio", "real_estate_agency",
    "dental_clinic", "construction_company", "car_dealership", "car_rental",
    "hotel_guest_house", "furniture_interior_design", "cleaning_company",
    "bakery_cafe", "law_firm", "photography_studio", "event_planning", "auto_repair_garage",
]


class LeadSearchRequest(BaseModel):
    niche: str
    country: str
    city: str | None = None  # blank/omitted = search the whole country
    max_leads: int = 20


@router.get("/niches")
def list_niches(_admin=Depends(require_admin)):
    return SUPPORTED_NICHES


@router.post("/search")
async def search_leads(req: LeadSearchRequest, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    if req.niche not in SUPPORTED_NICHES:
        raise HTTPException(400, f"Unsupported niche. Choose from {SUPPORTED_NICHES}")
    if req.max_leads < 1 or req.max_leads > 200:
        raise HTTPException(400, "max_leads must be between 1 and 200")

    city_clean = (req.city or "").strip()

    try:
        raw_results = await lead_finder_service.find_businesses(
            niche=req.niche, country=req.country, city=city_clean or None, max_leads=req.max_leads
        )
    except httpx.TimeoutException:
        scope = "this country" if not city_clean else f"{city_clean}, {req.country}"
        raise HTTPException(
            504,
            f"The search for {scope} took too long (OpenStreetMap's free API can be slow for "
            "large areas). Try a smaller area, a specific city, or try again in a moment.",
        )
    except httpx.HTTPError as exc:
        # Surface the real underlying OS-level cause (DNS failure, connection
        # refused, network unreachable, etc.) instead of httpx's generic
        # summary, so we can tell a routing/DNS issue apart from an actual
        # IP block by the upstream service.
        root_cause = repr(exc.__cause__) if exc.__cause__ else repr(exc)
        raise HTTPException(502, f"Could not reach OpenStreetMap right now: {exc} | root_cause: {root_cause}")

    # --- Dedup against past runs for this exact niche+country ---
    # Rule: never re-surface a business we've already emailed for this niche+country
    # (the user's core ask — don't market the same lead twice), and skip re-inserting
    # a business we already have on file here (same OSM node), to avoid duplicate rows.
    existing_for_scope = (
        db.query(Lead)
        .filter(Lead.niche == req.niche, Lead.country == req.country)
        .all()
    )
    already_emailed_keys = set()
    already_seen_osm_ids = set()
    for existing in existing_for_scope:
        osm_id = (existing.raw_source_data or {}).get("osm_id")
        if osm_id:
            already_seen_osm_ids.add(osm_id)
        was_emailed = any(d.status == "sent" for d in existing.email_drafts)
        if was_emailed:
            if osm_id:
                already_emailed_keys.add(osm_id)
            # Fallback key for businesses without a stable OSM id (e.g. manually added)
            already_emailed_keys.add((existing.business_name.strip().lower(), (existing.city or "").strip().lower()))

    def _is_dupe(biz: dict) -> bool:
        osm_id = biz.get("osm_id")
        name_key = (biz["name"].strip().lower(), city_clean.lower())
        if osm_id and (osm_id in already_emailed_keys or osm_id in already_seen_osm_ids):
            return True
        if name_key in already_emailed_keys:
            return True
        return False

    raw_results = [b for b in raw_results if not _is_dupe(b)]

    saved = []
    for biz in raw_results:
        status = classify_website_status(
            website_url=biz.get("website"),
            website_reachable=biz.get("website_reachable"),
            facebook_url=biz.get("facebook"),
            instagram_url=biz.get("instagram"),
        )
        if filter_disqualified(status):
            continue  # has a modern working website — not a lead

        score = compute_lead_score(
            status=status,
            google_rating=biz.get("google_rating"),
            review_count=biz.get("review_count"),
            has_phone=bool(biz.get("phone")),
            has_email=bool(biz.get("email")),
        )

        lead = Lead(
            business_name=biz["name"],
            description=biz.get("description"),
            niche=req.niche,
            country=req.country,
            city=city_clean or None,
            address=biz.get("address"),
            phone=biz.get("phone"),
            email=biz.get("email"),
            website=biz.get("website"),
            facebook=biz.get("facebook"),
            instagram=biz.get("instagram"),
            google_rating=biz.get("google_rating"),
            review_count=biz.get("review_count"),
            opening_hours=biz.get("opening_hours"),
            category=biz.get("category"),
            website_status=status.value,
            lead_score=score,
            raw_source_data=biz,
        )
        db.add(lead)
        saved.append(lead)

    db.commit()
    # Highest-opportunity leads first
    saved.sort(key=lambda l: l.lead_score, reverse=True)
    return [
        {
            "id": l.id, "business_name": l.business_name, "niche": l.niche,
            "website_status": l.website_status, "lead_score": l.lead_score,
            "phone": l.phone, "email": l.email, "city": l.city,
        }
        for l in saved
    ]


@router.get("")
def list_leads(niche: str | None = None, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    q = db.query(Lead)
    if niche:
        q = q.filter(Lead.niche == niche)
    leads = q.order_by(Lead.lead_score.desc()).all()
    return [
        {
            "id": l.id, "business_name": l.business_name, "niche": l.niche,
            "city": l.city, "website_status": l.website_status,
            "lead_score": l.lead_score, "phone": l.phone, "email": l.email,
        }
        for l in leads
    ]


@router.get("/{lead_id}")
def get_lead(lead_id: str, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    return {c.name: getattr(lead, c.name) for c in lead.__table__.columns}
