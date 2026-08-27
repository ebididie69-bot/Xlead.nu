"""
Database engine and session management.

Supports:
  - SQLite locally (default if DATABASE_URL is unset)
  - Postgres on Render / Railway / Neon (set DATABASE_URL)

Neon requires SSL. Connection strings look like:
  postgresql://user:pass@ep-xxx.region.aws.neon.tech/neondb?sslmode=require
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./leadforge.db")

# Heroku/Railway-style URLs use postgres:// — SQLAlchemy 2 needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

is_sqlite = DATABASE_URL.startswith("sqlite")

if is_sqlite:
    connect_args = {"check_same_thread": False}
    engine = create_engine(DATABASE_URL, connect_args=connect_args)
else:
    # Neon and most managed Postgres require TLS.
    # sslmode=require in the URL is enough for psycopg/libpq; we also pass
    # connect_args so SSL is enforced even if the query string was omitted.
    connect_args = {"sslmode": "require"}
    engine = create_engine(
        DATABASE_URL,
        connect_args=connect_args,
        pool_pre_ping=True,   # drop dead connections after Neon scale-to-zero
        pool_recycle=300,     # recycle before idle timeout surprises us
    )

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
    from app import models  # noqa: F401 — register models on Base.metadata
    Base.metadata.create_all(bind=engine)
