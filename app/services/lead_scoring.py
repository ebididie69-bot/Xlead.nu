"""
Lead scoring.

Kept as pure functions (no DB/network) so it's trivially unit-testable and
reusable from both the search pipeline and any re-scoring/admin tools.

Priority order: No Website > Broken Website > Facebook Only >
Instagram Only > Has Website (modern).

IMPORTANT CHANGE: We no longer filter out businesses with modern websites
at search time. Instead they get a low score (0-30) and are shown with a
"Has website" badge — the admin can still decide to target them if the site
looks outdated. This is critical for European markets where most businesses
have some kind of website, otherwise searches return zero results.
"""
from enum import Enum


class WebsiteStatus(str, Enum):
    NONE = "none"
    BROKEN = "broken"
    FACEBOOK_ONLY = "facebook_only"
    INSTAGRAM_ONLY = "instagram_only"
    MODERN = "modern"  # has a working website — low priority but not filtered out


_STATUS_BASE_SCORE = {
    WebsiteStatus.NONE: 100,
    WebsiteStatus.BROKEN: 85,
    WebsiteStatus.FACEBOOK_ONLY: 60,
    WebsiteStatus.INSTAGRAM_ONLY: 55,
    WebsiteStatus.MODERN: 20,  # low but not zero — shown, just ranked last
}


def classify_website_status(
    website_url: str | None,
    website_reachable: bool | None,
    facebook_url: str | None,
    instagram_url: str | None,
) -> WebsiteStatus:
    """
    website_reachable is the result of an upstream HTTP check (True = 200 OK,
    False = timeout/error). Pass None if not checked yet.
    """
    if website_url:
        if website_reachable is False:
            return WebsiteStatus.BROKEN
        if website_reachable is True:
            return WebsiteStatus.MODERN
        # Unknown reachability — treat as broken so it still surfaces as an
        # opportunity rather than being silently ranked as modern.
        return WebsiteStatus.BROKEN
    if facebook_url:
        return WebsiteStatus.FACEBOOK_ONLY
    if instagram_url:
        return WebsiteStatus.INSTAGRAM_ONLY
    return WebsiteStatus.NONE


def filter_disqualified(status: WebsiteStatus) -> bool:
    """
    Previously returned True for MODERN to filter them out entirely.
    Now always returns False — no leads are dropped at search time.
    The admin sees everything, ranked by score. Low-score leads
    (MODERN) appear at the bottom of the list.
    """
    return False  # never filter — let scoring handle priority


def compute_lead_score(
    status: WebsiteStatus,
    google_rating: float | None,
    review_count: int | None,
    has_phone: bool,
    has_email: bool,
) -> int:
    """
    0-100 score. Website absence/quality dominates (that's the whole premise
    of the outreach), with small boosts for an established business being an
    easier sell and having contact info available.
    """
    score = _STATUS_BASE_SCORE[status]

    # A business with a strong rating and review volume is more credible to
    # pitch — small nudge, capped so it can't overcome the website penalty.
    if google_rating and review_count:
        reputation_bonus = min(10, (google_rating - 3.0) * 3 + min(review_count, 50) / 25)
        score += max(0, reputation_bonus)

    if has_phone:
        score += 3
    if has_email:
        score += 2

    return max(0, min(100, round(score)))
