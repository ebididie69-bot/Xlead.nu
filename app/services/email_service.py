"""
Email generation + sending.

Generation reuses the same Gemini key but a separate prompt/schema from the
business-analysis one. Sending goes through the Gmail API using the admin's
own OAuth token (scope requested at login: gmail.send) — never a generic
SMTP relay, so it sends as the real admin Gmail account and respects Gmail's
own deliverability/reputation.

Nothing in this module sends automatically: routers/emails.py only calls
send_via_gmail() from an explicit "approve and send" action.
"""
import base64
import json
from email.mime.text import MIMEText

import httpx
from sqlalchemy.orm import Session

from app.core.security import get_setting, decrypt
from app.models import AdminIdentity

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

EMAIL_SCHEMA = {
    "subject": "string, <=60 chars, no clickbait/spam trigger words",
    "body": "string, plain text, 3-4 short paragraphs, no HTML",
    "cta": "string, one clear next step, e.g. 'Reply if you'd like the free preview link'",
}


class EmailGenerationError(RuntimeError):
    pass


async def generate_email(db: Session, lead: dict, demo_url: str | None) -> dict:
    api_key = get_setting(db, "GEMINI_API_KEY")
    if not api_key:
        raise EmailGenerationError("Gemini API key not configured. Add it in Settings.")

    prompt = f"""You are writing a cold outreach email from a freelance web designer to a
local business owner who currently has no modern website. Be warm, specific,
and brief — not salesy or hyped. Mention one concrete, plausible detail about
their business type. {"Include this exact preview link once in the body: " + demo_url if demo_url else "Do not invent a link."}

Return ONLY a JSON object matching this schema, no markdown or commentary:
{json.dumps(EMAIL_SCHEMA, indent=2)}

Business:
{json.dumps(lead, indent=2)}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.8, "responseMimeType": "application/json"},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{GEMINI_ENDPOINT}?key={api_key}", json=payload)
    if resp.status_code != 200:
        raise EmailGenerationError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")

    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


async def send_via_gmail(admin: AdminIdentity, to_email: str, subject: str, body: str) -> str:
    """Sends via Gmail API using the admin's stored OAuth access token. Returns the Gmail message id."""
    access_token = decrypt(admin.access_token_enc)
    if not access_token:
        raise EmailGenerationError("No valid Gmail access token — please sign in again.")

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
    if resp.status_code >= 400:
        raise EmailGenerationError(f"Gmail send failed ({resp.status_code}): {resp.text[:300]}")

    return resp.json().get("id", "")
