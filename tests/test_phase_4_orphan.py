"""
Phase 4 — orphan bridge harsh tests.

Covers:
- Idempotent upsert on the same flow_id.
- Cross-tenant attempts return 404 (no existence leak).
- Hijack guard: once a flow_id is bound to tenant A, tenant C cannot
  claim it even with a valid key + matching header.
- Orphan filter returns only unregistered flows.
- GET /v2/flows excludes flows from other tenants (AP list fallback).
- Piece manifest shape is enriched with pieces + mcp_tool_count.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

import models
from conftest import (
    KEY_A, KEY_B, KEY_C, PID_A, PID_B, PID_C, hdr,
)

pytestmark = pytest.mark.asyncio


async def test_register_employee_creates_row(client, db_session):
    r = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A), json={},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True
    assert body["tenant_id"] == PID_A
    assert body["flow_id"] == "flow-A-1"
    # enriched manifest
    assert "piece_manifest" in body
    assert "pieces" in body["piece_manifest"]
    assert body["piece_manifest"]["mcp_tool_count"] >= 1
    assert body["trigger_type"] in ("WEBHOOK", "webhook")


async def test_register_is_idempotent_and_updates(client, db_session):
    await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A), json={},
    )
    # Second call with a new display_name → updates, not duplicates
    r = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A), json={"display_name": "Renamed Bot"},
    )
    assert r.status_code == 200
    assert r.json()["created"] is False
    assert r.json()["display_name"] == "Renamed Bot"

    # Only one row exists for this flow_id
    rows = (await db_session.execute(
        select(models.FlowRegistry)
        .where(models.FlowRegistry.flow_id == "flow-A-1")
    )).scalars().all()
    assert len(rows) == 1


async def test_cross_tenant_register_returns_404(client, db_session):
    """Tenant B tries to register a flow that AP says belongs to A."""
    r = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_B, PID_B), json={},
    )
    assert r.status_code == 404
    # No row should have been written
    rows = (await db_session.execute(
        select(models.FlowRegistry)
        .where(models.FlowRegistry.flow_id == "flow-A-1")
    )).scalars().all()
    assert rows == []


async def test_hijack_after_registration_returns_404(client):
    """Once flow-A-1 is bound to A, C cannot re-associate it."""
    await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A), json={},
    )
    r = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_C, PID_C), json={},
    )
    assert r.status_code == 404


async def test_register_unknown_flow_returns_404(client):
    r = await client.post(
        "/v2/flows/does-not-exist/register-employee",
        headers=hdr(KEY_A, PID_A), json={},
    )
    assert r.status_code == 404


async def test_orphan_filter_returns_only_unregistered(client):
    """AP has 3 flows for A: flow-A-1, flow-A-2, flow-A-orphan.
    Register only flow-A-1 → orphan list contains the other two."""
    await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A), json={},
    )
    r = await client.get("/v2/flows?orphan=true", headers=hdr(KEY_A, PID_A))
    assert r.status_code == 200
    ids = {f["flow_id"] for f in r.json()["flows"]}
    assert "flow-A-1" not in ids
    assert "flow-A-orphan" in ids
    assert "flow-A-2" in ids


async def test_list_flows_excludes_other_tenants(client):
    """Tenant A must not see B's flows even when orphan=false."""
    r = await client.get("/v2/flows", headers=hdr(KEY_A, PID_A))
    ids = {f["flow_id"] for f in r.json()["flows"]}
    assert "flow-B-1" not in ids


async def test_flows_listing_marks_registered_vs_orphan_correctly(client):
    await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A), json={},
    )
    r = await client.get("/v2/flows", headers=hdr(KEY_A, PID_A))
    by_id = {f["flow_id"]: f for f in r.json()["flows"]}
    assert by_id["flow-A-1"]["registered"] is True
    assert by_id["flow-A-1"]["orphan"] is False
    assert by_id["flow-A-orphan"]["registered"] is False
    assert by_id["flow-A-orphan"]["orphan"] is True


async def test_register_rate_limit_fires(client):
    """Endpoint is @limiter.limit('20/minute'). 21st call must 429."""
    statuses = []
    for i in range(22):
        # Use flow-A-1 over and over — idempotent so no DB failure.
        r = await client.post(
            "/v2/flows/flow-A-1/register-employee",
            headers=hdr(KEY_A, PID_A), json={},
        )
        statuses.append(r.status_code)
    assert 429 in statuses
