"""
Google Drive file storage using the admin's OAuth token.
Used for: lead images, generated website screenshots.
All files stored in a single folder (GOOGLE_DRIVE_FOLDER_ID from Settings).
"""
import json
import httpx
from sqlalchemy.orm import Session

from app.core.security import decrypt, get_setting
from app.models import AdminIdentity


async def upload_file(
    admin: AdminIdentity,
    db: Session,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
) -> str:
    """Upload a file to Drive. Returns a publicly readable URL."""
    access_token = decrypt(admin.access_token_enc)
    if not access_token:
        raise RuntimeError("No valid Google access token — sign out and sign back in.")

    folder_id = get_setting(db, "GOOGLE_DRIVE_FOLDER_ID")
    metadata = {"name": filename}
    if folder_id:
        metadata["parents"] = [folder_id]

    boundary = "leadforge_boundary"
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + file_bytes + f"\r\n--{boundary}--".encode()

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            content=body,
        )
        if resp.status_code == 401:
            raise RuntimeError(
                "Google token expired — sign out and sign back in to refresh Drive access."
            )
        resp.raise_for_status()
        file_id = resp.json()["id"]

        await client.post(
            f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"role": "reader", "type": "anyone"},
        )

    return f"https://drive.google.com/uc?id={file_id}&export=view"
