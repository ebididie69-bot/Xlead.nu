"""
Screenshot generation via Playwright.

Captures the rendered demo page. Prefers Google Drive upload when the admin
session + GOOGLE_DRIVE_FOLDER_ID are available so files survive Render redeploys.
Falls back to local SCREENSHOT_DIR otherwise.
"""
import os
from playwright.async_api import async_playwright

SCREENSHOT_DIR = os.getenv("SCREENSHOT_DIR", "./screenshots")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

os.makedirs(SCREENSHOT_DIR, exist_ok=True)


async def capture_homepage_screenshot(demo_token: str, admin=None, db=None) -> str:
    url = f"{FRONTEND_URL}/demo/{demo_token}"
    out_path = os.path.join(SCREENSHOT_DIR, f"{demo_token}.png")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.screenshot(path=out_path, full_page=True)
        await browser.close()

    # Prefer persistent Drive storage when configured
    if admin is not None and db is not None:
        try:
            from app.core.security import get_setting
            from app.services.drive_service import upload_file

            if get_setting(db, "GOOGLE_DRIVE_FOLDER_ID"):
                with open(out_path, "rb") as f:
                    data = f.read()
                return await upload_file(
                    admin, db, data, f"{demo_token}.png", "image/png"
                )
        except Exception:
            # Fall through to local path — screenshot still captured
            pass

    return out_path
