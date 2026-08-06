"""
Security core: encryption at rest for API keys/tokens, and the single-admin
session guard.

MASTER_KEY must come from an environment variable (Vercel/host secret),
never from source code or the database. Losing it means re-entering all
Settings-page keys, which is the correct tradeoff vs. storing it alongside
the data it protects.
"""
import os
from functools import lru_cache
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import EncryptedSetting, AdminIdentity


@lru_cache
def _cipher() -> Fernet:
    key = os.getenv("MASTER_KEY")
    if not key:
        raise RuntimeError(
            "MASTER_KEY env var is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
            "and set it in your host's environment variables."
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    if plaintext is None:
        return None
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str | None:
    if not ciphertext:
        return None
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return None


# ---------------------------------------------------------------------------
# Secrets vault: get/set API keys entered on the Settings page
# ---------------------------------------------------------------------------

KNOWN_SETTINGS = {
    "GEMINI_API_KEY",
    "GROK_API_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GMAIL_SENDER_ADDRESS",
    "GOOGLE_DRIVE_FOLDER_ID",
    "IMAGE_GEN_API_KEY",
    "GOOGLE_PLACES_API_KEY",  # optional: enables real business photos + ratings enrichment
    "UNSPLASH_ACCESS_KEY",    # optional: generic niche stock photos, free tier
}


def set_setting(db: Session, key: str, value: str) -> None:
    if key not in KNOWN_SETTINGS:
        raise ValueError(f"Unknown setting key: {key}")
    row = db.get(EncryptedSetting, key)
    enc = encrypt(value)
    if row:
        row.value_enc = enc
    else:
        row = EncryptedSetting(key=key, value_enc=enc)
        db.add(row)
    db.commit()


def get_setting(db: Session, key: str) -> str | None:
    row = db.get(EncryptedSetting, key)
    return decrypt(row.value_enc) if row else None


def get_all_settings_masked(db: Session) -> dict:
    """For the Settings page GET: never return raw secrets, just whether set."""
    rows = {r.key: r for r in db.query(EncryptedSetting).all()}
    return {k: {"configured": k in rows} for k in KNOWN_SETTINGS}


# ---------------------------------------------------------------------------
# Single-admin session guard
# ---------------------------------------------------------------------------

def require_admin(request: Request, db: Session = Depends(get_db)) -> AdminIdentity:
    """
    Dependency for every protected route. Reads the signed session cookie
    (set during the OAuth callback, see routers/auth.py), and confirms it
    matches the one AdminIdentity row that is allowed to exist.
    """
    admin_id = request.session.get("admin_id")
    if not admin_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")
    admin = db.get(AdminIdentity, admin_id)
    if not admin:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session invalid")
    return admin
