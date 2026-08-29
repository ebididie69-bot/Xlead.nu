"""
Email generation + sending.

Generation goes through ai_service.py (Grok first, Gemini fallback).
Sending uses the Gmail API with the admin's OAuth access token.
Access tokens expire ~1h — we refresh them with the stored refresh_token.
"""
import base64
from datetime import datetime, timedelta
from email.mime.text import MIMEText

import httpx
from sqlalchemy.orm import Session

from app.core.security import decrypt, encrypt, get_setting
from app.models import AdminIdentity
from app.services.ai_service import generate_email as _ai_generate_email, AIError

EmailGenerationError = AIError


async def generate_email(db: Session, lead: dict, demo_url: str | None) -> dict:
    return await _ai_generate_email(db, lead, demo_url)


async def _refresh_access_token(db: Session, admin: AdminIdentity) -> str:
    """Exchange refresh_token for a new access_token; persist it on admin."""
    refresh = decrypt(admin.refresh_token_enc) if admin.refresh_token_enc else None
    if not refresh:
        raise EmailGenerationError(
            "No Google refresh token stored. Sign out, then sign in again and "
            "accept all Google permissions (Gmail). If Google does not show a consent "
            "screen, revoke app access at https://myaccount.google.com/permissions and retry."
        )

    client_id = get_setting(db, "GOOGLE_OAUTH_CLIENT_ID")
    client_secret = get_setting(db, "GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        import os
        client_id = client_id or os.getenv("GOOGLE_OAUTH_CLIENT_ID")
        client_secret = client_secret or os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise EmailGenerationError("Google OAuth client id/secret missing in Settings.")

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
    if resp.status_code >= 400:
        raise EmailGenerationError(
            f"Could not refresh Google token ({resp.status_code}): {resp.text[:250]}. "
            "Sign out and sign in again with Google."
        )

    data = resp.json()
    access = data.get("access_token")
    if not access:
        raise EmailGenerationError("Google token refresh returned no access_token.")

    admin.access_token_enc = encrypt(access)
    expires_in = int(data.get("expires_in", 3600))
    admin.token_expiry = datetime.utcnow() + timedelta(seconds=expires_in)
    # Google rarely returns a new refresh_token on refresh; keep the old one.
    if data.get("refresh_token"):
        admin.refresh_token_enc = encrypt(data["refresh_token"])
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return access


async def _ensure_access_token(db: Session, admin: AdminIdentity) -> str:
    """Return a usable access token, refreshing if expired or missing expiry."""
    access = decrypt(admin.access_token_enc) if admin.access_token_enc else None
    expiry = admin.token_expiry
    # Refresh 2 minutes early
    needs_refresh = (
        not access
        or expiry is None
        or expiry <= datetime.utcnow() + timedelta(minutes=2)
    )
    if needs_refresh:
        return await _refresh_access_token(db, admin)
    return access


async def send_via_gmail(
    db: Session,
    admin: AdminIdentity,
    to_email: str,
    subject: str,
    body: str,
) -> str:
    """
    Send via Gmail API as the signed-in admin.
    Returns Gmail message id.
    """
    to_email = (to_email or "").strip()
    if not to_email or "@" not in to_email:
        raise EmailGenerationError(f"Invalid lead email address: {to_email!r}")

    access_token = await _ensure_access_token(db, admin)

    message = MIMEText(body or "", _charset="utf-8")
    message["to"] = to_email
    message["subject"] = subject or "(no subject)"
    if admin.email:
        message["from"] = admin.email

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"raw": raw},
        )

        # One retry after forced refresh on 401
        if resp.status_code == 401:
            access_token = await _refresh_access_token(db, admin)
            resp = await client.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                headers={"Authorization": f"Bearer {access_token}"},
                json={"raw": raw},
            )

    if resp.status_code == 401:
        raise EmailGenerationError(
            "Gmail rejected the token (401). Enable Gmail API in Google Cloud Console, "
            "then sign out / sign in and grant gmail.send. "
            "Revoke old access: https://myaccount.google.com/permissions"
        )
    if resp.status_code == 403:
        raise EmailGenerationError(
            f"Gmail forbidden (403): {resp.text[:300]}. "
            "Enable the Gmail API for your OAuth project, and ensure the OAuth consent "
            "screen includes the gmail.send scope."
        )
    if resp.status_code >= 400:
        raise EmailGenerationError(
            f"Gmail send failed ({resp.status_code}): {resp.text[:400]}"
        )

    return resp.json().get("id", "")
