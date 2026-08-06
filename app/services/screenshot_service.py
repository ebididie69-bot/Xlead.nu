"""
Screenshot generation via Playwright.

Captures the rendered demo page (the actual frontend route, not the raw
JSON) so the screenshot reflects the real template + AI content exactly as
a lead would see it. Saved to local disk in dev; swap SCREENSHOT_DIR for a
Google Drive upload (see services/drive_service.py, next phase) in prod so
files survive serverless cold starts/redeploys.
"""
import os
from playwright.async_api import async_playwright

SCREENSHOT_DIR = os.getenv("SCREENSHOT_DIR", "./screenshots")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

os.makedirs(SCREENSHOT_DIR, exist_ok=True)


async def capture_homepage_screenshot(demo_token: str) -> str:
    url = f"{FRONTEND_URL}/demo/{demo_token}"
    out_path = os.path.join(SCREENSHOT_DIR, f"{demo_token}.png")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.screenshot(path=out_path, full_page=True)
        await browser.close()

    return out_path
