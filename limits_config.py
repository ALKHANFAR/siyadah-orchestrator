"""
Wave-1 Phase 2 — per-tenant rate limiting with slowapi.

Key function prefers the verified tenant id (set by auth.require_tenant
on request.state.project_id) so one tenant cannot starve another.
Falls back to the remote IP when the caller is unauthenticated —
prevents a flood of 401s from DOSing the service.

Storage:
- REDIS_URL set → Redis-backed (survives restarts, shared across
  replicas). Fails closed if Redis drops — limits library returns
  503 rather than unlimited.
- REDIS_URL unset → in-memory (dev only; warns at import time).

Budgets: applied per-route in main.py. Rationale:
- BUILD endpoints: 10/min/tenant — single-digit human usage, AP
  backend is the real bottleneck.
- READ/meta: 60/min/tenant — chat MCP flow reads schemas on every
  turn, needs headroom.

SSE /v2/mcp/sse is intentionally NOT decorated — long-lived streams
would exhaust the budget on handshake and starve subsequent tool
calls.
"""
from __future__ import annotations

import logging
import os

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

log = logging.getLogger("siyadah.limits")


def tenant_or_ip(request: Request) -> str:
    """Rate-limit key: verified tenant when present, else remote IP.

    The `tenant:` and `ip:` prefixes prevent a spoofed IP claim from
    colliding with a real tenant id.
    """
    pid = getattr(request.state, "project_id", None)
    if pid:
        return f"tenant:{pid}"
    return f"ip:{get_remote_address(request)}"


_REDIS_URL = os.getenv("REDIS_URL", "").strip()
if _REDIS_URL:
    _storage_uri = _REDIS_URL
    _storage_kind = "redis"
else:
    _storage_uri = "memory://"
    _storage_kind = "memory"
    log.warning(
        "REDIS_URL unset — rate limit counters are in-memory only. "
        "Multi-replica deploys will NOT share state (tenant can burst "
        "N× the limit). Set REDIS_URL in prod."
    )

limiter = Limiter(
    key_func=tenant_or_ip,
    storage_uri=_storage_uri,
    strategy="fixed-window",
    default_limits=[],  # opt-in per route
    headers_enabled=True,  # X-RateLimit-* response headers
)

log.info("Rate limiter initialised (storage=%s)", _storage_kind)
