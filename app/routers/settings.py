from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.security import set_setting, get_all_settings_masked, require_admin, KNOWN_SETTINGS

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingUpdate(BaseModel):
    key: str
    value: str


@router.get("")
def read_settings(db: Session = Depends(get_db), _admin=Depends(require_admin)):
    """
    Returns only {key: {configured: bool}} — never the actual secret values,
    even to the admin's own browser, once saved. Re-entering a key overwrites
    it; there is intentionally no "reveal" endpoint.
    """
    return get_all_settings_masked(db)


@router.put("")
def update_setting(payload: SettingUpdate, db: Session = Depends(get_db), _admin=Depends(require_admin)):
    set_setting(db, payload.key, payload.value)
    return {"ok": True, "key": payload.key}


@router.get("/known-keys")
def known_keys(_admin=Depends(require_admin)):
    return sorted(KNOWN_SETTINGS)
