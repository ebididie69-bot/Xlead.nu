"""
Unified AI service for LeadForge.

Priority:
  1. Groq Cloud — tried first if GROK_API_KEY is configured in Settings.
     Get a free key at https://console.groq.com (no credit card needed).
  2. Gemini (Google) — fallback if Groq isn't configured or quota hit.

Model: openai/gpt-oss-120b on Groq (replaces deprecated llama-3.3-70b-versatile
as of June 2026). Falls back to gpt-oss-20b if 120b is unavailable.
"""
import json
import httpx
from sqlalchemy.orm import Session

from app.core.security import get_setting

GROK_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROK_MODELS = [
    "openai/gpt-oss-120b",   # primary — recommended Groq replacement for llama-3.3-70b
    "openai/gpt-oss-20b",    # fallback if 120b unavailable
    "qwen/qwen3.6-27b",      # second fallback
]

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

BUSINESS_ANALYSIS_SCHEMA = {
    "business_tone": "string e.g. 'warm and professional'",
    "target_audience": "string one sentence",
    "brand_colors": {"primary": "#hex", "secondary": "#hex", "accent": "#hex"},
    "hero_title": "string <= 8 words",
    "hero_subtitle": "string <= 20 words",
    "about": "string 2-3 sentences",
    "services": [{"title": "string", "description": "string"}],
    "testimonials": [{"name": "string", "quote": "string", "role": "string"}],
    "faq": [{"question": "string", "answer": "string"}],
    "call_to_action": {"headline": "string", "button_text": "string"},
    "seo": {"title": "string <=60 chars", "description": "string <=155 chars", "keywords": ["string"]},
    "theme_recommendation": "one of: light | dark | warm | bold | minimal",
    "enabled_sections": ["hero","about","services","gallery","testimonials","faq","contact","map","footer"],
}

EMAIL_SCHEMA = {
    "subject": "string <=60 chars no spam words",
    "body": "string plain text 3-4 short paragraphs no HTML",
    "cta": "string one clear next step e.g. 'Reply to get your free demo link'",
}


class AIError(RuntimeError):
    pass

class AIQuotaError(AIError):
    pass

class AINotConfiguredError(AIError):
    pass


async def _call_groq(api_key: str, prompt: str) -> str:
    """Try each Groq model in order until one works."""
    last_error = None
    for model in GROK_MODELS:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a branding and copywriting assistant. "
                        "Always respond with raw JSON only — no markdown, "
                        "no code fences, no commentary before or after."
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
            raise AIQuotaError(f"Groq quota exceeded: {resp.text[:200]}")
        if resp.status_code == 404:
            last_error = AIError(f"Model {model} not found on Groq, trying next…")
            continue  # try next model
        if resp.status_code != 200:
            raise AIError(f"Groq API error {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise AIError(f"Unexpected Groq response: {e}")
    raise last_error or AIError("All Groq models failed.")


async def _call_gemini(api_key: str, prompt: str) -> str:
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
        raise AIError(f"Unexpected Gemini response: {e}")


async def _generate(db: Session, prompt: str) -> dict:
    groq_key = get_setting(db, "GROK_API_KEY")
    gemini_key = get_setting(db, "GEMINI_API_KEY")

    if not groq_key and not gemini_key:
        raise AINotConfiguredError(
            "No AI key configured. Add a GROK_API_KEY (free at console.groq.com) "
            "or GEMINI_API_KEY in Settings."
        )

    raw_text = None
    last_error = None

    if groq_key:
        try:
            raw_text = await _call_groq(groq_key, prompt)
        except AIQuotaError as e:
            last_error = e
            raw_text = None

    if raw_text is None and gemini_key:
        try:
            raw_text = await _call_gemini(gemini_key, prompt)
        except AIQuotaError as e:
            last_error = e
            raw_text = None

    if raw_text is None:
        raise (last_error or AINotConfiguredError("No AI provider available."))

    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise AIError(f"AI did not return valid JSON: {e}\nRaw: {raw_text[:300]}")


async def analyze_business(db: Session, lead: dict, niche: str) -> dict:
    prompt = f"""You are a branding and copywriting assistant for a web design agency.
Given the business information below, produce a JSON object matching EXACTLY this schema
(same keys, no extra keys, no markdown, no code fences):

{json.dumps(BUSINESS_ANALYSIS_SCHEMA, indent=2)}

Business niche: {niche}
Business information:
{json.dumps(lead, indent=2)}

Rules:
- Output ONLY the raw JSON object. No preamble, no commentary, no ``` fences.
- Only include sections in enabled_sections that make sense for this business.
- Keep copy specific to this business niche and location, not generic filler.
- Make testimonials sound plausible and natural, not obviously fake.
"""
    return await _generate(db, prompt)


async def generate_email(db: Session, lead: dict, demo_url: str | None) -> dict:
    link_instruction = (
        f"Include this exact preview link once in the body: {demo_url}"
        if demo_url else "Do not invent a link."
    )
    prompt = f"""You are writing a cold outreach EMAIL (not SMS, not WhatsApp — a professional email)
from a freelance web designer to a local business owner who currently has no modern website.
Be warm, specific, and brief — not salesy or hyped. Mention one concrete, plausible detail
about their business type. {link_instruction}

Return ONLY a JSON object matching this schema, no markdown or commentary:
{json.dumps(EMAIL_SCHEMA, indent=2)}

Business:
{json.dumps(lead, indent=2)}
"""
    return await _generate(db, prompt)
