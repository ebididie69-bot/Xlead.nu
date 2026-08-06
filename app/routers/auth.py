"""
Single-admin Google OAuth.

Flow:
  GET /auth/login     -> redirects to Google's consent screen
  GET /auth/callback  -> Google redirects back here with a code
  POST /auth/logout   -> clears the session cookie

Only the Google account whose email matches ADMIN_EMAIL (env var) is allowed
to create/refresh a session. Anyone else who completes Google's OAuth screen
still gets rejected at the callback — this is what makes it single-admin
rather than open sign-up.
"""
import os
from datetime import datetime, timedelta

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AdminIdentity
from app.core.security import get_setting, encrypt, require_admin

router = APIRouter(prefix="/auth", tags=["auth"])

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")  # the one email allowed to sign in


def _build_oauth(db: Session) -> OAuth:
    """
    OAuth client credentials come from the Settings page (encrypted vault),
    not hardcoded — so the admin can rotate them without touching env vars
    or redeploying, aside from the very first bootstrap.
    """
    client_id = get_setting(db, "GOOGLE_OAUTH_CLIENT_ID") or os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = get_setting(db, "GOOGLE_OAUTH_CLIENT_SECRET") or os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(500, "Google OAuth credentials are not configured yet. Add them in Settings.")

    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={
            "scope": (
                "openid email profile "
                "https://www.googleapis.com/auth/gmail.send "
                "https://www.googleapis.com/auth/drive.file"
            )
        },
    )
    return oauth


@router.get("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    oauth = _build_oauth(db)
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    oauth = _build_oauth(db)
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}

    email = userinfo.get("email")
    if not ADMIN_EMAIL:
        raise HTTPException(500, "ADMIN_EMAIL is not set on the server — refusing to sign anyone in.")
    if email != ADMIN_EMAIL:
        # Reject anyone who isn't the configured admin, even with a valid Google login.
        return RedirectResponse(f"{FRONTEND_URL}/login?error=unauthorized")

    admin = db.query(AdminIdentity).filter_by(google_sub=userinfo["sub"]).first()
    if not admin:
        admin = AdminIdentity(google_sub=userinfo["sub"], email=email)
        db.add(admin)

    admin.name = userinfo.get("name")
    admin.picture = userinfo.get("picture")
    admin.access_token_enc = encrypt(token.get("access_token"))
    if token.get("refresh_token"):  # Google only sends this on first consent
        admin.refresh_token_enc = encrypt(token.get("refresh_token"))
    expires_in = token.get("expires_in", 3600)
    admin.token_expiry = datetime.utcnow() + timedelta(seconds=expires_in)
    db.commit()

    request.session["admin_id"] = admin.id
    return RedirectResponse(f"{FRONTEND_URL}/dashboard")


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
async def me(admin: AdminIdentity = Depends(require_admin)):
    return {"email": admin.email, "name": admin.name, "picture": admin.picture}
