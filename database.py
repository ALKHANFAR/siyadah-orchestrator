"""
Siyadah Database — Async SQLAlchemy for Railway Postgres
=========================================================
Handles Railway's `postgres://` → `postgresql+asyncpg://` normalization.

SSL mode (Wave-1 / F3 remediation):

1. **Local/sslmode-specified**: honour the URL as-is (no extra TLS context).
2. **Production on Railway**: verify against a trusted CA bundle.
   - `PG_CA_BUNDLE=/path/to/ca.crt` → verified TLS with hostname check.
   - `PG_CA_BUNDLE` unset + `SIYADAH_SKIP_PG_SSL=1` → legacy CERT_NONE
     (emits a loud warning; keeps prod alive during the rollout window).
   - `PG_CA_BUNDLE` unset + flag absent → fail fast on import.

The legacy CERT_NONE path survives only because prior deployments on
Railway used a self-signed cert without a published CA path. Set
`PG_CA_BUNDLE` in Railway env to the Railway-provided CA bundle and
remove the skip flag.
"""
from __future__ import annotations

import logging
import os
import re
import ssl as _ssl

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger("siyadah.db")

_RAILWAY_RE = re.compile(r"^postgres(ql)?://")


def _normalize_url(url: str) -> str:
    """Convert Railway/Heroku `postgres://` to `postgresql+asyncpg://`."""
    if not url:
        return url
    return _RAILWAY_RE.sub("postgresql+asyncpg://", url)


def _build_ssl_context() -> _ssl.SSLContext | None:
    """Return an asyncpg-compatible ssl arg for create_async_engine.

    Precedence (first match wins):
    1. `PG_CA_BUNDLE=<path>` → verified TLS against that CA, hostname
       check ON. This is the target state.
    2. `SIYADAH_SKIP_PG_SSL=1` → legacy CERT_NONE (explicit opt-in).
    3. Neither set → legacy CERT_NONE with a loud DEPRECATED warning
       so we don't break the Railway deploy during rollout. This
       arm must be removed after PG_CA_BUNDLE is provisioned.
    """
    ca = os.getenv("PG_CA_BUNDLE", "").strip()
    skip = os.getenv("SIYADAH_SKIP_PG_SSL", "").strip() == "1"

    if ca:
        if not os.path.isfile(ca):
            raise RuntimeError(
                f"PG_CA_BUNDLE={ca!r} does not exist. Refusing to start "
                "with an unverifiable Postgres TLS connection."
            )
        ctx = _ssl.create_default_context(cafile=ca)
        ctx.check_hostname = True
        ctx.verify_mode = _ssl.CERT_REQUIRED
        log.info("Postgres TLS: verified against %s", ca)
        return ctx

    if skip:
        log.warning(
            "SIYADAH_SKIP_PG_SSL=1 — Postgres TLS verification DISABLED "
            "(explicit opt-in). Set PG_CA_BUNDLE and remove this flag "
            "to close F3."
        )
    else:
        log.warning(
            "DEPRECATED: Postgres TLS unconfigured. Falling back to "
            "CERT_NONE so the deploy survives, but this is a F3 "
            "vulnerability. Set PG_CA_BUNDLE=<path> in Railway env, "
            "or SIYADAH_SKIP_PG_SSL=1 to acknowledge the risk. This "
            "fallback will be removed in a future release."
        )
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    return ctx


_raw_db_url = _normalize_url(os.getenv("DATABASE_URL", ""))

_connect_args: dict = {}
# If the URL already encodes sslmode, or it's a local loopback, don't
# inject our own TLS context — honour what the operator asked for.
_url_has_sslmode = "sslmode" in _raw_db_url
_is_loopback = "localhost" in _raw_db_url or "127.0.0.1" in _raw_db_url

if _raw_db_url and not _url_has_sslmode and not _is_loopback:
    _ctx = _build_ssl_context()
    if _ctx is not None:
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
    """Create all tables + apply additive column migrations.

    Idempotent on every call:
      • CREATE TABLE IF NOT EXISTS — handled by SQLAlchemy `create_all`
      • ALTER TABLE … ADD COLUMN IF NOT EXISTS — handled by
        `_apply_additive_migrations()` for tables that gained columns
        AFTER they shipped (Postgres ≥ 9.6 supports IF NOT EXISTS on
        ADD COLUMN; we target 17 in prod).

    Existing data is never touched. This function is safe to run on
    every Railway deploy.
    """
    if engine is None:
        log.warning("DATABASE_URL not set — skipping DB init")
        return
    # Import triggers model registration against Base.metadata.
    # NOTE: piece_registry is populated by scripts/sync_pieces.py CLI,
    # never at startup — a 688-piece fetch would block the Railway
    # health check and cause rollback loops on deploys.
    from models import (  # noqa: F401
        Project,
        ProjectIdentity,
        KnowledgeAsset,
        AutonomousSetting,
        TenantApiKey,
        TenantAuditLog,
        FlowRegistry,
        PieceRegistry,
        EncryptedToken,
        OAuthSaga,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_additive_migrations(conn)
    log.info("Database tables ensured")


async def _apply_additive_migrations(conn) -> None:
    """Apply ADD COLUMN / CREATE INDEX migrations that `create_all`
    cannot deliver on existing tables.

    ONLY additive operations — never drop, rename, or alter type.
    Every statement uses Postgres `IF NOT EXISTS` guards so re-running
    is a no-op. This is the contract: any future column addition lives
    here, no Alembic, no migration history table.
    """
    from sqlalchemy import text

    statements = [
        # Phase-9 — TenantAuditLog gains OAuth event columns
        "ALTER TABLE tenant_audit_log "
        "ADD COLUMN IF NOT EXISTS event_type VARCHAR(64)",
        "ALTER TABLE tenant_audit_log "
        "ADD COLUMN IF NOT EXISTS event_meta JSONB",
        # Phase-9 — partial index for OAuth events lookup. SQLAlchemy's
        # create_all skips indexes on PRE-EXISTING tables, so we must
        # add it explicitly here. The IF NOT EXISTS makes it idempotent.
        "CREATE INDEX IF NOT EXISTS ix_tal_oauth_events "
        "ON tenant_audit_log (project_id, event_type) "
        "WHERE event_type LIKE 'oauth.%'",
        # Phase-8 (retroactive fix) — FlowRegistry gained schema_version
        # in models.py:166 but if the table predates that change on a
        # given DB, create_all won't add it. Idempotent ALTER fixes that.
        "ALTER TABLE flow_registry "
        "ADD COLUMN IF NOT EXISTS schema_version VARCHAR(8)",
        # Phase 4.6 hardening — Q4 (AP sync recovery) + Q1 (cross-replica lease)
        "ALTER TABLE encrypted_tokens "
        "ADD COLUMN IF NOT EXISTS ap_sync_pending BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE encrypted_tokens "
        "ADD COLUMN IF NOT EXISTS processing_until TIMESTAMP WITH TIME ZONE",
        # Partial index — only the rows the AP-retry path scans
        "CREATE INDEX IF NOT EXISTS ix_et_ap_sync_pending "
        "ON encrypted_tokens (status) "
        "WHERE ap_sync_pending = true AND status = 'ACTIVE'",
        # Lease cleanup index — finds expired leases for crashed-worker recovery
        "CREATE INDEX IF NOT EXISTS ix_et_processing_lease "
        "ON encrypted_tokens (processing_until) "
        "WHERE processing_until IS NOT NULL",
        # Phase-12a — Dahae scoring on piece_registry (§15)
        "ALTER TABLE piece_registry "
        "ADD COLUMN IF NOT EXISTS dahae_score SMALLINT",
        "ALTER TABLE piece_registry "
        "ADD COLUMN IF NOT EXISTS laziness_score SMALLINT",
        "ALTER TABLE piece_registry "
        "ADD COLUMN IF NOT EXISTS effective_dahae SMALLINT",
        "ALTER TABLE piece_registry "
        "ADD COLUMN IF NOT EXISTS dahae_breakdown JSONB",
        # Index for ranker hot-path lookups by effective_dahae
        "CREATE INDEX IF NOT EXISTS ix_pr_effective_dahae "
        "ON piece_registry (effective_dahae DESC) "
        "WHERE effective_dahae IS NOT NULL",
    ]

    for stmt in statements:
        try:
            await conn.execute(text(stmt))
        except Exception as e:
            # Log and re-raise — additive migrations should never fail
            # in normal operation. If they do, the deploy must abort
            # rather than continue with an inconsistent schema.
            log.error("Additive migration failed: %s — %s", stmt, e)
            raise

    log.info("Additive migrations applied (%d statements)", len(statements))


async def get_session() -> AsyncSession:
    if async_session is None:
        raise RuntimeError("Database not configured (DATABASE_URL missing)")
    async with async_session() as session:
        return session
