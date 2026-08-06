"""
Database engine and session management.

SQLite is used for simplicity and zero infra cost. For production beyond a
single admin's personal use, swap DATABASE_URL for Postgres (e.g. Supabase /
Neon free tier) since SQLite files do NOT persist on Vercel's serverless
filesystem — see README "Deployment notes".
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./leadforge.db")

# Railway (and Heroku-style) Postgres add-ons hand out a connection string
# starting with "postgres://", which SQLAlchemy 2.0 rejects outright —
# it requires the "postgresql://" scheme. Normalize it here rather than
# requiring every deploy to remember to edit the env var by hand.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency: yields a DB session and guarantees it closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables. Called once at app startup."""
    from app import models  # noqa: F401 (ensures models are registered)
    Base.metadata.create_all(bind=engine)
