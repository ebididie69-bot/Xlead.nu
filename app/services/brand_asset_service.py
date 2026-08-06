"""
Brand asset discovery.

Before falling back to stock photos or AI generation, try to pull the
business's *actual* logo/brand image from whatever web presence they already
have (their listed website, Facebook page, or Instagram profile). This is the
highest-trust image tier — genuinely theirs — and sits ahead of Google Places
photos in the priority chain when available, since it also often surfaces
a proper logo (which Places photos rarely do).

Deliberately dependency-light: no BeautifulSoup, just a couple of regexes
against a raw HTML fetch. Fails soft (returns []) on anything — blocked
requests, JS-rendered pages, missing tags — since this is a best-effort tier,
not a requirement.
"""
import re
import httpx

OG_IMAGE_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
OG_IMAGE_RE_ALT = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.IGNORECASE)
TWITTER_IMAGE_RE = re.compile(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
FAVICON_RE = re.compile(r'<link[^>]+rel=["\'](?:shortcut icon|icon|apple-touch-icon)["\'][^>]+href=["\']([^"\']+)["\']', re.IGNORECASE)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LeadForgeAI-BrandFetch/1.0)"
}


def _resolve(base_url: str, maybe_relative: str) -> str:
    if maybe_relative.startswith("http"):
        return maybe_relative
    from urllib.parse import urljoin
    return urljoin(base_url, maybe_relative)


async def _extract_brand_image(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        resp = await client.get(url, headers=HEADERS, timeout=10, follow_redirects=True)
        if resp.status_code >= 400:
            return None
        html = resp.text
    except httpx.HTTPError:
        return None

    for pattern in (OG_IMAGE_RE, OG_IMAGE_RE_ALT, TWITTER_IMAGE_RE):
        match = pattern.search(html)
        if match:
            return {
                "url": _resolve(str(resp.url), match.group(1)),
                "source": "real",
                "alt": "Business brand image",
            }
    return None


async def _extract_logo(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        resp = await client.get(url, headers=HEADERS, timeout=10, follow_redirects=True)
        if resp.status_code >= 400:
            return None
        html = resp.text
    except httpx.HTTPError:
        return None

    match = FAVICON_RE.search(html)
    if match:
        return {
            "url": _resolve(str(resp.url), match.group(1)),
            "source": "real",
            "alt": "Business logo",
        }
    return None


async def fetch_brand_assets(website: str | None, facebook: str | None, instagram: str | None) -> dict:
    """
    Returns up to {"hero": {...}, "logo": {...}} using whichever real web
    presence is available, checked in order: business's own website first
    (most likely to have a proper og:image + favicon/logo), then Facebook,
    then Instagram. Instagram/Facebook pages are frequently JS-rendered and
    will often yield nothing — that's expected and fine, the caller falls
    through to stock/AI tiers.
    """
    candidates = [u for u in (website, facebook, instagram) if u]
    if not candidates:
        return {}

    assets: dict = {}
    async with httpx.AsyncClient() as client:
        for url in candidates:
            if "hero" not in assets:
                hero = await _extract_brand_image(client, url)
                if hero:
                    assets["hero"] = hero
            if "logo" not in assets:
                logo = await _extract_logo(client, url)
                if logo:
                    assets["logo"] = logo
            if len(assets) == 2:
                break
    return assets
