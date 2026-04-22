"""
Siyadah Database — Async SQLAlchemy for Railway Postgres
=========================================================
Handles Railway's `postgres://` → `postgresql+asyncpg://` normalization.
"""
from __future__ import annotations

import logging
import os
import re

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger("siyadah.db")

_RAILWAY_RE = re.compile(r"^postgres(ql)?://")


def _normalize_url(url: str) -> str:
    """Convert Railway/Heroku `postgres://` to `postgresql+asyncpg://`."""
    if not url:
        return url
    return _RAILWAY_RE.sub("postgresql+asyncpg://", url)


_raw_db_url = _normalize_url(os.getenv("DATABASE_URL", ""))

# Guard: Railway env templates that were not substituted (e.g. copied raw
# `<RAILWAY_PRIVATE_DOMAIN>` or unrendered `${{Postgres.PGHOST}}` reference)
# produce silent DNS failures. Detect and refuse to connect with a clear log.
if _raw_db_url and (
    "<" in _raw_db_url and ">" in _raw_db_url
    or "${{" in _raw_db_url
    or "RAILWAY_PRIVATE_DOMAIN" in _raw_db_url
):
    log.error(
        "DATABASE_URL contains an unsubstituted template placeholder: %r. "
        "Fix in Railway → Variables using ${{Postgres.PGHOST}} etc., or paste "
        "the real hostname. Falling back to in-memory (no persistence).",
        _raw_db_url,
    )
    _raw_db_url = ""

_connect_args: dict = {}
_skip_ssl = "sslmode" in _raw_db_url or "localhost" in _raw_db_url or "127.0.0.1" in _raw_db_url
if _raw_db_url and not _skip_ssl:
    import ssl as _ssl
    _ctx = _ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = _ssl.CERT_NONE
    _connect_args = {"ssl": _ctx}

DATABASE_URL = re.sub(r"[?&]sslmode=[^&]*", "", _raw_db_url)

engine = (
    create_async_engine(
        DATABASE_URL, echo=False, pool_pre_ping=True, pool_size=5,
        connect_args=_connect_args,
    )
    if DATABASE_URL
    else None
)

async_session: async_sessionmaker[AsyncSession] | None = (
    async_sessionmaker(engine, expire_on_commit=False) if engine else None
)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    """Create all tables (idempotent). Called once at startup."""
    if engine is None:
        log.warning("DATABASE_URL not set — skipping DB init")
        return
    from models import Project, ProjectIdentity, KnowledgeAsset, AutonomousSetting  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("Database tables ensured")


async def get_session() -> AsyncSession:
    if async_session is None:
        raise RuntimeError("Database not configured (DATABASE_URL missing)")
    async with async_session() as session:
        return session
