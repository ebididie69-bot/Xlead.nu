"""
Lead scoring.

Kept as pure functions (no DB/network) so it's trivially unit-testable and
reusable from both the search pipeline and any re-scoring/admin tools.

Priority order per the spec: No Website > Broken Website > Facebook Only >
Instagram Only > has a modern website (should usually be filtered out
entirely upstream, not just scored low).
"""
from enum import Enum


class WebsiteStatus(str, Enum):
    NONE = "none"
    BROKEN = "broken"
    FACEBOOK_ONLY = "facebook_only"
    INSTAGRAM_ONLY = "instagram_only"
    MODERN = "modern"  # disqualifying — filtered out before scoring, see filter_disqualified()


_STATUS_BASE_SCORE = {
    WebsiteStatus.NONE: 100,
    WebsiteStatus.BROKEN: 85,
    WebsiteStatus.FACEBOOK_ONLY: 60,
    WebsiteStatus.INSTAGRAM_ONLY: 55,
    WebsiteStatus.MODERN: 0,
}


def classify_website_status(website_url: str | None, website_reachable: bool | None,
                             facebook_url: str | None, instagram_url: str | None) -> WebsiteStatus:
    """
    website_reachable is the result of an upstream HTTP check (200 + real
    content vs. timeout/parked-domain/dead cert). Pass None if not checked.
    """
    if website_url:
        if website_reachable is False:
            return WebsiteStatus.BROKEN
        if website_reachable is True:
            return WebsiteStatus.MODERN
        # Unknown reachability — treat conservatively as broken so it still
        # surfaces as an opportunity rather than being silently dropped.
        return WebsiteStatus.BROKEN
    if facebook_url:
        return WebsiteStatus.FACEBOOK_ONLY
    if instagram_url:
        return WebsiteStatus.INSTAGRAM_ONLY
    return WebsiteStatus.NONE


def filter_disqualified(status: WebsiteStatus) -> bool:
    """True if this lead should be dropped entirely (has a real modern site)."""
    return status == WebsiteStatus.MODERN


def compute_lead_score(status: WebsiteStatus, google_rating: float | None,
                        review_count: int | None, has_phone: bool, has_email: bool) -> int:
    """
    0-100 score. Website absence/quality dominates (that's the whole premise
    of the outreach), with small boosts for reachability (phone/email present)
    and an established, well-reviewed business being an easier sell.
    """
    score = _STATUS_BASE_SCORE[status]

    # A business with a strong rating and review volume is more credible to
    # pitch (real, active, cares about reputation) — small nudge, capped.
    if google_rating and review_count:
        reputation_bonus = min(10, (google_rating - 3.0) * 3 + min(review_count, 50) / 25)
        score += max(0, reputation_bonus)

    if has_phone:
        score += 3
    if has_email:
        score += 2

    return max(0, min(100, round(score)))
