"""
Unified AI service for LeadForge.

Priority:
  1. Grok (xAI) — tried first if GROK_API_KEY is configured in Settings.
     xAI's free tier is more generous than Gemini's.
  2. Gemini (Google) — fallback if Grok isn't configured, or if Grok itself
     returns a quota/rate-limit error (429).

Both providers are asked for raw JSON only — no markdown, no HTML. The same
BUSINESS_ANALYSIS_SCHEMA and EMAIL_SCHEMA are sent to whichever model runs,
so the output contract is identical regardless of which provider handles it.

Graceful degradation: a 429 from either provider is caught and re-raised as
AIQuotaError (a distinct subclass), so callers can decide whether to retry
with the other provider or return a friendlier error to the user — rather
than surfacing a raw 429 as an uncaught crash.
"""
import json
import httpx
from sqlalchemy.orm import Session

from app.core.security import get_setting

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
GROK_ENDPOINT = "https://api.x.ai/v1/chat/completions"
GROK_MODEL = "grok-3-mini"  # cheapest/fastest xAI model; swap to "grok-3" for higher quality

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
BUSINESS_ANALYSIS_SCHEMA = {
    "business_tone": "string, e.g. 'warm and professional' or 'bold and energetic'",
    "target_audience": "string, one sentence",
    "brand_colors": {"primary": "#hex", "secondary": "#hex", "accent": "#hex"},
    "hero_title": "string, <= 8 words",
    "hero_subtitle": "string, <= 20 words",
    "about": "string, 2-3 sentences",
    "services": [{"title": "string", "description": "string"}],
    "testimonials": [{"name": "string", "quote": "string", "role": "string"}],
    "faq": [{"question": "string", "answer": "string"}],
    "call_to_action": {"headline": "string", "button_text": "string"},
    "seo": {"title": "string, <=60 chars", "description": "string, <=155 chars", "keywords": ["string"]},
    "theme_recommendation": "string, one of: light | dark | warm | bold | minimal",
    "enabled_sections": ["hero", "about", "services", "gallery", "testimonials", "faq", "contact", "map", "footer"],
}

EMAIL_SCHEMA = {
    "subject": "string, <=60 chars, no clickbait/spam trigger words",
    "body": "string, plain text, 3-4 short paragraphs, no HTML",
    "cta": "string, one clear next step, e.g. 'Reply if you'd like the free preview link'",
}

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class AIError(RuntimeError):
    """Base class for all AI service errors."""

class AIQuotaError(AIError):
    """Raised when a provider returns 429 (quota/rate-limit exceeded)."""

class AINotConfiguredError(AIError):
    """Raised when neither Grok nor Gemini keys are configured."""


# ---------------------------------------------------------------------------
# Internal: provider calls
# ---------------------------------------------------------------------------
async def _call_grok(api_key: str, prompt: str) -> str:
    """Call Grok and return the raw text response. Raises AIQuotaError on 429."""
    payload = {
        "model": GROK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a branding and copywriting assistant. "
                    "You always respond with raw JSON only — no markdown, "
                    "no code fences, no commentary before or after the JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            GROK_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    if resp.status_code == 429:
        raise AIQuotaError(f"Grok quota exceeded: {resp.text[:200]}")
    if resp.status_code != 200:
        raise AIError(f"Grok API error {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise AIError(f"Unexpected Grok response shape: {e}")


async def _call_gemini(api_key: str, prompt: str) -> str:
    """Call Gemini and return the raw text response. Raises AIQuotaError on 429."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json"},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{GEMINI_ENDPOINT}?key={api_key}", json=payload)
    if resp.status_code == 429:
        raise AIQuotaError(f"Gemini quota exceeded: {resp.text[:200]}")
    if resp.status_code != 200:
        raise AIError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise AIError(f"Unexpected Gemini response shape: {e}")


async def _generate(db: Session, prompt: str) -> dict:
    """
    Try Grok first, fall back to Gemini on quota errors or if Grok isn't
    configured. Raises AINotConfiguredError if neither key is set, and
    AIQuotaError only if both providers are quota-exhausted simultaneously
    (extremely unlikely, but handled cleanly).
    """
    grok_key = get_setting(db, "GROK_API_KEY")
    gemini_key = get_setting(db, "GEMINI_API_KEY")

    if not grok_key and not gemini_key:
        raise AINotConfiguredError(
            "No AI key configured. Add a GROK_API_KEY or GEMINI_API_KEY in Settings."
        )

    raw_text = None
    last_error = None

    # --- Try Grok first ---
    if grok_key:
        try:
            raw_text = await _call_grok(grok_key, prompt)
        except AIQuotaError as e:
            last_error = e
            raw_text = None  # fall through to Gemini
        # Any other AIError propagates immediately — it's a real config/API problem

    # --- Fall back to Gemini ---
    if raw_text is None and gemini_key:
        try:
            raw_text = await _call_gemini(gemini_key, prompt)
        except AIQuotaError as e:
            last_error = e
            raw_text = None

    if raw_text is None:
        if last_error:
            raise last_error  # both quota-exhausted
        raise AINotConfiguredError("No AI provider available.")

    # Strip accidental markdown fences some models add despite instructions
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise AIError(f"AI did not return valid JSON: {e}\nRaw response: {raw_text[:300]}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def analyze_business(db: Session, lead: dict, niche: str) -> dict:
    """
    Generate structured website content for a lead. Returns a dict matching
    BUSINESS_ANALYSIS_SCHEMA. Raises AIError subclasses on failure.
    """
    prompt = f"""You are a branding and copywriting assistant for a web design agency.
Given the public business information below, produce a JSON object that matches
EXACTLY this schema (same keys, no extra keys, no markdown, no HTML, no code fences):

{json.dumps(BUSINESS_ANALYSIS_SCHEMA, indent=2)}

Business niche: {niche}
Business information:
{json.dumps(lead, indent=2)}

Rules:
- Output ONLY the raw JSON object. No preamble, no commentary, no ``` fences.
- Only include sections in enabled_sections that make sense for this business
  (e.g. omit "gallery" if there's no indication of visual products/spaces).
- Testimonials should read as plausible generic examples, not claims about
  real named customers, since none were provided.
- Keep copy specific to this business's actual niche and location, not generic filler.
"""
    return await _generate(db, prompt)


async def generate_email(db: Session, lead: dict, demo_url: str | None) -> dict:
    """
    Generate a personalized cold outreach email for a lead. Returns a dict
    matching EMAIL_SCHEMA. Raises AIError subclasses on failure.
    """
    link_instruction = (
        f"Include this exact preview link once in the body: {demo_url}"
        if demo_url
        else "Do not invent a link."
    )
    prompt = f"""You are writing a cold outreach email from a freelance web designer to a
local business owner who currently has no modern website. Be warm, specific,
and brief — not salesy or hyped. Mention one concrete, plausible detail about
their business type. {link_instruction}

Return ONLY a JSON object matching this schema, no markdown or commentary:
{json.dumps(EMAIL_SCHEMA, indent=2)}

Business:
{json.dumps(lead, indent=2)}
"""
    return await _generate(db, prompt)
