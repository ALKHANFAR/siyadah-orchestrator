"""
Wave-1 tenant enforcement middleware.

Implements docs/WAVE-1-DESIGN.md §4 — verifies that each /v2/* call carries
a matching (X-API-Key, X-Siyadah-Tenant) pair backed by a tenant_api_keys
row. Attaches the verified project_id to request.state.project_id so
endpoint handlers can stop trusting body.project_id.

Rollout is gated by REQUIRE_TENANT_ENFORCE:
- false (default): dry-run. Violations are logged to tenant_audit_log but
  requests pass through. Lets the BFF deploy its X-Siyadah-Tenant header
  injection without breaking the chat.
- true: violations return 401/403.

Bootstrap mode: if tenant_api_keys is empty AND the legacy
ORCHESTRATOR_API_KEY env matches, the request is accepted without tenant
binding (request.state.project_id = None). This keeps prod alive during
the short window between migration and seeding.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update

log = logging.getLogger("siyadah.auth")


# ─── config (env-driven) ──────────────────────────────────────────────
ENFORCE = os.getenv("REQUIRE_TENANT_ENFORCE", "false").lower() == "true"
LEGACY_API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "")

# Paths that bypass tenant enforcement entirely.
PUBLIC_PATHS = frozenset({
    "/", "/health", "/openapi.json", "/docs", "/redoc",
    "/favicon.ico",
})

# Phase-9 — patterns that bypass require_tenant because they authenticate
# via a different proof of possession:
#   • OAuth callback: state token (HMAC-signed) carries the tenant id
#   • Provider webhooks (Layer 4): JWT/HMAC signature verification
#
# Each handler MUST verify the alternative proof immediately; falling off
# the require_tenant path means there's no other gate.
PUBLIC_PATH_PATTERNS = (
    re.compile(r"^/v2/oauth/[a-z0-9_-]+/callback$"),
    re.compile(r"^/v2/webhooks/[a-z0-9_-]+/[a-z0-9_-]+$"),
)

# Path prefixes that REQUIRE tenant enforcement when ENFORCE=true.
PROTECTED_PREFIXES = ("/v2/",)


def _hash_key(raw: str) -> str:
    """sha256 hex — matches the column definition in tenant_api_keys.key_hash."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _needs_enforcement(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return False
    # Phase-9 — OAuth callbacks + provider webhooks authenticate via
    # state-token HMAC or signature verification, not X-API-Key. The
    # handlers themselves are the gate; bypass require_tenant here.
    if any(p.match(path) for p in PUBLIC_PATH_PATTERNS):
        return False
    return any(path.startswith(p) for p in PROTECTED_PREFIXES)


async def _lookup_key(key_hash: str):
    """Return the TenantApiKey row for this key hash, or None.

    Imported lazily to avoid circular imports at module load: database.py
    must be fully initialised before we query it, and main.py imports this
    module during app construction.
    """
    from database import async_session
    from models import TenantApiKey

    if async_session is None:
        return None
    async with async_session() as s:
        res = await s.execute(
            select(TenantApiKey).where(
                TenantApiKey.key_hash == key_hash,
                TenantApiKey.revoked_at.is_(None),
            )
        )
        return res.scalar_one_or_none()


async def _touch_last_used(key_hash: str) -> None:
    """Best-effort update of last_used_at. Fire-and-forget."""
    try:
        from database import async_session
        from models import TenantApiKey
        from sqlalchemy import func as sql_func

        if async_session is None:
            return
        async with async_session() as s:
            await s.execute(
                update(TenantApiKey)
                .where(TenantApiKey.key_hash == key_hash)
                .values(last_used_at=sql_func.now())
            )
            await s.commit()
    except Exception as exc:  # nosec B110 — never fail the request
        log.debug("touch_last_used failed: %s", exc)


async def _audit(
    request: Request,
    http_status: int,
    violation: Optional[str],
) -> None:
    """Best-effort insert into tenant_audit_log. Never raises.

    Fire-and-forget via asyncio.create_task when the request is not a
    violation (hot path). Violations always await so we don't miss
    evidence during the dry-run window.
    """
    try:
        from database import async_session
        from models import TenantAuditLog

        if async_session is None:
            return
        body_digest = getattr(request.state, "payload_digest", None)
        async with async_session() as s:
            row = TenantAuditLog(
                project_id=getattr(request.state, "project_id", None),
                api_key_hash=getattr(request.state, "api_key_hash", None),
                endpoint=f"{request.method} {request.url.path}",
                http_status=http_status,
                payload_digest=body_digest,
                request_id=getattr(request.state, "request_id", None),
                remote_ip=request.client.host if request.client else None,
                user_agent=(request.headers.get("user-agent", "") or "")[:500],
                violation=violation,
            )
            s.add(row)
            await s.commit()
    except Exception as exc:  # nosec B110
        log.error("audit-log failed: %s", exc)


async def require_tenant(request: Request, call_next):
    """Wave-1 middleware. Must be registered via app.middleware('http').

    Binds request_id (always) and tenant_id (when resolved) onto the
    structlog contextvars so every downstream log line in this request
    carries them. Always cleared in a finally block.
    """
    # Lazy import — logging_config is configured during app startup,
    # but auth.py is imported early.
    try:
        from logging_config import bind_request_context, clear_request_context
    except Exception:  # nosec B110 — fall back to no-op binding
        def bind_request_context(**_kw): return None
        def clear_request_context(): return None

    path = request.url.path

    # Always stamp a request_id so downstream handlers + logs can correlate.
    request.state.request_id = str(uuid.uuid4())
    request.state.project_id = None
    request.state.api_key_hash = None
    request.state.scopes = []

    bind_request_context(request_id=request.state.request_id)

    try:
        if not _needs_enforcement(path):
            return await call_next(request)

        raw_key = request.headers.get("X-API-Key", "") or ""
        claimed_pid = (request.headers.get("X-Siyadah-Tenant", "") or "").strip()

        violation: Optional[str] = None
        resolved = None

        if not raw_key:
            violation = "missing_api_key"
        else:
            key_hash = _hash_key(raw_key)
            request.state.api_key_hash = key_hash

            resolved = await _lookup_key(key_hash)
            if resolved is None:
                # Bootstrap: if no rows at all, trust legacy env-key compare so
                # we don't break prod before the seed migration runs.
                if LEGACY_API_KEY and hmac.compare_digest(
                    raw_key.encode("utf-8"),
                    LEGACY_API_KEY.encode("utf-8"),
                ):
                    log.info(
                        "auth-bootstrap req=%s path=%s — legacy key accepted "
                        "(seed tenant_api_keys to remove this path)",
                        request.state.request_id, path,
                    )
                else:
                    violation = "unknown_or_revoked_key"
            else:
                if not claimed_pid:
                    violation = "missing_tenant_header"
                elif not hmac.compare_digest(
                    resolved.project_id.encode("utf-8"),
                    claimed_pid.encode("utf-8"),
                ):
                    violation = "tenant_mismatch"
                else:
                    request.state.project_id = resolved.project_id
                    request.state.scopes = list(resolved.scopes or [])
                    # Bind tenant onto the log context so downstream
                    # log.info() calls in the route handler inherit it.
                    bind_request_context(tenant_id=resolved.project_id)
                    # fire-and-forget last_used_at update
                    asyncio.create_task(_touch_last_used(resolved.key_hash))

        # Violation classification:
        # - API_KEY violations are ALWAYS enforced (parity with pre-Wave-1).
        # - TENANT violations honour REQUIRE_TENANT_ENFORCE (dry-run default).
        API_KEY_VIOLATIONS = {"missing_api_key", "unknown_or_revoked_key"}
        TENANT_VIOLATIONS = {"missing_tenant_header", "tenant_mismatch"}

        if violation in API_KEY_VIOLATIONS:
            status = 401
            await _audit(request, status, violation)
            log.warning(
                "auth-block req=%s path=%s violation=%s status=%d",
                request.state.request_id, path, violation, status,
            )
            return JSONResponse(
                status_code=status,
                content={
                    "error": violation,
                    "request_id": request.state.request_id,
                },
            )

        if violation in TENANT_VIOLATIONS and ENFORCE:
            status = 401 if violation == "missing_tenant_header" else 403
            await _audit(request, status, violation)
            log.warning(
                "auth-block req=%s path=%s violation=%s status=%d",
                request.state.request_id, path, violation, status,
            )
            return JSONResponse(
                status_code=status,
                content={
                    "error": violation,
                    "request_id": request.state.request_id,
                },
            )

        # Dry-run: tenant violation logged, request passes through.
        if violation in TENANT_VIOLATIONS and not ENFORCE:
            log.warning(
                "DRY-RUN tenant-violation req=%s path=%s violation=%s",
                request.state.request_id, path, violation,
            )
            await _audit(request, 0, violation)
            # fall through to call_next

        response = await call_next(request)

        # Record successful writes (not violations — they're already logged).
        if not violation:
            # Fire-and-forget for the happy path — keep the hot path fast.
            asyncio.create_task(_audit(request, response.status_code, None))

        return response
    finally:
        clear_request_context()
