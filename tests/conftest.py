"""
Pytest fixtures for the Wave-1 harsh integration suite.

Shared across test_phase_*.py. Designed to run against a REAL local
PostgreSQL + Redis (no mocks for infra) — this is how we catch bugs
that only surface under concurrency, DB constraint violations, and
real network timing.

Assumes (see .github/workflows/ci.yml for CI provisioning):
- PostgreSQL reachable at $TEST_DATABASE_URL (default:
  postgresql+asyncpg://sy:sy@127.0.0.1:5432/siyadah_test)
- Redis  reachable at $TEST_REDIS_URL  (default:
  redis://127.0.0.1:6380/0)
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

# Must run before main is imported so lifespan picks up test config.
# FORCE-override — the shell may already carry a production-looking
# DATABASE_URL/REDIS_URL that would break the suite.
os.environ["DATABASE_URL"] = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://sy:sy@127.0.0.1:5432/siyadah_test",
)
os.environ["REDIS_URL"] = os.getenv(
    "TEST_REDIS_URL", "redis://127.0.0.1:6380/0",
)
os.environ["REQUIRE_TENANT_ENFORCE"] = "true"
os.environ["SIYADAH_SKIP_PG_SSL"] = "1"
os.environ["AP_EMAIL"] = ""
os.environ["AP_PASSWORD"] = ""
os.environ["AP_BASE_URL"] = "http://localhost:9999"
os.environ["AP_PROJECT_ID"] = "TEST_DEFAULT_PID"
os.environ["ORCHESTRATOR_API_KEY"] = ""
os.environ["LOG_LEVEL"] = "INFO"
os.environ["ORCHESTRATOR_ALLOWED_ORIGINS"] = "http://testclient"
# Wipe any pre-existing AP connection hints so stub doesn't hit real net.
for _k in list(os.environ):
    if _k.startswith("AP_MCP_") or _k.startswith("GMAIL_") or _k.startswith("SHEETS_"):
        os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

import main
import models
from database import Base, async_session, engine


# ─── tenant helpers ─────────────────────────────────────────────

PID_A = "tenant-A"
PID_B = "tenant-B"
PID_C = "tenant-C"
KEY_A = "raw-key-A-" + "a" * 40
KEY_B = "raw-key-B-" + "b" * 40
KEY_C = "raw-key-C-" + "c" * 40


def sha(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()


def hdr(key: str, pid: str) -> dict:
    return {"X-API-Key": key, "X-Siyadah-Tenant": pid}


# ─── pytest config ──────────────────────────────────────────────

def pytest_configure(config):
    config.addinivalue_line("markers",
                            "slow: tests that sleep or run heavy concurrency")


@pytest_asyncio.fixture(scope="session")
async def _schema():
    """Drop + recreate all tables once per test session."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    # session teardown — leave DB so we can inspect on failure
    pass


@pytest_asyncio.fixture(autouse=True)
async def _clean_state(_schema):
    """Before every test: flush Redis and wipe all audit/registry/tenant
    rows. Fresh projects + api keys are re-seeded. Each test sees the
    same blank state."""
    # Redis
    try:
        r = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        await r.flushdb()
        await r.aclose()
    except Exception:
        pass

    async with async_session() as s:
        await s.execute(delete(models.FlowRegistry))
        await s.execute(delete(models.TenantAuditLog))
        await s.execute(delete(models.TenantApiKey))
        await s.execute(delete(models.Project).where(
            models.Project.project_id.in_([PID_A, PID_B, PID_C])
        ))
        await s.commit()

    async with async_session() as s:
        s.add(models.Project(project_id=PID_A, name="A"))
        s.add(models.Project(project_id=PID_B, name="B"))
        s.add(models.Project(project_id=PID_C, name="C"))
        s.add(models.TenantApiKey(
            project_id=PID_A, key_hash=sha(KEY_A), label="A-key",
            scopes=["read", "write"],
        ))
        s.add(models.TenantApiKey(
            project_id=PID_B, key_hash=sha(KEY_B), label="B-key",
            scopes=["read", "write"],
        ))
        s.add(models.TenantApiKey(
            project_id=PID_C, key_hash=sha(KEY_C), label="C-key",
            scopes=["read", "write"],
        ))
        await s.commit()
    yield


@pytest_asyncio.fixture
async def app_instance():
    """Yield the app with lifespan context so startup hooks fire."""
    async with main.app.router.lifespan_context(main.app):
        yield main.app


@pytest_asyncio.fixture
async def client(app_instance):
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(
        transport=transport,
        base_url="http://testclient",
        timeout=30.0,
    ) as c:
        yield c


@pytest_asyncio.fixture
async def db_session():
    """Yield an async SQLAlchemy session for inspection in assertions."""
    async with async_session() as s:
        yield s


@pytest_asyncio.fixture(autouse=True)
def _stub_ap_flow_methods(monkeypatch):
    """Replace SiyadahEngine.get_flow / list_flows with deterministic
    fakes so we can exercise Phase-4 cross-tenant guards without a
    live Activepieces sandbox.
    """
    AP_FLOWS = {
        "flow-A-1": {"projectId": PID_A, "displayName": "A1"},
        "flow-A-2": {"projectId": PID_A, "displayName": "A2"},
        "flow-A-orphan": {"projectId": PID_A, "displayName": "A-orph"},
        "flow-B-1": {"projectId": PID_B, "displayName": "B1"},
    }

    async def fake_get_flow(self, fid):
        if fid in AP_FLOWS:
            raw = AP_FLOWS[fid]
            return {
                "id": fid,
                "projectId": raw["projectId"],
                "displayName": raw["displayName"],
                "status": "ENABLED",
                "version": {
                    "id": f"v1-{fid}",
                    "displayName": raw["displayName"],
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
        raise main.HTTPException(404, detail="not found")

    async def fake_list_flows(self, pid):
        return [
            {
                "id": fid,
                "projectId": f["projectId"],
                "displayName": f["displayName"],
                "status": "ENABLED",
                "version": {"displayName": f["displayName"]},
            }
            for fid, f in AP_FLOWS.items()
            if f["projectId"] == pid
        ]

    async def fake_mcp_register(self, pid, flow_id, tool_name, description):
        """Default stub: AP doesn't know the /mcp-server/register endpoint
        (simulates older AP build). sync_flow_to_mcp catches the 404 and
        returns None — flow_registry gets mcp_tool_name = NULL.
        Tests that WANT mcp registration to succeed override this via
        their own monkeypatch."""
        raise main.HTTPException(404, detail="mcp-server/register not implemented")

    async def fake_mcp_unregister(self, pid, tool_name):
        raise main.HTTPException(404, detail="mcp-server/tools not implemented")

    monkeypatch.setattr(main.SiyadahEngine, "get_flow", fake_get_flow)
    monkeypatch.setattr(main.SiyadahEngine, "list_flows", fake_list_flows)
    monkeypatch.setattr(main.SiyadahEngine,
                        "register_flow_as_mcp_tool", fake_mcp_register)
    monkeypatch.setattr(main.SiyadahEngine,
                        "unregister_flow_from_mcp", fake_mcp_unregister)
    # Ensure E() has a non-None engine so route handlers don't crash.
    if main._engine is None:
        main._engine = main.SiyadahEngine("http://localhost", "stub")
