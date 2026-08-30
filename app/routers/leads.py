import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.security import require_admin
from app.models import Lead, EmailDraft, GeneratedWebsite
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
    city: str | None = None
    max_leads: int = 20


@router.get("/network-test")
async def network_test(_admin=Depends(require_admin)):
    targets = {
        "github (unrelated control)": "https://api.github.com",
        "nominatim (already used for geocoding)": "https://nominatim.openstreetmap.org/status",
        "overpass main": "https://overpass-api.de/api/status",
        "overpass mirror (kumi)": "https://overpass.kumi.systems/api/status",
    }
    results = {}
    async with httpx.AsyncClient(
        timeout=10, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    ) as client:
        for label, url in targets.items():
            try:
                resp = await client.get(url, headers={"User-Agent": "LeadForgeAI/1.0"})
                results[label] = f"OK ({resp.status_code})"
            except Exception as exc:
                results[label] = f"FAILED: {exc!r}"
    return results


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
            db=db, niche=req.niche, country=req.country, city=city_clean or None, max_leads=req.max_leads
        )
    except lead_finder_service.PlacesApiError as exc:
        raise HTTPException(502, f"Google Places API error: {exc}")
    except httpx.TimeoutException:
        scope = "this country" if not city_clean else f"{city_clean}, {req.country}"
        raise HTTPException(
            504,
            f"The search for {scope} took too long. Try a smaller area, a specific city, or try again in a moment.",
        )
    except httpx.HTTPError as exc:
        root_cause = repr(exc.__cause__) if exc.__cause__ else repr(exc)
        raise HTTPException(
            502,
            f"Could not reach the business-lookup service right now: {exc} | root_cause: {root_cause}. "
            "If this keeps happening, add a GOOGLE_PLACES_API_KEY in Settings for a more reliable source.",
        )

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
            continue

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
def list_leads(
    niche: str | None = None,
    country: str | None = None,
    city: str | None = None,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    q = db.query(Lead)
    if niche:
        q = q.filter(Lead.niche == niche)
    if country:
        q = q.filter(Lead.country == country)
    if city:
        q = q.filter(Lead.city == city)
    leads = q.order_by(Lead.lead_score.desc()).all()
    # Soft-archived leads stay in DB (demo + sent emails) but leave the UI list
    leads = [l for l in leads if not (l.raw_source_data or {}).get("archived")]

    lead_ids = [l.id for l in leads]
    tokens_by_lead = {}
    if lead_ids:
        for gw in db.query(GeneratedWebsite).filter(GeneratedWebsite.lead_id.in_(lead_ids)).all():
            tokens_by_lead[gw.lead_id] = gw.demo_token

    return [
        {
            "id": l.id, "business_name": l.business_name, "niche": l.niche,
            "country": l.country, "city": l.city, "website_status": l.website_status,
            "lead_score": l.lead_score, "phone": l.phone, "email": l.email,
            "demo_token": tokens_by_lead.get(l.id),
        }
        for l in leads
    ]


@router.get("/{lead_id}")
def get_lead(lead_id: str, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    return {c.name: getattr(lead, c.name) for c in lead.__table__.columns}


class ImportedBusiness(BaseModel):
    name: str
    niche: str
    country: str
    city: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    facebook: str | None = None
    instagram: str | None = None
    google_rating: float | None = None
    review_count: int | None = None
    source_id: str | None = None
    source: str = "termux_import"


class ImportRequest(BaseModel):
    businesses: list[ImportedBusiness]


def _require_import_token(request: Request):
    import os
    expected = os.getenv("IMPORT_API_KEY", "")
    if not expected:
        raise HTTPException(503, "Import endpoint not configured on this server (IMPORT_API_KEY not set).")
    token = request.headers.get("X-Import-Token", "")
    if not token or token != expected:
        raise HTTPException(401, "Invalid or missing X-Import-Token header.")


@router.post("/import")
async def import_leads(
    req: ImportRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    _require_import_token(request)
    from app.services.lead_scoring import classify_website_status, filter_disqualified, compute_lead_score

    existing_leads = db.query(Lead).all()
    seen_source_ids = set()
    seen_name_city = set()
    already_emailed_ids = set()

    for l in existing_leads:
        src_id = (l.raw_source_data or {}).get("osm_id")
        if src_id:
            seen_source_ids.add(src_id)
        seen_name_city.add((l.business_name.strip().lower(), (l.city or "").strip().lower()))
        has_sent = any(d.status == "sent" for d in l.email_drafts)
        if has_sent and src_id:
            already_emailed_ids.add(src_id)

    added = []
    skipped_dedup = 0
    skipped_emailed = 0
    skipped_disqualified = 0

    for biz in req.businesses:
        src_id = biz.source_id
        if src_id and src_id in already_emailed_ids:
            skipped_emailed += 1
            continue
        name_key = (biz.name.strip().lower(), (biz.city or "").strip().lower())
        if (src_id and src_id in seen_source_ids) or name_key in seen_name_city:
            skipped_dedup += 1
            continue

        website_status = classify_website_status(
            website_url=biz.website,
            website_reachable=None,
            facebook_url=biz.facebook,
            instagram_url=biz.instagram,
        )
        if filter_disqualified(website_status):
            skipped_disqualified += 1
            continue

        score = compute_lead_score(
            status=website_status,
            google_rating=biz.google_rating,
            review_count=biz.review_count,
            has_phone=bool(biz.phone),
            has_email=bool(biz.email),
        )

        lead = Lead(
            business_name=biz.name,
            niche=biz.niche,
            country=biz.country,
            city=biz.city,
            address=biz.address,
            phone=biz.phone,
            email=biz.email,
            website=biz.website,
            facebook=biz.facebook,
            instagram=biz.instagram,
            google_rating=biz.google_rating,
            review_count=biz.review_count,
            website_status=website_status,
            lead_score=score,
            raw_source_data={"osm_id": src_id, "source": biz.source},
        )
        db.add(lead)
        added.append(biz.name)
        if src_id:
            seen_source_ids.add(src_id)
        seen_name_city.add(name_key)

    db.commit()
    return {
        "added": len(added),
        "skipped_already_in_db": skipped_dedup,
        "skipped_already_emailed": skipped_emailed,
        "skipped_disqualified": skipped_disqualified,
        "added_names": added,
    }


@router.post("/import-web")
async def import_leads_web(
    req: ImportRequest,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    from app.services.lead_scoring import classify_website_status, filter_disqualified, compute_lead_score

    existing_leads = db.query(Lead).all()
    seen_source_ids = set()
    seen_name_city = set()
    already_emailed_ids = set()

    for l in existing_leads:
        src_id = (l.raw_source_data or {}).get("osm_id")
        if src_id:
            seen_source_ids.add(src_id)
        seen_name_city.add((l.business_name.strip().lower(), (l.city or "").strip().lower()))
        has_sent = any(d.status == "sent" for d in l.email_drafts)
        if has_sent and src_id:
            already_emailed_ids.add(src_id)

    added_leads = []
    skipped_dedup = 0
    skipped_emailed = 0
    skipped_disqualified = 0

    for biz in req.businesses:
        src_id = biz.source_id
        if src_id and src_id in already_emailed_ids:
            skipped_emailed += 1
            continue
        name_key = (biz.name.strip().lower(), (biz.city or "").strip().lower())
        if (src_id and src_id in seen_source_ids) or name_key in seen_name_city:
            skipped_dedup += 1
            continue

        website_status = classify_website_status(
            website_url=biz.website,
            website_reachable=None,
            facebook_url=biz.facebook,
            instagram_url=biz.instagram,
        )
        if filter_disqualified(website_status):
            skipped_disqualified += 1
            continue

        score = compute_lead_score(
            status=website_status,
            google_rating=biz.google_rating,
            review_count=biz.review_count,
            has_phone=bool(biz.phone),
            has_email=bool(biz.email),
        )
        lead = Lead(
            business_name=biz.name,
            niche=biz.niche or "",
            country=biz.country or "",
            city=biz.city,
            address=biz.address,
            phone=biz.phone,
            email=biz.email,
            website=biz.website,
            facebook=biz.facebook,
            instagram=biz.instagram,
            website_status=website_status,
            lead_score=score,
            raw_source_data={"osm_id": src_id, "source": biz.source},
        )
        db.add(lead)
        db.flush()
        added_leads.append({"id": lead.id, "business_name": lead.business_name})
        if src_id:
            seen_source_ids.add(src_id)
        seen_name_city.add(name_key)

    db.commit()
    return {
        "added": len(added_leads),
        "skipped_already_in_db": skipped_dedup,
        "skipped_already_emailed": skipped_emailed,
        "skipped_disqualified": skipped_disqualified,
        "added_leads": added_leads,
    }


@router.delete("/{lead_id}")
def delete_lead(
    lead_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    """
    Hide a lead from the Lead Finder list (soft archive) when it has a
    generated website or a sent email — so the prospect can still open
    their demo site and outreach history is preserved.

    Hard-delete only when there is no website and no sent email.
    """
    from sqlalchemy.orm.attributes import flag_modified

    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    has_site = db.query(GeneratedWebsite).filter_by(lead_id=lead_id).first() is not None
    has_sent = any(d.status == "sent" for d in (lead.email_drafts or []))

    if has_site or has_sent:
        data = dict(lead.raw_source_data or {})
        data["archived"] = True
        lead.raw_source_data = data
        flag_modified(lead, "raw_source_data")
        db.commit()
        return {
            "deleted": lead_id,
            "mode": "archived",
            "demo_kept": has_site,
            "emails_kept": True,
            "message": "Lead hidden from list. Demo site and sent emails were kept.",
        }

    db.query(GeneratedWebsite).filter_by(lead_id=lead_id).delete()
    db.query(EmailDraft).filter_by(lead_id=lead_id).delete()
    db.delete(lead)
    db.commit()
    return {"deleted": lead_id, "mode": "hard", "demo_kept": False}
