"""
Phase 6 — flow awareness harsh tests.

Covers:
- GET /v2/flows/{id}/graph returns node+edge shape; cross-tenant 404.
- POST /v2/flows/check-duplicate:
  * kind=none when no match
  * kind=exact when same (trigger, sorted(pieces))
  * kind=near when shared piece but different full set
  * tenant scope: A cannot see B's match even if pieces are identical
- SQL-injection attempt in display_name doesn't touch DB.
"""
from __future__ import annotations

import pytest

from conftest import KEY_A, KEY_B, PID_A, PID_B, hdr


# ── Graph endpoint ──────────────────────────────────────────────

async def test_graph_returns_nodes_and_edges_for_own_flow(client):
    r = await client.get("/v2/flows/flow-A-1/graph",
                         headers=hdr(KEY_A, PID_A))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["flow_id"] == "flow-A-1"
    assert body["trigger_type"] in ("WEBHOOK", "webhook")
    assert isinstance(body["nodes"], list) and len(body["nodes"]) >= 1
    assert isinstance(body["edges"], list)
    # trigger + 1 chained action → 2 nodes, 1 edge
    kinds = [n["kind"] for n in body["nodes"]]
    assert "trigger" in kinds


async def test_graph_cross_tenant_is_404(client):
    """Tenant B asks for A's flow graph — must 404 (no existence leak)."""
    r = await client.get("/v2/flows/flow-A-1/graph",
                         headers=hdr(KEY_B, PID_B))
    assert r.status_code == 404


async def test_graph_missing_flow_is_404(client):
    r = await client.get("/v2/flows/does-not-exist/graph",
                         headers=hdr(KEY_A, PID_A))
    assert r.status_code == 404


async def test_graph_requires_auth(client):
    r = await client.get("/v2/flows/flow-A-1/graph")
    assert r.status_code == 401


# ── check-duplicate endpoint ────────────────────────────────────

async def test_check_duplicate_returns_none_on_empty_registry(client):
    r = await client.post(
        "/v2/flows/check-duplicate",
        headers=hdr(KEY_A, PID_A),
        json={"trigger_type": "WEBHOOK",
              "pieces": ["@activepieces/gmail"]},
    )
    assert r.status_code == 200
    assert r.json() == {"kind": "none"}


async def test_check_duplicate_returns_exact_match_after_register(client):
    # Register flow-A-1 first so flow_registry has a row with
    # trigger=WEBHOOK + pieces=[@webhook, @gmail].
    await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A), json={},
    )
    r = await client.post(
        "/v2/flows/check-duplicate",
        headers=hdr(KEY_A, PID_A),
        json={"trigger_type": "WEBHOOK",
              "pieces": ["@activepieces/webhook", "@activepieces/gmail"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "exact"
    assert body["existing"]["flow_id"] == "flow-A-1"


async def test_check_duplicate_returns_near_match_on_overlap(client):
    await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A), json={},
    )
    # same trigger, pieces share gmail but not webhook — near match.
    r = await client.post(
        "/v2/flows/check-duplicate",
        headers=hdr(KEY_A, PID_A),
        json={"trigger_type": "WEBHOOK",
              "pieces": ["@activepieces/gmail", "@activepieces/sheets"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "near"
    assert body["matches"][0]["flow_id"] == "flow-A-1"


async def test_check_duplicate_different_trigger_is_none(client):
    await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A), json={},
    )
    # pieces identical, trigger differs → kind=none (no cross-trigger match)
    r = await client.post(
        "/v2/flows/check-duplicate",
        headers=hdr(KEY_A, PID_A),
        json={"trigger_type": "SCHEDULE",
              "pieces": ["@activepieces/webhook", "@activepieces/gmail"]},
    )
    assert r.status_code == 200
    assert r.json() == {"kind": "none"}


async def test_check_duplicate_is_tenant_scoped(client):
    """Tenant A's registered flow must NEVER show up when tenant B asks
    the same signature — zero cross-tenant leakage."""
    await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A), json={},
    )
    r = await client.post(
        "/v2/flows/check-duplicate",
        headers=hdr(KEY_B, PID_B),
        json={"trigger_type": "WEBHOOK",
              "pieces": ["@activepieces/webhook", "@activepieces/gmail"]},
    )
    assert r.status_code == 200
    assert r.json() == {"kind": "none"}, (
        f"cross-tenant match leak: {r.json()}"
    )


async def test_check_duplicate_sql_injection_in_display_name(client, db_session):
    """display_name is reflected in the response but must NEVER reach
    the DB as SQL. Attempt an injection and verify:
    - response returns normally
    - tenant_api_keys table is intact afterwards
    """
    from sqlalchemy import select, func
    import models

    evil = "'; DROP TABLE tenant_api_keys; --"
    r = await client.post(
        "/v2/flows/check-duplicate",
        headers=hdr(KEY_A, PID_A),
        json={"trigger_type": "WEBHOOK", "pieces": [],
              "display_name": evil},
    )
    assert r.status_code == 200

    # Table still has rows (conftest seeds 3)
    count = (await db_session.execute(
        select(func.count()).select_from(models.TenantApiKey)
    )).scalar_one()
    assert count >= 3


async def test_check_duplicate_rate_limited(client):
    """60/minute per tenant — 61st call must 429."""
    # Empty-registry fast path: very cheap request, easy to burst.
    body = {"trigger_type": "WEBHOOK", "pieces": ["@x"]}
    got_429 = False
    for _ in range(65):
        r = await client.post("/v2/flows/check-duplicate",
                              headers=hdr(KEY_A, PID_A), json=body)
        if r.status_code == 429:
            got_429 = True
            break
    assert got_429
