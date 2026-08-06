"""
Gemini business-analysis service.

Contract: Gemini is asked for STRUCTURED JSON ONLY, matching BUSINESS_ANALYSIS_SCHEMA.
It never generates HTML or React — the React templates own all markup, and
the AI only fills the content slots they read from. This keeps templates
reusable across every business in a niche and prevents the model from
inventing markup that doesn't match the design system.
"""
import json
import httpx
from sqlalchemy.orm import Session

from app.core.security import get_setting

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

# The exact shape the frontend templates expect. Sent to Gemini as part of
# the prompt so it knows precisely which keys to fill.
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


class GeminiError(RuntimeError):
    pass


def _build_prompt(lead: dict, niche: str) -> str:
    return f"""You are a branding and copywriting assistant for a web design agency.
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


async def analyze_business(db: Session, lead: dict, niche: str) -> dict:
    api_key = get_setting(db, "GEMINI_API_KEY")
    if not api_key:
        raise GeminiError("Gemini API key not configured. Add it in Settings.")

    prompt = _build_prompt(lead, niche)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "responseMimeType": "application/json",
        },
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{GEMINI_ENDPOINT}?key={api_key}", json=payload)

    if resp.status_code != 200:
        raise GeminiError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise GeminiError(f"Unexpected Gemini response shape: {e}")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise GeminiError(f"Gemini did not return valid JSON: {e}")

    return parsed
