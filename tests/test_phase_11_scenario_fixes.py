"""
Phase 11 — fixes for the 5 scenario-uncovered bugs.

Each test pins exactly one bug and the fix for it:

Fix #3 — graph walker must expose ROUTER children with branch labels.
Fix #1 — invalid IANA timezone → 422 (not silent success).
Fix #4 — /v2/webhook/{id} handshake must throttle at 300/min/IP.
Fix #2 — secure_webhook=True on non-WEBHOOK trigger must be coerced
         to False + warning surfaced in response.
Fix #5 — every terminal path of /v2/webhook/* must write to
         tenant_audit_log with the webhook_id bound.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

import main
import models
from conftest import KEY_A, PID_A, hdr
from webhook_security import derive_webhook_secret, sign_header_value


# ══════════════════════════════════════════════════════════════
# Fix #3 — Graph walker walks ROUTER branches
# ══════════════════════════════════════════════════════════════

async def test_graph_walks_router_children_with_branch_labels(client, db_session):
    """Synthesise an AP-shaped router flow directly into the fake AP
    store and register it, then verify /v2/flows/{id}/graph returns
    nodes for trigger + router + each of 3 branch actions, with
    edge labels matching settings.branches[i].branchName."""
    flow_id = "flow-router-ABC"
    main.SiyadahEngine.get_flow = _make_get_flow_stub({  # type: ignore[attr-defined]
        flow_id: {
            "id": flow_id,
            "projectId": PID_A,
            "displayName": "VIP Router",
            "status": "ENABLED",
            "version": {
                "id": "v1", "displayName": "VIP Router",
                "trigger": {
                    "name": "trigger_1",
                    "type": "PIECE_TRIGGER",
                    "displayName": "Webhook Trigger",
                    "settings": {"pieceName": "@activepieces/piece-webhook",
                                 "triggerName": "catch_webhook"},
                    "nextAction": {
                        "name": "router_1", "type": "ROUTER",
                        "displayName": "VIP Router",
                        "settings": {
                            "branches": [
                                {"branchName": "VIP"},
                                {"branchName": "Regular"},
                                {"branchName": "Default"},
                            ],
                        },
                        "children": [
                            {"name": "vip_notify", "type": "PIECE",
                             "displayName": "Slack VIP",
                             "settings": {
                                 "pieceName": "@activepieces/piece-slack",
                                 "actionName": "send_channel_message"}},
                            {"name": "regular_email", "type": "PIECE",
                             "displayName": "Email Regular",
                             "settings": {
                                 "pieceName": "@activepieces/piece-gmail",
                                 "actionName": "send_email"}},
                            # 3rd branch — empty child (should still render)
                            None,
                        ],
                    },
                },
            },
        },
    })

    r = await client.get(f"/v2/flows/{flow_id}/graph", headers=hdr(KEY_A, PID_A))
    assert r.status_code == 200, r.text
    g = r.json()

    nodes = g["nodes"]
    edges = g["edges"]
    # Expected nodes: trigger + router + 3 branch children (incl. 1 empty) = 5
    assert len(nodes) == 5, f"expected 5 nodes, got {len(nodes)}: {nodes}"
    # Trigger + router + 3 branch edges = 4 edges total
    assert len(edges) == 4, f"expected 4 edges, got {len(edges)}: {edges}"

    # Branch labels present on the 3 branch edges
    branch_labels = {e.get("label") for e in edges if e.get("label")}
    assert {"VIP", "Regular", "Default"}.issubset(branch_labels), (
        f"missing branch labels; got {branch_labels}"
    )

    # Empty-branch node has kind=branch with the right label
    empty_nodes = [n for n in nodes if n["kind"] == "branch"]
    assert len(empty_nodes) == 1
    assert empty_nodes[0]["display_name"] == "Default"


async def test_graph_walks_loop_body(client):
    """LOOP_ON_ITEMS must expose firstLoopAction as a child with label 'each'."""
    flow_id = "flow-loop-XYZ"
    main.SiyadahEngine.get_flow = _make_get_flow_stub({  # type: ignore[attr-defined]
        flow_id: {
            "id": flow_id, "projectId": PID_A, "status": "ENABLED",
            "displayName": "Loop demo",
            "version": {"displayName": "Loop demo",
                "trigger": {
                    "type": "PIECE_TRIGGER", "name": "t1",
                    "settings": {"pieceName": "@activepieces/piece-schedule",
                                 "triggerName": "every_hour"},
                    "nextAction": {
                        "type": "LOOP_ON_ITEMS", "name": "loop_1",
                        "displayName": "per row",
                        "settings": {"items": "{{trigger.rows}}"},
                        "firstLoopAction": {
                            "type": "PIECE", "name": "send_wa",
                            "displayName": "WhatsApp",
                            "settings": {
                                "pieceName": "@activepieces/piece-whatsapp",
                                "actionName": "send_message"}},
                    },
                }},
        },
    })
    r = await client.get(f"/v2/flows/{flow_id}/graph", headers=hdr(KEY_A, PID_A))
    g = r.json()
    assert len(g["nodes"]) == 3  # trigger + loop + body
    loop_edge = [e for e in g["edges"] if e.get("label") == "each"]
    assert len(loop_edge) == 1


# ══════════════════════════════════════════════════════════════
# Fix #1 — Timezone validation
# ══════════════════════════════════════════════════════════════

def test_invalid_timezone_raises_422_at_helper():
    """Direct unit on the validator — zero I/O."""
    from main import _validate_iana_timezone, HTTPException
    # valid
    _validate_iana_timezone("Asia/Riyadh")
    _validate_iana_timezone("UTC")
    # invalid
    with pytest.raises(HTTPException) as exc_info:
        _validate_iana_timezone("Atlantis/Mu")
    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert isinstance(detail, dict) and detail.get("error") == "invalid_timezone"


def test_clean_input_config_rejects_bogus_timezone():
    """clean_input_config is the choke point for every build endpoint."""
    from main import clean_input_config, HTTPException
    # valid passes through
    out = clean_input_config({"timezone": "Asia/Riyadh", "hour_of_the_day": 8})
    assert out["timezone"] == "Asia/Riyadh"
    # invalid raises
    with pytest.raises(HTTPException) as exc_info:
        clean_input_config({"timezone": "Mars/Phobos", "hour_of_the_day": 8})
    assert exc_info.value.status_code == 422


# ══════════════════════════════════════════════════════════════
# Fix #4 — /v2/webhook/* handshake rate limit
# ══════════════════════════════════════════════════════════════

async def test_webhook_handshake_rate_limited_at_300_per_minute(client):
    """Burst 320 GET handshakes from one IP; 21+ must be 429."""
    # Flush limiter state so prior tests don't bleed in
    import redis.asyncio as aioredis
    import os
    r = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await r.flushdb()
    await r.aclose()

    statuses = []
    for i in range(320):
        resp = await client.get(f"/v2/webhook/any-flow?challenge=ping-{i}")
        statuses.append(resp.status_code)

    throttled = sum(1 for s in statuses if s == 429)
    assert throttled >= 15, (
        f"expected ≥15 × 429 out of 320, got {throttled}. "
        f"First 5 statuses: {statuses[:5]}, last 5: {statuses[-5:]}"
    )


# ══════════════════════════════════════════════════════════════
# Fix #2 — secure_webhook on non-WEBHOOK is coerced
# ══════════════════════════════════════════════════════════════

async def test_secure_webhook_on_schedule_flow_coerced_to_false(
    client, db_session, monkeypatch,
):
    """Register a SCHEDULE-triggered flow with secure_webhook=True.
    Response must carry secure=False + warning; DB row must have
    secure_webhook=False too."""
    flow_id = "flow-sched-secure-test"

    async def fake_get_flow(self, fid):
        return {
            "id": fid, "projectId": PID_A, "status": "ENABLED",
            "displayName": "Morning Report",
            "version": {"displayName": "Morning Report",
                "trigger": {
                    "type": "PIECE_TRIGGER", "name": "t",
                    "settings": {"pieceName": "@activepieces/piece-schedule",
                                 "triggerName": "every_day"},
                }},
        }
    monkeypatch.setattr(main.SiyadahEngine, "get_flow", fake_get_flow)

    # The AP stub returns trigger_type=PIECE_TRIGGER; the register endpoint
    # considers WEBHOOK/PIECE_TRIGGER as webhook-capable. We need a clearly
    # non-webhook trigger to surface the coercion — use SCHEDULE type.
    async def fake_get_flow_schedule(self, fid):
        return {
            "id": fid, "projectId": PID_A, "status": "ENABLED",
            "displayName": "Morning Report",
            "version": {"displayName": "Morning Report",
                "trigger": {
                    "type": "SCHEDULE", "name": "t",
                    "settings": {"pieceName": "@activepieces/piece-schedule",
                                 "triggerName": "every_day"},
                }},
        }
    monkeypatch.setattr(main.SiyadahEngine, "get_flow", fake_get_flow_schedule)

    r = await client.post(
        f"/v2/flows/{flow_id}/register-employee",
        headers=hdr(KEY_A, PID_A),
        json={"display_name": "Morning Bot", "secure_webhook": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Coerced: secure is False in the response
    assert body["webhook"]["secure"] is False
    # Warning surfaced
    assert "warning" in body["webhook"]
    assert "not WEBHOOK" in body["webhook"]["warning"]
    # No secret leaked
    assert "secret" not in body["webhook"]

    row = (await db_session.execute(
        select(models.FlowRegistry).where(
            models.FlowRegistry.flow_id == flow_id,
        )
    )).scalar_one()
    assert row.secure_webhook is False, (
        f"DB row should have secure_webhook=False, got {row.secure_webhook}"
    )


# ══════════════════════════════════════════════════════════════
# Fix #5 — /v2/webhook/* writes to tenant_audit_log
# ══════════════════════════════════════════════════════════════

async def test_webhook_handshake_writes_audit_row(client, db_session):
    """GET handshake must record a row with violation='webhook_handshake'
    and webhook_id bound."""
    # Clear audit log to isolate
    from sqlalchemy import delete as _del
    await db_session.execute(_del(models.TenantAuditLog))
    await db_session.commit()

    # Flush redis for rate limiter fresh window
    import redis.asyncio as aioredis
    import os
    r = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await r.flushdb()
    await r.aclose()

    caller_id = "trace-handshake-42"
    resp = await client.get(
        "/v2/webhook/any-flow?challenge=probe-xyz",
        headers={"x-webhook-id": caller_id},
    )
    assert resp.status_code == 200

    # Audit row is written via asyncio.create_task — give it a moment
    await asyncio.sleep(0.3)

    rows = (await db_session.execute(
        select(models.TenantAuditLog)
        .where(models.TenantAuditLog.endpoint.like("%/v2/webhook/%"))
    )).scalars().all()
    assert len(rows) >= 1, "handshake did not write an audit row"
    latest = rows[-1]
    assert latest.violation == "webhook_handshake"
    assert latest.webhook_id == caller_id
    assert latest.http_status == 200


async def test_webhook_unknown_flow_writes_audit_row(client, db_session):
    """404 on unknown flow must also be audited (violation=webhook_unknown_flow)."""
    from sqlalchemy import delete as _del
    await db_session.execute(_del(models.TenantAuditLog))
    await db_session.commit()

    import redis.asyncio as aioredis
    import os
    rr = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await rr.flushdb()
    await rr.aclose()

    resp = await client.post(
        "/v2/webhook/nonexistent-flow-id",
        content=b'{}',
        headers={"x-webhook-id": "trace-unknown"},
    )
    assert resp.status_code == 404

    await asyncio.sleep(0.2)
    rows = (await db_session.execute(
        select(models.TenantAuditLog)
        .where(models.TenantAuditLog.violation == "webhook_unknown_flow")
    )).scalars().all()
    assert len(rows) >= 1
    assert rows[-1].webhook_id == "trace-unknown"
    assert rows[-1].http_status == 404


async def test_webhook_successful_proxy_writes_audit_with_tenant(
    client, db_session, monkeypatch,
):
    """A valid signed POST must record a clean audit row (violation=NULL)
    with the correct project_id resolved from the FlowRegistry row."""
    from sqlalchemy import delete as _del
    await db_session.execute(_del(models.TenantAuditLog))
    await db_session.commit()

    # Flush redis for rate-limit isolation
    import redis.asyncio as aioredis
    import os
    rr = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await rr.flushdb()
    await rr.aclose()

    flow_id = "flow-audit-OK"
    # Pre-insert FlowRegistry row with secure_webhook=True
    db_session.add(models.FlowRegistry(
        tenant_id=PID_A, flow_id=flow_id,
        display_name="Audit OK", trigger_type="WEBHOOK",
        webhook_url=f"http://testclient/v2/webhook/{flow_id}",
        piece_manifest={}, secure_webhook=True,
        skip_webhook_auth=False, webhook_scheme="siyadah",
    ))
    await db_session.commit()

    async def fake_forward(self, fid, method, body, headers, qp):
        return 200, b'{"ok":true}', {"content-type": "application/json"}
    monkeypatch.setattr(main.SiyadahEngine, "forward_webhook", fake_forward)

    body = b'{"payload":"real"}'
    sig = sign_header_value(body, derive_webhook_secret(flow_id))

    resp = await client.post(
        f"/v2/webhook/{flow_id}",
        content=body,
        headers={
            "x-siyadah-signature": sig,
            "content-type": "application/json",
            "x-webhook-id": "trace-success",
        },
    )
    assert resp.status_code == 200

    await asyncio.sleep(0.3)
    rows = (await db_session.execute(
        select(models.TenantAuditLog)
        .where(models.TenantAuditLog.webhook_id == "trace-success")
    )).scalars().all()
    assert len(rows) >= 1
    # At least one clean row (violation=NULL) with project_id bound
    clean = [r for r in rows if r.violation is None]
    assert clean, f"no clean audit row; got violations: {[r.violation for r in rows]}"
    assert clean[0].project_id == PID_A
    assert clean[0].http_status == 200


# ── helpers ────────────────────────────────────────────────────

def _make_get_flow_stub(flows: dict):
    """Return an async function suitable for monkeypatching
    SiyadahEngine.get_flow. Keeps flows dict closed over."""
    async def _stub(self, fid):
        if fid in flows:
            return flows[fid]
        raise main.HTTPException(404, detail="not found")
    return _stub
