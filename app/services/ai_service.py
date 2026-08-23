"""
Unified AI service for LeadForge.

Priority:
  1. Groq Cloud — tried first if GROK_API_KEY is configured in Settings.
     Get a free key at https://console.groq.com (no credit card needed).
  2. Gemini (Google) — fallback if Groq isn't configured or quota hit.

IMPORTANT: brand_colors and theme_recommendation are intentionally EXCLUDED
from BUSINESS_ANALYSIS_SCHEMA. Each template has its own fixed premium color
palette — the AI provides content only, never design decisions. Colors must
never be overridden by AI output.
"""
import json
import httpx
from sqlalchemy.orm import Session

from app.core.security import get_setting

GROK_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROK_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.6-27b",
    "llama3-70b-8192",
]

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

# NO brand_colors, NO theme_recommendation, NO business_tone, NO target_audience.
# Templates have fixed premium palettes — AI writes copy only.
BUSINESS_ANALYSIS_SCHEMA = {
    "hero_title": "string <= 8 words, punchy and specific to this business niche",
    "hero_subtitle": "string <= 20 words, one clear value proposition for this business",
    "about": "string, 2-3 sentences about this specific business, warm and credible",
    "services": [{"title": "string", "description": "string, 1-2 sentences"}],
    "testimonials": [{"name": "string", "quote": "string, authentic-sounding", "role": "string e.g. Regular Client"}],
    "faq": [{"question": "string", "answer": "string, clear and concise"}],
    "call_to_action": {"headline": "string", "button_text": "string, action-oriented"},
    "seo": {
        "title": "string <=60 chars",
        "description": "string <=155 chars",
        "keywords": ["string"]
    },
    "enabled_sections": ["hero", "about", "services", "gallery", "testimonials", "faq", "contact", "footer"],
}

EMAIL_SCHEMA = {
    "subject": "string <=60 chars, no spam trigger words, specific to this business",
    "body": "string, plain text, 3-4 short paragraphs, no HTML, warm and personal",
    "cta": "string, one clear next step e.g. 'Reply to get your free preview link'",
}


class AIError(RuntimeError):
    pass

class AIQuotaError(AIError):
    pass

class AINotConfiguredError(AIError):
    pass


async def _call_groq(api_key: str, prompt: str) -> str:
    last_error = None
    for model in GROK_MODELS:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a copywriting assistant for a web design agency. "
                        "You write business content only — headlines, descriptions, services, testimonials. "
                        "You never suggest colors, fonts, themes or any design decisions. "
                        "Always respond with raw JSON only — no markdown, no code fences, no commentary."
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
            last_error = AIError(f"Model {model} not available, trying next…")
            continue
        if resp.status_code != 200:
            raise AIError(f"Groq API error {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise AIError(f"Unexpected Groq response shape: {e}")
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
        raise AIError(f"Unexpected Gemini response shape: {e}")


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
    prompt = f"""You are a copywriting assistant for a web design agency.
Write website CONTENT ONLY for a local business. Do NOT suggest colors, fonts,
themes, or any design decisions — the template handles all visual design.

Produce a JSON object matching EXACTLY this schema (same keys, no extra keys, no markdown, no code fences):

{json.dumps(BUSINESS_ANALYSIS_SCHEMA, indent=2)}

Business niche: {niche}
Business information:
{json.dumps(lead, indent=2)}

Rules:
- Output ONLY the raw JSON object. No preamble, no commentary, no ``` fences.
- Only include sections in enabled_sections that make sense for this business.
- hero_title must be specific to this niche and location — never generic filler.
- Services should reflect what this type of business actually offers (3-6 services).
- Testimonials should sound authentic and human, not like marketing copy.
- Keep all copy warm, professional and human — never robotic or sales-heavy.
"""
    return await _generate(db, prompt)


async def generate_email(db: Session, lead: dict, demo_url: str | None) -> dict:
    link_instruction = (
        f"Include this exact preview link naturally once in the body: {demo_url}"
        if demo_url else "Do not invent or include any links."
    )
    prompt = f"""You are writing a cold outreach EMAIL from a freelance web designer to a local
business owner who currently has no modern website. Be warm, specific, and brief.
Write like a real person — not a marketing department. {link_instruction}

Return ONLY a JSON object matching this schema, no markdown or commentary:
{json.dumps(EMAIL_SCHEMA, indent=2)}

Business details:
{json.dumps(lead, indent=2)}
"""
    return await _generate(db, prompt)
