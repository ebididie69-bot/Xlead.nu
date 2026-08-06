"""
Public demo endpoint. NOT behind require_admin — this is what the client
opens in their browser. Security relies entirely on the unguessable token,
not on hiding the endpoint, so:
  - Token must match exactly (no partial/prefix lookups).
  - No enumeration: a missing/expired token returns the same generic 404
    payload as a malformed one — never "closest match" or a list of tokens.
  - Response instructs indexers not to index this page.
"""
from datetime import datetime
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import GeneratedWebsite

router = APIRouter(prefix="/demo", tags=["demo"])

NOINDEX_HEADERS = {"X-Robots-Tag": "noindex, nofollow"}


@router.get("/{token}")
def get_demo_site(token: str, db: Session = Depends(get_db)):
    site = db.query(GeneratedWebsite).filter_by(demo_token=token).first()

    if not site or site.status == "expired" or (site.expiry_date and site.expiry_date < datetime.utcnow()):
        return JSONResponse(
            status_code=404,
            headers=NOINDEX_HEADERS,
            content={
                "error": "not_found",
                "message": "This demo link is invalid or has expired.",
            },
        )

    # Only ever return this one site's data — never anything that could let
    # the frontend discover/list sibling demos.
    return JSONResponse(
        headers=NOINDEX_HEADERS,
        content={
            "template_key": site.template_key,
            "business_name": site.business_name,  # shown ON the page; never in the URL/token itself
            "theme": site.theme,
            "content": site.generated_json,
            "enabled_sections": site.enabled_sections,
            "images": site.images or {},
        },
    )
