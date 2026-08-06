"""
ORM models.

Design notes:
- Single-admin app: no `users` table with roles/permissions. The AdminIdentity
  table holds exactly one row representing the one Google account allowed in.
- Websites are stored as structured JSON (generated_json), never raw HTML.
- Secrets (API keys) live in the EncryptedSetting table, encrypted at rest —
  see app/core/security.py for the Fernet cipher wrapper.
"""
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, ForeignKey, Boolean, JSON
)
from sqlalchemy.orm import relationship
from app.database import Base


def gen_id() -> str:
    return uuid.uuid4().hex


class AdminIdentity(Base):
    """The single administrator's linked Google identity + OAuth tokens."""
    __tablename__ = "admin_identity"

    id = Column(String, primary_key=True, default=gen_id)
    google_sub = Column(String, unique=True, nullable=False)  # Google's stable user id
    email = Column(String, nullable=False)
    name = Column(String)
    picture = Column(String)

    # OAuth tokens for calling Gmail / Drive APIs on the admin's behalf.
    # Encrypted at rest via EncryptedString (see core/security.py).
    access_token_enc = Column(Text)
    refresh_token_enc = Column(Text)
    token_expiry = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)


class EncryptedSetting(Base):
    """
    Key/value vault for admin-supplied secrets: Gemini key, Grok key,
    Google OAuth client id/secret, image-gen key, etc.
    Values are stored encrypted; only ever decrypted server-side, in memory,
    right before an outbound API call.
    """
    __tablename__ = "encrypted_settings"

    key = Column(String, primary_key=True)   # e.g. "GEMINI_API_KEY"
    value_enc = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Lead(Base):
    __tablename__ = "leads"

    id = Column(String, primary_key=True, default=gen_id)
    business_name = Column(String, nullable=False)
    description = Column(Text)
    niche = Column(String, index=True)
    country = Column(String)
    city = Column(String)

    address = Column(String)
    phone = Column(String)
    email = Column(String)
    website = Column(String)  # existing site if any (used to score/disqualify)
    facebook = Column(String)
    instagram = Column(String)

    google_rating = Column(Float)
    review_count = Column(Integer)
    opening_hours = Column(JSON)
    category = Column(String)

    website_status = Column(String, default="none")  # none|broken|facebook_only|instagram_only|modern
    lead_score = Column(Integer, default=0)

    raw_source_data = Column(JSON)  # unprocessed data pulled from search, for audit/debug

    created_at = Column(DateTime, default=datetime.utcnow)

    generated_website = relationship("GeneratedWebsite", back_populates="lead", uselist=False)
    email_drafts = relationship("EmailDraft", back_populates="lead")


class GeneratedWebsite(Base):
    __tablename__ = "generated_websites"

    id = Column(String, primary_key=True, default=gen_id)
    lead_id = Column(String, ForeignKey("leads.id"), nullable=False)
    business_name = Column(String, nullable=False)  # denormalized for admin UI only, never in URLs

    demo_token = Column(String, unique=True, nullable=False, index=True)  # random, in the public URL
    template_key = Column(String, nullable=False)  # e.g. "gym_fitness"
    theme = Column(JSON)  # colors/fonts chosen by AI

    generated_json = Column(JSON, nullable=False)  # full AI-generated content, never HTML
    enabled_sections = Column(JSON)  # list of section keys to render
    images = Column(JSON)  # {slot: {url, source: real|stock|ai_generated, alt, attribution?}}

    screenshot_path = Column(String)  # path/URL to homepage screenshot
    status = Column(String, default="draft")  # draft|published|expired

    created_at = Column(DateTime, default=datetime.utcnow)
    expiry_date = Column(DateTime)

    lead = relationship("Lead", back_populates="generated_website")


class EmailDraft(Base):
    __tablename__ = "email_drafts"

    id = Column(String, primary_key=True, default=gen_id)
    lead_id = Column(String, ForeignKey("leads.id"), nullable=False)

    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    cta = Column(String)

    status = Column(String, default="draft")  # draft|approved|sent|failed
    gmail_message_id = Column(String)  # populated once sent via Gmail API
    failure_reason = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime)

    lead = relationship("Lead", back_populates="email_drafts")
