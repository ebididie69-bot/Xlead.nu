from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import datetime

from app.database import get_db
from app.core.security import require_admin, get_setting
from app.models import Lead, GeneratedWebsite, EmailDraft, AdminIdentity
from app.services.email_service import generate_email, send_via_gmail, EmailGenerationError

router = APIRouter(prefix="/api/emails", tags=["emails"])


class GenerateEmailRequest(BaseModel):
    lead_id: str


class EditEmailRequest(BaseModel):
    subject: str | None = None
    body: str | None = None
    cta: str | None = None


@router.post("/generate")
async def generate(
    req: GenerateEmailRequest,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    lead = db.get(Lead, req.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    site = db.query(GeneratedWebsite).filter_by(lead_id=lead.id).first()
    demo_url = None
    if site:
        import os
        frontend_url = get_setting(db, "FRONTEND_URL") or os.getenv("FRONTEND_URL") or ""
        demo_url = f"{frontend_url.rstrip('/')}/demo/{site.demo_token}"

    lead_dict = {
        "name": lead.business_name,
        "niche": lead.niche,
        "category": lead.category,
        "city": lead.city,
        "country": lead.country,
        "address": lead.address,
        "phone": lead.phone,
        "email": lead.email,
        "website": lead.website,
        "facebook": lead.facebook,
        "instagram": lead.instagram,
    }

    try:
        generated = await generate_email(db, lead_dict, demo_url)
    except EmailGenerationError as e:
        raise HTTPException(502, str(e))

    draft = EmailDraft(
        lead_id=lead.id,
        subject=generated["subject"],
        body=generated["body"],
        cta=generated.get("cta"),
        status="draft",
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return {
        "id": draft.id,
        "lead_id": draft.lead_id,
        "subject": draft.subject,
        "body": draft.body,
        "cta": draft.cta,
        "status": draft.status,
    }


@router.get("")
def list_emails(
    status: str | None = None,
    lead_id: str | None = None,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    q = db.query(EmailDraft)
    if status:
        q = q.filter(EmailDraft.status == status)
    if lead_id:
        q = q.filter(EmailDraft.lead_id == lead_id)
    drafts = q.order_by(EmailDraft.created_at.desc()).all()
    return [
        {
            "id": d.id,
            "lead_id": d.lead_id,
            "subject": d.subject,
            "body": d.body,
            "cta": d.cta,
            "status": d.status,
            "failure_reason": getattr(d, "failure_reason", None),
            "created_at": str(d.created_at) if d.created_at else None,
            "sent_at": str(d.sent_at) if d.sent_at else None,
        }
        for d in drafts
    ]


@router.patch("/{draft_id}")
def edit_draft(
    draft_id: str,
    req: EditEmailRequest,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    draft = db.get(EmailDraft, draft_id)
    if not draft:
        raise HTTPException(404, "Draft not found")
    if draft.status == "sent":
        raise HTTPException(400, "Cannot edit an already-sent email")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(draft, field, value)
    if draft.status in ("approved", "failed"):
        draft.status = "draft"
        draft.failure_reason = None
    db.commit()
    return {"id": draft.id, "status": draft.status, "subject": draft.subject, "body": draft.body, "cta": draft.cta}


@router.post("/{draft_id}/approve")
def approve_draft(
    draft_id: str,
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
):
    draft = db.get(EmailDraft, draft_id)
    if not draft:
        raise HTTPException(404, "Draft not found")
    if draft.status == "sent":
        raise HTTPException(400, "Already sent")
    draft.status = "approved"
    draft.failure_reason = None
    db.commit()
    return {"id": draft.id, "status": draft.status}


@router.post("/{draft_id}/send")
async def send_draft(
    draft_id: str,
    db: Session = Depends(get_db),
    admin: AdminIdentity = Depends(require_admin),
):
    """
    Explicit send — the ONLY code path that calls Gmail API.
    Requires status=='approved' and a valid email on the lead.
    """
    draft = db.get(EmailDraft, draft_id)
    if not draft:
        raise HTTPException(404, "Draft not found")
    if draft.status != "approved":
        raise HTTPException(
            400,
            f"Only approved drafts can be sent (current status: {draft.status}). "
            f"If failed, click Approve again first."
            + (f" Last error: {draft.failure_reason}" if draft.failure_reason else ""),
        )

    lead = db.get(Lead, draft.lead_id)
    if not lead or not (lead.email or "").strip():
        raise HTTPException(
            400,
            "This lead has no email address — fix the lead email in Lead Finder / re-import, then try again",
        )

    try:
        message_id = await send_via_gmail(db, admin, lead.email.strip(), draft.subject, draft.body)
        draft.status = "sent"
        draft.gmail_message_id = message_id
        draft.sent_at = datetime.utcnow()
        draft.failure_reason = None
    except Exception as e:
        draft.status = "failed"
        draft.failure_reason = str(e)
        db.commit()
        raise HTTPException(502, f"Send failed: {e}")

    db.commit()
    return {"ok": True, "gmail_message_id": message_id, "sent_to": lead.email}
