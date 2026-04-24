"""
Phase 8 — SSE tenant binding + dry-run mode harsh tests.

The previous suites covered the HTTP middleware in enforce mode.
This file closes two gaps:

1. SSE session hijack protection:
   - `/v2/mcp/messages/{sid}` must 403 when the caller's verified
     tenant ≠ the session's binding.

2. Dry-run (REQUIRE_TENANT_ENFORCE=false) behaviour:
   - API-key violations still 401 (parity with legacy).
   - Tenant violations produce http_status=0 audit rows but the
     request passes through. Body project_id wins for resolve_pid
     when state.project_id is None.
"""
from __future__ import annotations

import asyncio
import importlib
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

import models
from conftest import KEY_A, KEY_B, PID_A, PID_B, hdr


# ──────────────────────────────────────────────────────────────
# SSE tenant-binding tests
# ──────────────────────────────────────────────────────────────

async def test_sse_message_cross_tenant_403(client):
    """Tenant A opens an SSE session directly via the internal store;
    tenant B POSTs to /v2/mcp/messages/{sid} with a legitimate key.
    The caller-tenant check in mcp_sse.mcp_sse_message must 403."""
    import mcp_sse

    sid = "sess-for-tenant-A"
    # Seed a session manually — bypassing the long-lived /sse handshake
    await mcp_sse._save_session(sid, {
        "session_id": sid,
        "tenant_id": PID_A,
        "created_at": 0,
        "status": "connected",
    })
    mcp_sse._queues[sid] = asyncio.Queue()

    try:
        r = await client.post(
            f"/v2/mcp/messages/{sid}",
            headers=hdr(KEY_B, PID_B),
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        )
        assert r.status_code == 403
        assert "different tenant" in r.text.lower()
    finally:
        await mcp_sse._delete_session(sid)
        mcp_sse._queues.pop(sid, None)


async def test_sse_message_correct_tenant_passes(client):
    """Same session, correct tenant → 200 + queued response."""
    import mcp_sse

    sid = "sess-for-tenant-A-ok"
    await mcp_sse._save_session(sid, {
        "session_id": sid,
        "tenant_id": PID_A,
        "created_at": 0,
        "status": "connected",
    })
    mcp_sse._queues[sid] = asyncio.Queue()

    try:
        r = await client.post(
            f"/v2/mcp/messages/{sid}",
            headers=hdr(KEY_A, PID_A),
            json={"jsonrpc": "2.0", "method": "initialize", "id": 42},
        )
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "accepted"
    finally:
        await mcp_sse._delete_session(sid)
        mcp_sse._queues.pop(sid, None)


async def test_sse_message_missing_session_404(client):
    r = await client.post(
        "/v2/mcp/messages/nonexistent-sid",
        headers=hdr(KEY_A, PID_A),
        json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
    )
    assert r.status_code == 404


# ──────────────────────────────────────────────────────────────
# Dry-run mode tests
#
# These run against a second FastAPI instance re-imported with
# REQUIRE_TENANT_ENFORCE=false. We do NOT share the session-scoped
# `client` fixture — that one is enforced. Instead we build a
# minimal local transport around a freshly reloaded auth module.
# ──────────────────────────────────────────────────────────────

@pytest.fixture
async def dryrun_client():
    # Flip the flag, reload auth so its module-level ENFORCE captures
    # the new value.
    prior = os.environ.get("REQUIRE_TENANT_ENFORCE")
    os.environ["REQUIRE_TENANT_ENFORCE"] = "false"
    import auth as _auth
    importlib.reload(_auth)
    # require_tenant is looked up attribute-style by Starlette's
    # BaseHTTPMiddleware — rebind the app's middleware stack to pick
    # up the reloaded function.
    import main as _main
    # We cannot rewire the already-built middleware stack cleanly,
    # but monkey-patching auth.ENFORCE at module level is enough
    # because require_tenant reads it via module attribute lookup.
    _auth.ENFORCE = False

    transport = ASGITransport(app=_main.app)
    async with AsyncClient(
        transport=transport, base_url="http://testclient", timeout=15.0,
    ) as c:
        yield c

    # restore
    if prior is None:
        os.environ.pop("REQUIRE_TENANT_ENFORCE", None)
    else:
        os.environ["REQUIRE_TENANT_ENFORCE"] = prior
    _auth.ENFORCE = prior == "true"


async def test_dryrun_missing_tenant_header_passes_through(dryrun_client):
    """In dry-run, keyA without X-Siyadah-Tenant should NOT 401 — the
    request passes through. Body project_id fallback is then used."""
    r = await dryrun_client.get("/v2/templates", headers={"X-API-Key": KEY_A})
    assert r.status_code == 200, r.text


async def test_dryrun_tenant_mismatch_passes_through(dryrun_client):
    """In dry-run, a claim that doesn't match the bound project is
    logged but not blocked."""
    r = await dryrun_client.get("/v2/templates",
                                 headers=hdr(KEY_A, "wrong-tenant"))
    assert r.status_code == 200, r.text


async def test_dryrun_missing_api_key_still_401(dryrun_client):
    """Dry-run is ONLY for tenant violations. API-key violations still
    enforce because they were enforced pre-Wave-1 too (parity guarantee)."""
    r = await dryrun_client.get("/v2/templates")
    assert r.status_code == 401
    assert "missing_api_key" in r.text


async def test_dryrun_records_violation_with_status_0(dryrun_client, db_session):
    """Dry-run violation rows carry http_status=0 so the stats tool
    can tell them apart from enforced blocks."""
    # Clear prior audit rows first
    await db_session.execute(delete(models.TenantAuditLog))
    await db_session.commit()

    await dryrun_client.get("/v2/templates",
                             headers=hdr(KEY_A, "spoofed"))
    await asyncio.sleep(0.2)

    rows = (await db_session.execute(
        select(models.TenantAuditLog)
        .where(models.TenantAuditLog.violation == "tenant_mismatch")
    )).scalars().all()
    assert rows, "dry-run violation not recorded"
    assert any(r.http_status == 0 for r in rows), (
        f"expected http_status=0 on dry-run row; got {[r.http_status for r in rows]}"
    )


async def test_dryrun_unknown_key_still_401(dryrun_client):
    """Unknown key = API-key violation = always blocked, even in dry-run."""
    r = await dryrun_client.get(
        "/v2/templates",
        headers={"X-API-Key": "unknown-xxx", "X-Siyadah-Tenant": PID_A},
    )
    assert r.status_code == 401
    assert "unknown_or_revoked_key" in r.text
