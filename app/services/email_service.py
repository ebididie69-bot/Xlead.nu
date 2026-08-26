"""
Email generation + sending.

Generation goes through ai_service.py (Grok first, Gemini fallback).
Sending uses the Gmail API with the admin's OAuth access token.
"""
import base64
from email.mime.text import MIMEText

import httpx
from sqlalchemy.orm import Session

from app.core.security import decrypt
from app.models import AdminIdentity
from app.services.ai_service import generate_email as _ai_generate_email, AIError

EmailGenerationError = AIError


async def generate_email(db: Session, lead: dict, demo_url: str | None) -> dict:
    return await _ai_generate_email(db, lead, demo_url)


async def send_via_gmail(admin: AdminIdentity, to_email: str, subject: str, body: str) -> str:
    """Sends via Gmail API using the admin's stored OAuth access token. Returns the Gmail message id."""
    access_token = decrypt(admin.access_token_enc)
    if not access_token:
        raise EmailGenerationError(
            "No valid Gmail access token — sign out and sign back in with Google to refresh permissions."
        )

    message = MIMEText(body)
    message["to"] = to_email
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"raw": raw},
        )
    if resp.status_code == 401:
        raise EmailGenerationError(
            "Gmail access token expired or revoked — sign out and sign back in with Google, then try again."
        )
    if resp.status_code >= 400:
        raise EmailGenerationError(f"Gmail send failed ({resp.status_code}): {resp.text[:300]}")

    return resp.json().get("id", "")
