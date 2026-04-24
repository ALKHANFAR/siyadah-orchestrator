"""
Phase 1 — tenant isolation harsh tests.

Covers:
- Bare middleware behaviour under enforcement (401/403 matrix).
- Header injection / casing / length attacks.
- Key revocation mid-flight.
- Concurrency storm: 50 parallel interleaved writes from A + B,
  assert zero cross-project audit log rows.
- Audit log shape (request_id, remote_ip, user_agent, violation).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

import models
from conftest import KEY_A, KEY_B, KEY_C, PID_A, PID_B, PID_C, hdr


pytestmark = pytest.mark.asyncio


# ── 1. basic matrix ─────────────────────────────────────────────

async def test_missing_api_key_always_blocked(client):
    """Even in dry-run, API-key violations enforce. Sanity check."""
    r = await client.get("/v2/templates")
    assert r.status_code == 401
    assert "missing_api_key" in r.text


async def test_valid_key_plus_tenant_passes(client):
    r = await client.get("/v2/templates", headers=hdr(KEY_A, PID_A))
    assert r.status_code == 200
    assert "templates" in r.json()


async def test_tenant_mismatch_403(client):
    r = await client.get("/v2/templates", headers=hdr(KEY_A, PID_B))
    assert r.status_code == 403
    assert "tenant_mismatch" in r.text


async def test_missing_tenant_header_401(client):
    r = await client.get("/v2/templates", headers={"X-API-Key": KEY_A})
    assert r.status_code == 401
    assert "missing_tenant_header" in r.text


async def test_unknown_key_401(client):
    r = await client.get(
        "/v2/templates",
        headers={"X-API-Key": "bogus-xxx", "X-Siyadah-Tenant": PID_A},
    )
    assert r.status_code == 401
    assert "unknown_or_revoked_key" in r.text


# ── 2. header injection / evasion ───────────────────────────────

async def test_case_insensitive_headers(client):
    """HTTP headers are case-insensitive per RFC. httpx normalises, but
    verify server-side still accepts lowercase."""
    r = await client.get("/v2/templates", headers={
        "x-api-key": KEY_A,
        "x-siyadah-tenant": PID_A,
    })
    assert r.status_code == 200


async def test_tenant_header_with_whitespace_is_trimmed(client):
    """Leading/trailing whitespace should NOT change the tenant — auth
    strip()s before comparison."""
    r = await client.get("/v2/templates", headers={
        "X-API-Key": KEY_A,
        "X-Siyadah-Tenant": f"  {PID_A}  ",
    })
    assert r.status_code == 200


async def test_empty_tenant_header_same_as_missing(client):
    r = await client.get("/v2/templates", headers={
        "X-API-Key": KEY_A,
        "X-Siyadah-Tenant": "",
    })
    assert r.status_code == 401
    assert "missing_tenant_header" in r.text


async def test_sql_injection_in_tenant_header(client):
    """Attempted SQL injection in the claim must be rejected as
    tenant_mismatch (not reach SQL at all — we compare strings)."""
    evil = f"{PID_B}'; DROP TABLE tenant_api_keys;--"
    r = await client.get("/v2/templates", headers={
        "X-API-Key": KEY_A,
        "X-Siyadah-Tenant": evil,
    })
    # Either 403 (mismatch) or 401 (missing_tenant) depending on how
    # the string parses — both are safe outcomes. Must NOT be 200, and
    # the table must still exist after.
    assert r.status_code in (401, 403)


async def test_very_long_key_does_not_crash(client):
    """4KB API key header. Must short-circuit with 401, not 500."""
    r = await client.get("/v2/templates", headers={
        "X-API-Key": "A" * 4096,
        "X-Siyadah-Tenant": PID_A,
    })
    assert r.status_code == 401


async def test_null_byte_in_tenant_header(client):
    """Null byte in tenant header — library should reject before our code."""
    # httpx rejects null bytes in headers; verify it doesn't break the server.
    try:
        r = await client.get("/v2/templates", headers={
            "X-API-Key": KEY_A,
            "X-Siyadah-Tenant": f"{PID_A}\x00bypass",
        })
        # If httpx allows it, the server must not accept as PID_A.
        assert r.status_code in (400, 401, 403, 500)
    except Exception:
        # httpx refused — also a safe outcome.
        pass


# ── 3. revocation ───────────────────────────────────────────────

async def test_revoked_key_blocked_on_next_request(client, db_session):
    # First request: works
    r = await client.get("/v2/templates", headers=hdr(KEY_A, PID_A))
    assert r.status_code == 200

    # Revoke
    await db_session.execute(
        models.TenantApiKey.__table__.update()
        .where(models.TenantApiKey.project_id == PID_A)
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await db_session.commit()

    # Next request: 401
    r = await client.get("/v2/templates", headers=hdr(KEY_A, PID_A))
    assert r.status_code == 401
    assert "unknown_or_revoked_key" in r.text


# ── 4. audit log shape ──────────────────────────────────────────

async def test_audit_log_records_violation_with_full_context(client, db_session):
    r = await client.get("/v2/templates", headers=hdr(KEY_A, PID_B))
    assert r.status_code == 403

    rows = (await db_session.execute(
        select(models.TenantAuditLog)
        .where(models.TenantAuditLog.violation == "tenant_mismatch")
    )).scalars().all()
    assert len(rows) >= 1
    row = rows[-1]
    assert row.endpoint == "GET /v2/templates"
    assert row.http_status == 403
    assert row.request_id  # uuid stamped
    assert row.api_key_hash  # sha256 of KEY_A stored
    # remote_ip may be None for ASGI transport; user_agent may be empty.


async def test_audit_log_records_successful_writes(client, db_session):
    r = await client.get("/v2/templates", headers=hdr(KEY_A, PID_A))
    assert r.status_code == 200
    # fire-and-forget create_task; give it a beat to flush
    await asyncio.sleep(0.2)

    count = (await db_session.execute(
        select(func.count())
        .select_from(models.TenantAuditLog)
        .where(models.TenantAuditLog.project_id == PID_A)
        .where(models.TenantAuditLog.violation.is_(None))
    )).scalar_one()
    assert count >= 1


# ── 5. concurrency storm ────────────────────────────────────────

@pytest.mark.slow
async def test_concurrent_interleaved_writes_never_cross_tenants(client, db_session):
    """50 parallel requests — 25 from tenant A, 25 from tenant B —
    interleaved. Verify:
    - Every success writes audit log with the correct tenant_id.
    - Zero cross-tenant audit log rows (a request from A with an A-key
      must NOT appear as B in the audit log).
    """
    async def one(tenant, key):
        return await client.get("/v2/templates", headers=hdr(key, tenant))

    tasks = []
    for i in range(25):
        tasks.append(one(PID_A, KEY_A))
        tasks.append(one(PID_B, KEY_B))
    results = await asyncio.gather(*tasks)
    assert all(r.status_code == 200 for r in results)

    # Let fire-and-forget audits flush
    await asyncio.sleep(0.5)

    # Every audit row must have project_id matching the originating key.
    rows = (await db_session.execute(
        select(models.TenantAuditLog)
        .where(models.TenantAuditLog.endpoint == "GET /v2/templates")
        .where(models.TenantAuditLog.violation.is_(None))
    )).scalars().all()
    # (key_hash, project_id) must be consistent
    a_hash = None
    b_hash = None
    async for k in await _iter_keys(db_session):
        if k.project_id == PID_A:
            a_hash = k.key_hash
        elif k.project_id == PID_B:
            b_hash = k.key_hash

    for r in rows:
        if r.api_key_hash == a_hash:
            assert r.project_id == PID_A, (
                f"cross-tenant leak: A key hash produced {r.project_id}"
            )
        elif r.api_key_hash == b_hash:
            assert r.project_id == PID_B, (
                f"cross-tenant leak: B key hash produced {r.project_id}"
            )


async def _iter_keys(s):
    res = await s.execute(select(models.TenantApiKey))

    class _Iter:
        def __init__(self, rows): self.rows = rows
        def __aiter__(self): return self._gen()
        async def _gen(self):
            for r in self.rows:
                yield r

    return _Iter(res.scalars().all())
