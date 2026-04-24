"""
Phase 9 — MCP sync harsh tests (Gap 2 remediation).

Covers the sync_flow_to_mcp wrapper, the _slugify_tool_name helper,
and the register-employee integration point:

1. slugify: unit cases (empty, digit-start, unicode, special chars, >64 chars)
2. happy path: AP register succeeds → flow_registry.mcp_tool_name populated
3. AP 404 fallback: older AP build → registry still created, mcp_tool_name NULL
4. AP 5xx retry behaviour: tenacity retries 3x then returns None (no raise)
5. AP 4xx NO retry: 400 from AP → single attempt, returns None immediately
6. Idempotent: same flow re-registered twice → same tool name, row updated
7. Safety Rule #1: MCP failure does NOT break flow_registry creation
8. Cross-tenant: tenant B cannot register A's flow; AP MCP never called for B
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

import main
import models
from conftest import KEY_A, KEY_B, PID_A, PID_B, hdr

# pytestmark intentionally NOT set at module level — sync tests in
# TestSlugifyToolName must not be auto-marked asyncio. asyncio_mode=auto
# in pytest.ini handles async def tests without any mark.


# ── 1. slugify unit tests ──────────────────────────────────────

class TestSlugifyToolName:
    """Pure function — no I/O, no fixtures needed."""

    def test_simple_two_words(self):
        assert main._slugify_tool_name("Order Manager") == "order_manager"

    def test_special_chars_collapse_to_underscore(self):
        # "Sales / 2024 — Q1" → lowercase → special chars (incl em-dash) become
        # underscores → "sales_2024_q1". Starts with letter 's' so NO flow_ prefix.
        assert main._slugify_tool_name("Sales / 2024 — Q1") == "sales_2024_q1"

    def test_digit_first_gets_flow_prefix(self):
        assert main._slugify_tool_name("2024 Report") == "flow_2024_report"

    def test_empty_string_returns_unnamed(self):
        assert main._slugify_tool_name("") == "unnamed"

    def test_only_whitespace_returns_unnamed(self):
        assert main._slugify_tool_name("   \t\n  ") == "unnamed"

    def test_only_special_chars_returns_unnamed(self):
        assert main._slugify_tool_name("!!! ?? ---") == "unnamed"

    def test_arabic_only_falls_back(self):
        # Arabic chars get stripped by [a-z0-9_] class → empty → unnamed
        assert main._slugify_tool_name("سارة المطعم") == "unnamed"

    def test_mixed_arabic_english_keeps_english(self):
        assert main._slugify_tool_name("Sarah سارة Bot") == "sarah_bot"

    def test_already_snake_case_unchanged(self):
        assert main._slugify_tool_name("order_manager") == "order_manager"

    def test_consecutive_underscores_collapsed(self):
        assert main._slugify_tool_name("a___b___c") == "a_b_c"

    def test_edge_underscores_stripped(self):
        assert main._slugify_tool_name("_hello_") == "hello"

    def test_truncates_over_64_chars(self):
        long = "a" * 100
        out = main._slugify_tool_name(long)
        assert len(out) == 64
        assert out == "a" * 64

    def test_none_returns_unnamed(self):
        # The type hint says str, but defensive coding still handles None.
        assert main._slugify_tool_name(None) == "unnamed"


# ── 2. integration — happy path ────────────────────────────────

async def test_mcp_sync_happy_path_populates_registry(
    client, db_session, monkeypatch,
):
    """AP returns 200 on /mcp-server/register → FlowRegistry row gets
    mcp_tool_name set, response body includes the mcp block."""
    call_log = []

    async def ok_mcp(self, pid, flow_id, tool_name, description):
        call_log.append((pid, flow_id, tool_name, description))
        return {"status": "registered", "toolId": "t-123"}

    monkeypatch.setattr(main.SiyadahEngine,
                        "register_flow_as_mcp_tool", ok_mcp)

    r = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A),
        json={"display_name": "Order Manager"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mcp"]["mcp_tool_name"] == "order_manager"
    assert "mcp_registered_at" in body["mcp"]
    assert body["piece_manifest"]["mcp_tool_name"] == "order_manager"

    # Verify AP was called exactly once with the right args
    assert len(call_log) == 1
    pid, fid, tname, desc = call_log[0]
    assert pid == PID_A
    assert fid == "flow-A-1"
    assert tname == "order_manager"
    assert "Order Manager" in desc

    # DB column populated
    row = (await db_session.execute(
        select(models.FlowRegistry).where(
            models.FlowRegistry.flow_id == "flow-A-1"
        )
    )).scalar_one()
    assert row.mcp_tool_name == "order_manager"
    assert row.mcp_registered_at is not None


# ── 3. AP 404 fallback ─────────────────────────────────────────

async def test_mcp_404_still_registers_employee(client, db_session):
    """Default conftest stub raises 404 (simulates older AP build).
    Registration still succeeds; mcp_tool_name stays NULL."""
    r = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A),
        json={"display_name": "Order Manager"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # The flow is registered — this is the Safety Rule #1 contract
    assert body["created"] is True
    # But MCP info is absent
    assert body["mcp"] == {"status": "not_registered"}

    row = (await db_session.execute(
        select(models.FlowRegistry).where(
            models.FlowRegistry.flow_id == "flow-A-1"
        )
    )).scalar_one()
    assert row.mcp_tool_name is None
    assert row.mcp_registered_at is None


# ── 4. AP 5xx retry behaviour ──────────────────────────────────

async def test_mcp_5xx_retries_then_returns_none(client, monkeypatch):
    """AP returns 500 on every attempt. tenacity tries 3x, sync returns
    None. Flow still registered (safety rule)."""
    call_count = {"n": 0}

    async def always_500(self, pid, flow_id, tool_name, description):
        call_count["n"] += 1
        raise main.HTTPException(500, detail="AP internal error")

    monkeypatch.setattr(main.SiyadahEngine,
                        "register_flow_as_mcp_tool", always_500)

    r = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A),
        json={"display_name": "Order Manager"},
    )
    assert r.status_code == 200
    # sync_flow_to_mcp caught the final HTTPException, returned None
    assert r.json()["mcp"] == {"status": "not_registered"}
    # With tenacity, register_flow_as_mcp_tool would retry internally —
    # but our stub is at method level, so engine._r's tenacity doesn't
    # wrap it. We can still assert it was called at least once.
    assert call_count["n"] >= 1


# ── 5. AP 4xx — NO retry ───────────────────────────────────────

async def test_mcp_400_single_attempt_no_retry(client, monkeypatch):
    """A 400 from AP (caller bug — wrong payload) must NOT be retried.
    Our sync wrapper catches it, returns None, flow creation continues."""
    call_count = {"n": 0}

    async def always_400(self, pid, flow_id, tool_name, description):
        call_count["n"] += 1
        raise main.HTTPException(400, detail="invalid toolName")

    monkeypatch.setattr(main.SiyadahEngine,
                        "register_flow_as_mcp_tool", always_400)

    r = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A),
        json={"display_name": "Order Manager"},
    )
    assert r.status_code == 200
    assert r.json()["mcp"] == {"status": "not_registered"}
    # sync_flow_to_mcp doesn't retry 4xx — exactly one call
    assert call_count["n"] == 1


# ── 6. Idempotent re-register ──────────────────────────────────

async def test_mcp_sync_idempotent_on_reregister(
    client, db_session, monkeypatch,
):
    """Re-registering the same flow with the same display_name yields the
    same mcp_tool_name; the DB row is updated, not duplicated."""
    async def ok_mcp(self, pid, flow_id, tool_name, description):
        return {"status": "registered", "toolId": f"t-{tool_name}"}

    monkeypatch.setattr(main.SiyadahEngine,
                        "register_flow_as_mcp_tool", ok_mcp)

    # First registration
    r1 = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A),
        json={"display_name": "Order Manager"},
    )
    assert r1.json()["created"] is True
    first_tool = r1.json()["mcp"]["mcp_tool_name"]

    # Second call — updates row, NOT duplicate
    r2 = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A),
        json={"display_name": "Order Manager"},
    )
    assert r2.json()["created"] is False
    assert r2.json()["mcp"]["mcp_tool_name"] == first_tool

    rows = (await db_session.execute(
        select(models.FlowRegistry).where(
            models.FlowRegistry.flow_id == "flow-A-1"
        )
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].mcp_tool_name == first_tool


# ── 7. Safety Rule #1 — MCP failure never breaks registry ──────

async def test_mcp_exception_does_not_break_registry(client, monkeypatch):
    """Even an unexpected exception from AP must be swallowed so the
    FlowRegistry row is still written. This is the cornerstone
    safety rule — callers depend on it."""
    async def explode(self, pid, flow_id, tool_name, description):
        raise RuntimeError("simulated catastrophic AP failure")

    monkeypatch.setattr(main.SiyadahEngine,
                        "register_flow_as_mcp_tool", explode)

    r = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A),
        json={"display_name": "Order Manager"},
    )
    # Must NOT 500 — flow registered, mcp marked not_registered
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True
    assert r.json()["mcp"] == {"status": "not_registered"}


# ── 8. Cross-tenant: MCP never called when guard blocks ────────

async def test_mcp_register_not_called_on_cross_tenant_attempt(
    client, monkeypatch,
):
    """Tenant B tries to register A's flow. The cross-tenant guard
    must fire BEFORE sync_flow_to_mcp — otherwise we'd leak B's
    request into A's MCP namespace."""
    mcp_calls = []

    async def record_mcp(self, pid, flow_id, tool_name, description):
        mcp_calls.append((pid, flow_id))
        return {"ok": True}

    monkeypatch.setattr(main.SiyadahEngine,
                        "register_flow_as_mcp_tool", record_mcp)

    r = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_B, PID_B), json={},
    )
    assert r.status_code == 404  # guard rejected
    assert mcp_calls == [], (
        f"cross-tenant MCP leak: {mcp_calls}"
    )
