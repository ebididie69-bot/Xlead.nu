from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import datetime

from app.database import get_db
from app.core.security import require_admin
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
async def generate(req: GenerateEmailRequest, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    lead = db.get(Lead, req.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    if not lead.email:
        raise HTTPException(400, "This lead has no email address on file")

    site = db.query(GeneratedWebsite).filter_by(lead_id=lead.id).first()
    demo_url = None
    if site:
        # NOTE: FRONTEND_URL should be the deployed domain in production.
        demo_url = f"/demo/{site.demo_token}"

    lead_dict = {"name": lead.business_name, "category": lead.category, "city": lead.city}
    try:
        generated = await generate_email(db, lead_dict, demo_url)
    except EmailGenerationError as e:
        raise HTTPException(502, str(e))

    draft = EmailDraft(
        lead_id=lead.id, subject=generated["subject"], body=generated["body"],
        cta=generated.get("cta"), status="draft",
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return {"id": draft.id, "subject": draft.subject, "body": draft.body, "cta": draft.cta, "status": draft.status}


@router.get("")
def list_emails(status: str | None = None, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    q = db.query(EmailDraft)
    if status:
        q = q.filter(EmailDraft.status == status)
    drafts = q.order_by(EmailDraft.created_at.desc()).all()
    return [
        {
            "id": d.id, "lead_id": d.lead_id, "subject": d.subject,
            "status": d.status, "created_at": d.created_at, "sent_at": d.sent_at,
        }
        for d in drafts
    ]


@router.patch("/{draft_id}")
def edit_draft(draft_id: str, req: EditEmailRequest, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """Admin reviews/edits before approving — required step, nothing auto-sends."""
    draft = db.get(EmailDraft, draft_id)
    if not draft:
        raise HTTPException(404, "Draft not found")
    if draft.status == "sent":
        raise HTTPException(400, "Cannot edit an already-sent email")
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(draft, field, value)
    db.commit()
    return {"ok": True}


@router.post("/{draft_id}/approve")
def approve_draft(draft_id: str, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    draft = db.get(EmailDraft, draft_id)
    if not draft:
        raise HTTPException(404, "Draft not found")
    draft.status = "approved"
    db.commit()
    return {"ok": True}


@router.post("/{draft_id}/send")
async def send_draft(draft_id: str, db: Session = Depends(get_db), admin: AdminIdentity = Depends(require_admin)):
    """
    Explicit, one-at-a-time send action — this is the ONLY code path that
    calls the Gmail API to actually deliver mail. Requires status=='approved'
    so a draft can never be sent without the admin reviewing it first.
    """
    draft = db.get(EmailDraft, draft_id)
    if not draft:
        raise HTTPException(404, "Draft not found")
    if draft.status != "approved":
        raise HTTPException(400, "Only approved drafts can be sent")

    lead = db.get(Lead, draft.lead_id)
    try:
        message_id = await send_via_gmail(admin, lead.email, draft.subject, draft.body)
        draft.status = "sent"
        draft.gmail_message_id = message_id
        draft.sent_at = datetime.utcnow()
    except Exception as e:
        draft.status = "failed"
        draft.failure_reason = str(e)
        db.commit()
        raise HTTPException(502, f"Send failed: {e}")

    db.commit()
    return {"ok": True, "gmail_message_id": message_id}
