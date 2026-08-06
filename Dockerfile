# Render's native Python runtime doesn't give apt/root access, which
# Playwright's Chromium needs a handful of system libraries for. Using
# Playwright's own base image sidesteps that entirely — everything Chromium
# needs is already installed and version-matched to the playwright pip package.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render sets $PORT at runtime — do not hardcode 8000 here.
# --proxy-headers trusts Render's forwarded X-Forwarded-Proto header, so
# request.url_for() in auth.py generates https:// URLs for the OAuth
# callback instead of http:// (Render terminates TLS and forwards over
# plain HTTP internally, same as Railway).
CMD uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'
