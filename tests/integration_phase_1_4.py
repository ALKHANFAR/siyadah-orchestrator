"""
Real integration tests for Wave-1 Phases 1-4.

Hits the actual FastAPI app via httpx.ASGITransport against a real
Postgres (localhost:5432) and real Redis (localhost:6380). No mocks
for the middleware, DB, or rate limiter. The only stubbed edge is
Activepieces (no live AP sandbox available locally), done by
monkey-patching SiyadahEngine.get_flow / list_flows with fakes that
return canned projectId so we can still exercise the cross-tenant
guards in Phase 4.

Run with: python3 tests/integration_phase_1_4.py
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sys
import time

# ── test env (MUST be set before importing main) ──────────────────
TEST_DB = "postgresql+asyncpg://sy:sy@127.0.0.1:5432/siyadah_test"
os.environ["DATABASE_URL"] = TEST_DB
os.environ["REDIS_URL"] = "redis://127.0.0.1:6380/0"
os.environ["REQUIRE_TENANT_ENFORCE"] = "true"      # we want enforcement ON
os.environ["SIYADAH_SKIP_PG_SSL"] = "1"             # local pg has no TLS
os.environ["AP_EMAIL"] = ""                          # skip AP auth in lifespan
os.environ["AP_PASSWORD"] = ""
os.environ["AP_BASE_URL"] = "http://localhost:9999"  # unreachable, not used
os.environ["AP_PROJECT_ID"] = "TEST_DEFAULT_PID"
os.environ["ORCHESTRATOR_API_KEY"] = ""             # force tenant_api_keys path only
os.environ["LOG_LEVEL"] = "INFO"
os.environ["ORCHESTRATOR_ALLOWED_ORIGINS"] = "http://testclient"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Redirect stdout through a Tee so we can assert on JSON log emission
# WITHOUT silencing the terminal feedback.
_captured = io.StringIO()
_orig_stdout = sys.stdout


class _Tee:
    def write(self, b):
        _captured.write(b)
        _orig_stdout.write(b)

    def flush(self):
        _orig_stdout.flush()


# import triggers module-level logging config before we swap stdout, so
# keep stdout intact during the import, THEN swap.
import main  # noqa: E402
import auth  # noqa: E402
import models  # noqa: E402
from database import async_session, engine  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

sys.stdout = _Tee()

from httpx import ASGITransport, AsyncClient  # noqa: E402


PID_A = "tenant-A"
PID_B = "tenant-B"
KEY_A = "raw-key-for-tenant-A-xxxxxxxxxxxxxxxxxxx"
KEY_B = "raw-key-for-tenant-B-yyyyyyyyyyyyyyyyyyy"


def _sha(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()


# ── report / assertion helpers ──────────────────────────────────

_failures: list[str] = []
_passes: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        _passes.append(label)
        _orig_stdout.write(f"  ✓ {label}\n")
    else:
        _failures.append(f"{label}: {detail}")
        _orig_stdout.write(f"  ✗ {label} — {detail}\n")


async def seed_tenants():
    """Create two projects + two api-key rows for A and B."""
    # Projects row uses the Project model: project_id, name (check schema)
    async with async_session() as s:
        # clear any prior rows
        await s.execute(delete(models.FlowRegistry))
        await s.execute(delete(models.TenantAuditLog))
        await s.execute(delete(models.TenantApiKey))
        # project rows may or may not exist; use raw SQL upsert to tolerate schema variance
        await s.execute(
            delete(models.Project).where(models.Project.project_id.in_([PID_A, PID_B]))
        )
        s.add(models.Project(project_id=PID_A, name="Tenant A"))
        s.add(models.Project(project_id=PID_B, name="Tenant B"))
        s.add(models.TenantApiKey(
            project_id=PID_A, key_hash=_sha(KEY_A), label="A-key",
            scopes=["read", "write"],
        ))
        s.add(models.TenantApiKey(
            project_id=PID_B, key_hash=_sha(KEY_B), label="B-key",
            scopes=["read", "write"],
        ))
        await s.commit()


async def run_phase_1_isolation(client: AsyncClient) -> None:
    _orig_stdout.write("\n━━━ Phase 1: tenant isolation ━━━\n")

    # 1. Missing API key → 401 regardless of REQUIRE_TENANT_ENFORCE
    r = await client.get("/v2/templates")
    check("1.1 missing X-API-Key → 401",
          r.status_code == 401,
          f"got {r.status_code}: {r.text[:120]}")

    # 2. Valid key + matching tenant → 200 with body.project_id ignored
    r = await client.get("/v2/templates",
                         headers={"X-API-Key": KEY_A, "X-Siyadah-Tenant": PID_A})
    check("1.2 valid keyA + tenantA → 200",
          r.status_code == 200,
          f"got {r.status_code}: {r.text[:120]}")

    # 3. Valid key A with claim=B → 403 tenant_mismatch (enforced)
    r = await client.get("/v2/templates",
                         headers={"X-API-Key": KEY_A, "X-Siyadah-Tenant": PID_B})
    check("1.3 keyA claiming tenantB → 403 tenant_mismatch",
          r.status_code == 403 and "tenant_mismatch" in r.text,
          f"got {r.status_code}: {r.text[:160]}")

    # 4. Valid key, no tenant header → 401 missing_tenant_header (enforced)
    r = await client.get("/v2/templates", headers={"X-API-Key": KEY_A})
    check("1.4 keyA + no tenant header → 401 missing_tenant_header",
          r.status_code == 401 and "missing_tenant_header" in r.text,
          f"got {r.status_code}: {r.text[:160]}")

    # 5. Unknown key → 401 unknown_or_revoked_key
    r = await client.get("/v2/templates",
                         headers={"X-API-Key": "bogus-key",
                                  "X-Siyadah-Tenant": PID_A})
    check("1.5 unknown key → 401",
          r.status_code == 401 and "unknown_or_revoked_key" in r.text,
          f"got {r.status_code}: {r.text[:160]}")

    # 6. Audit log recorded a tenant_mismatch row
    async with async_session() as s:
        rows = (await s.execute(
            select(models.TenantAuditLog).where(
                models.TenantAuditLog.violation == "tenant_mismatch",
            )
        )).scalars().all()
    check("1.6 audit log captured tenant_mismatch",
          len(rows) >= 1,
          f"found {len(rows)} rows")


async def run_phase_2_rate_limit(client: AsyncClient) -> None:
    _orig_stdout.write("\n━━━ Phase 2: rate limit ━━━\n")

    # /v2/build-and-deploy is @limiter.limit("10/minute") per tenant.
    # Fire 12 requests; first 10 will fail with validation (no engine)
    # but that's fine — we only care that calls 11+ return 429.
    statuses = []
    for i in range(12):
        r = await client.post(
            "/v2/build-and-deploy",
            headers={"X-API-Key": KEY_A, "X-Siyadah-Tenant": PID_A,
                     "Content-Type": "application/json"},
            json={"template": "does-not-exist", "config": {}},
        )
        statuses.append(r.status_code)

    throttled = [i for i, s in enumerate(statuses) if s == 429]
    check("2.1 11th+ request returns 429",
          len(throttled) >= 1 and throttled[0] >= 10,
          f"statuses: {statuses}")

    # Body message includes rate_limit_exceeded
    r = await client.post(
        "/v2/build-and-deploy",
        headers={"X-API-Key": KEY_A, "X-Siyadah-Tenant": PID_A,
                 "Content-Type": "application/json"},
        json={"template": "does-not-exist", "config": {}},
    )
    check("2.2 429 body contains rate_limit_exceeded",
          r.status_code == 429 and "rate_limit_exceeded" in r.text,
          f"got {r.status_code}: {r.text[:160]}")

    # Tenant B in the same window should NOT be throttled
    r = await client.post(
        "/v2/build-and-deploy",
        headers={"X-API-Key": KEY_B, "X-Siyadah-Tenant": PID_B,
                 "Content-Type": "application/json"},
        json={"template": "does-not-exist", "config": {}},
    )
    check("2.3 tenant B unaffected by tenant A's limit",
          r.status_code != 429,
          f"B got {r.status_code} (expected non-429)")


async def run_phase_3_structlog(client: AsyncClient) -> None:
    _orig_stdout.write("\n━━━ Phase 3: structlog JSON ━━━\n")

    # Make a request that will definitely emit logs
    await client.get("/v2/templates",
                     headers={"X-API-Key": KEY_A, "X-Siyadah-Tenant": PID_A})

    captured = _captured.getvalue()
    # find at least one JSON line that looks like a log event
    json_lines = []
    for line in captured.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("event"):
            json_lines.append(obj)

    check("3.1 stdout has ≥1 JSON log line with an 'event' key",
          len(json_lines) >= 1,
          f"found {len(json_lines)} JSON events")

    # At least one should carry request_id from require_tenant
    with_req_id = [o for o in json_lines if "request_id" in o]
    check("3.2 ≥1 JSON line carries request_id (contextvars binding)",
          len(with_req_id) >= 1,
          f"found {len(with_req_id)} with request_id")


def _fake_ap_flow(project_id: str, flow_id: str = "flow-A-42") -> dict:
    return {
        "id": flow_id,
        "projectId": project_id,
        "displayName": "Test Flow",
        "status": "ENABLED",
        "version": {
            "id": "v1",
            "displayName": "Test Flow",
            "trigger": {
                "type": "WEBHOOK",
                "settings": {"pieceName": "@activepieces/webhook"},
                "nextAction": {
                    "type": "PIECE",
                    "settings": {"pieceName": "@activepieces/gmail"},
                },
            },
        },
    }


async def run_phase_4_orphan_bridge(client: AsyncClient) -> None:
    _orig_stdout.write("\n━━━ Phase 4: orphan bridge ━━━\n")

    # Monkey-patch SiyadahEngine to avoid live AP calls.
    async def fake_get_flow(self, fid):
        # Simulate AP: flow-A-42 belongs to tenant A, flow-B-99 to tenant B,
        # everything else 404s.
        if fid == "flow-A-42":
            return _fake_ap_flow(PID_A, "flow-A-42")
        if fid == "flow-B-99":
            return _fake_ap_flow(PID_B, "flow-B-99")
        raise main.HTTPException(404, detail="not found")

    async def fake_list_flows(self, pid):
        if pid == PID_A:
            return [_fake_ap_flow(PID_A, "flow-A-42"),
                    _fake_ap_flow(PID_A, "flow-A-orphan")]
        if pid == PID_B:
            return [_fake_ap_flow(PID_B, "flow-B-99")]
        return []

    main.SiyadahEngine.get_flow = fake_get_flow
    main.SiyadahEngine.list_flows = fake_list_flows

    # Also patch the shared E() factory so it doesn't require auth.
    # The global _engine can just be a real instance with no token.
    if main._engine is None:
        main._engine = main.SiyadahEngine("http://localhost", "stub")

    # 4.1 Tenant A registers its own flow
    r = await client.post(
        "/v2/flows/flow-A-42/register-employee",
        headers={"X-API-Key": KEY_A, "X-Siyadah-Tenant": PID_A,
                 "Content-Type": "application/json"},
        json={},
    )
    check("4.1 tenant A registers flow-A-42 → 200 created",
          r.status_code == 200 and r.json().get("created") is True,
          f"got {r.status_code}: {r.text[:180]}")

    # 4.2 Idempotent: same call again returns 200 with created=False
    r = await client.post(
        "/v2/flows/flow-A-42/register-employee",
        headers={"X-API-Key": KEY_A, "X-Siyadah-Tenant": PID_A,
                 "Content-Type": "application/json"},
        json={},
    )
    check("4.2 idempotent re-register → 200 created=False",
          r.status_code == 200 and r.json().get("created") is False,
          f"got {r.status_code}: {r.text[:180]}")

    # 4.3 Cross-tenant: Tenant B tries to register flow-A-42 → 404
    r = await client.post(
        "/v2/flows/flow-A-42/register-employee",
        headers={"X-API-Key": KEY_B, "X-Siyadah-Tenant": PID_B,
                 "Content-Type": "application/json"},
        json={},
    )
    check("4.3 tenant B registering A's flow → 404 hijack blocked",
          r.status_code == 404,
          f"got {r.status_code}: {r.text[:180]}")

    # 4.4 GET /v2/flows?orphan=true for tenant A → shows flow-A-orphan only
    r = await client.get(
        "/v2/flows?orphan=true",
        headers={"X-API-Key": KEY_A, "X-Siyadah-Tenant": PID_A},
    )
    orphans = r.json().get("flows", [])
    orphan_ids = [f["flow_id"] for f in orphans]
    check("4.4 GET /v2/flows?orphan=true → contains flow-A-orphan",
          r.status_code == 200 and "flow-A-orphan" in orphan_ids and "flow-A-42" not in orphan_ids,
          f"status={r.status_code} ids={orphan_ids}")

    # 4.5 GET /v2/flows (no filter) → flow-A-42 marked registered
    r = await client.get(
        "/v2/flows",
        headers={"X-API-Key": KEY_A, "X-Siyadah-Tenant": PID_A},
    )
    allflows = r.json().get("flows", [])
    registered = {f["flow_id"]: f for f in allflows}
    check("4.5 GET /v2/flows → flow-A-42 shows registered=true",
          registered.get("flow-A-42", {}).get("registered") is True,
          f"got {registered}")


async def main_test() -> int:
    # Drop + recreate all tables under our test DB so the schema matches
    # the latest models.
    from database import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    # Flush the test Redis so rate-limit counters don't carry over
    # from a prior run.
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        await r.flushdb()
        await r.aclose()
        _orig_stdout.write("  · flushed test Redis\n")
    except Exception as exc:
        _orig_stdout.write(f"  · Redis flush skipped: {exc}\n")

    await seed_tenants()

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport,
                            base_url="http://testclient") as client:
        # Trigger lifespan startup
        async with main.app.router.lifespan_context(main.app):
            await run_phase_1_isolation(client)
            await run_phase_2_rate_limit(client)
            await run_phase_3_structlog(client)
            await run_phase_4_orphan_bridge(client)

    _orig_stdout.write("\n━━━ Summary ━━━\n")
    _orig_stdout.write(f"  passed: {len(_passes)}\n")
    _orig_stdout.write(f"  failed: {len(_failures)}\n")
    for f in _failures:
        _orig_stdout.write(f"    • {f}\n")
    return 0 if not _failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main_test()))
