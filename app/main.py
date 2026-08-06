import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.database import init_db
from app.routers import auth, settings, leads, websites, emails, demo

app = FastAPI(
    title="LeadForge AI API",
    description="Single-admin lead generation, AI website, and outreach pipeline.",
    version="1.0.0",
)

# Signs the session cookie that holds admin_id after Google OAuth login.
# SECRET_KEY must be a separate, long random value from MASTER_KEY (core/security.py) —
# don't reuse one secret for two purposes.
#
# same_site: frontend (Vercel) and backend (Railway) live on different domains
# in production, which makes this a cross-site request from the browser's
# perspective. SameSite=Lax cookies are NOT sent on cross-site fetch/XHR calls
# — only on top-level navigation — so login would appear to succeed at the
# OAuth callback but every subsequent API call would look logged-out. Browsers
# also require Secure whenever SameSite=None is used, hence https_only=True.
# Locally (same-origin, http://localhost) Lax is fine and simpler to debug with.
IS_PRODUCTION = os.getenv("ENV", "development") == "production"
app.add_middleware(GZipMiddleware, minimum_size=500)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SECRET_KEY"],
    same_site="none" if IS_PRODUCTION else "lax",
    https_only=IS_PRODUCTION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:5173")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()


app.include_router(auth.router)
app.include_router(settings.router)
app.include_router(leads.router)
app.include_router(websites.router)
app.include_router(emails.router)
app.include_router(demo.router)  # public, unauthenticated


@app.get("/api/health")
def health():
    return {"status": "ok"}
