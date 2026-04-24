"""
Real-system scenarios — 4 complex digital-employee flows.

Each scenario exercises a DIFFERENT flow shape end-to-end through the
real orchestrator: real Postgres, real Redis, real FastAPI (via
httpx.ASGITransport), real HMAC, real audit log writes. Only the
Activepieces boundary is stubbed (no live AP instance available in the
sandbox) but the stub mimics AP's response shapes faithfully.

Each scenario is instrumented to surface a NEW problem — something the
65+26+20+… existing unit tests do NOT cover. Findings are printed in
the final report with a "FINDING" tag.

Usage::

    cd /home/user/siyadah-orchestrator
    .venv_test/bin/python tests/scenarios/real_4_flows.py
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path

# Env must be set BEFORE main import
os.environ["DATABASE_URL"] = "postgresql+asyncpg://sy:sy@127.0.0.1:5432/siyadah_test"
os.environ["REDIS_URL"] = "redis://127.0.0.1:6380/0"
os.environ["REQUIRE_TENANT_ENFORCE"] = "true"
os.environ["SIYADAH_SKIP_PG_SSL"] = "1"
os.environ["AP_EMAIL"] = ""
os.environ["AP_PASSWORD"] = ""
os.environ["AP_BASE_URL"] = "http://fake-ap:9999"
os.environ["AP_PROJECT_ID"] = "unused-default"
os.environ["ORCHESTRATOR_API_KEY"] = ""
os.environ["ORCHESTRATOR_ALLOWED_ORIGINS"] = "http://testclient"
os.environ["ORCHESTRATOR_PUBLIC_URL"] = "http://testclient"
os.environ["WEBHOOK_SIGNING_MASTER_KEY"] = "real-scenarios-master-key-xyz789"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import hashlib as _h
import hmac as _hmac

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

import main
import models
from database import Base, async_session, engine
from webhook_security import derive_webhook_secret, sign_header_value


# ── setup helpers ────────────────────────────────────────────

def _sha256(x: str) -> str:
    return _h.sha256(x.encode()).hexdigest()


TENANTS = {
    "A": {"pid": "tenant-shawarma", "key": "key-A-" + "a" * 40},
    "B": {"pid": "tenant-cairo-tours", "key": "key-B-" + "b" * 40},
    "C": {"pid": "tenant-realestate", "key": "key-C-" + "c" * 40},
}


FAKE_AP_FLOWS = {}  # flow_id -> fake AP flow dict


async def reset_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as s:
        for t in TENANTS.values():
            s.add(models.Project(project_id=t["pid"], name=t["pid"]))
            s.add(models.TenantApiKey(
                project_id=t["pid"], key_hash=_sha256(t["key"]),
                label=f"{t['pid']}-key", scopes=["read", "write"],
            ))
        await s.commit()


async def flush_redis():
    import redis.asyncio as aioredis
    r = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await r.flushdb()
    await r.aclose()


def install_ap_stubs():
    """Install stubs on SiyadahEngine to simulate real AP behaviour
    without needing a live AP instance. The stubs ARE realistic:
    - golden_build returns flow_id + webhook_url as if AP had deployed
    - get_flow returns the flow structure we recorded at build time
    - list_connections: simulate ACTIVE conns for all required pieces
    - list_pieces + get_piece_schema: return minimal piece metadata
    - register_flow_as_mcp_tool simulates success
    - forward_webhook returns 200 (mimics AP accepting)"""
    import uuid

    async def fake_list_connections(self, pid):
        # Simulate every piece having an ACTIVE connection so guard
        # passes. Matches DEFAULT_CONNECTIONS short names.
        return [
            {"externalId": f"{p}_conn", "id": f"{p}_conn_id",
             "displayName": f"{p} conn", "pieceName": f"@activepieces/piece-{p}",
             "status": "ACTIVE"}
            for p in ("gmail", "google-sheets", "webhook", "schedule",
                       "slack", "whatsapp")
        ]

    async def fake_list_pieces(self):
        return [
            {"name": f"@activepieces/piece-{p}", "displayName": p,
             "version": "0.1.0",
             "actions": {"send_email": {}, "append_row": {}, "send_message": {}},
             "triggers": {"catch_webhook": {}, "every_day": {},
                          "every_hour": {}}}
            for p in ("gmail", "google-sheets", "webhook", "schedule",
                       "slack", "whatsapp")
        ]

    # This is the method fetch_piece_schema() calls on engine
    async def fake_get_piece(self, piece_name, ver=None):
        return {
            "name": piece_name, "version": "0.1.0",
            "actions": {
                "send_email": {"props": {"to": {"type": "SHORT_TEXT"}}},
                "append_row": {"props": {"values": {"type": "OBJECT"}}},
                "send_channel_message": {"props": {"channel": {"type": "SHORT_TEXT"}}},
                "send_message": {"props": {"to": {"type": "SHORT_TEXT"}}},
                "search_mail": {"props": {"query": {"type": "SHORT_TEXT"}}},
            },
            "triggers": {
                "catch_webhook": {"props": {"authType": {"type": "STATIC_DROPDOWN"}}},
                "every_day": {"props": {
                    "hour_of_the_day": {"type": "NUMBER"},
                    "timezone": {"type": "SHORT_TEXT"},
                    "run_on_weekends": {"type": "BOOLEAN"}}},
                "every_hour": {"props": {"timezone": {"type": "SHORT_TEXT"}}},
            },
            "auth": {"type": "NONE"},
        }

    _golden_orig = main.golden_build

    async def fake_get_flow(self, fid):
        if fid in FAKE_AP_FLOWS:
            return FAKE_AP_FLOWS[fid]
        raise main.HTTPException(404, detail="flow_not_found")

    async def fake_golden_build(engine, pid, name, trigger):
        """Record the flow in FAKE_AP_FLOWS so get_flow can retrieve it."""
        fid = f"flow-{uuid.uuid4().hex[:12]}"
        trig_type = (trigger or {}).get("type") or "unknown"
        FAKE_AP_FLOWS[fid] = {
            "id": fid,
            "projectId": pid,
            "displayName": name,
            "status": "ENABLED",
            "version": {
                "id": f"v-{fid}",
                "displayName": name,
                "trigger": trigger,
            },
        }
        # Return the same shape real golden_build would
        return {
            "flow_id": fid,
            "trigger_type": trig_type,
            "publish": {"status": "ENABLED"},
            "diagnosis": {"ok": True},
            "webhook_url": (
                f"http://fake-ap:9999/api/v1/webhooks/{fid}"
                if trig_type in ("PIECE_TRIGGER",) else None
            ),
            "resource_link": None, "pulse_sent": False,
            "client_email": None,
        }

    async def fake_list_flows(self, pid):
        return [f for f in FAKE_AP_FLOWS.values() if f["projectId"] == pid]

    async def fake_mcp_register(self, pid, flow_id, tool_name, description):
        return {"status": "registered", "toolId": f"tool-{tool_name}"}

    forward_log = []

    async def fake_forward_webhook(self, flow_id, method, body, headers, qp):
        forward_log.append({
            "flow_id": flow_id, "method": method,
            "body_size": len(body),
            "x_webhook_id": headers.get("x-forwarded-webhook-id"),
            "content_type": headers.get("content-type"),
        })
        return 200, b'{"accepted":true}', {"content-type": "application/json"}

    # Inject
    main.golden_build = fake_golden_build
    main.SiyadahEngine.get_flow = fake_get_flow
    main.SiyadahEngine.list_flows = fake_list_flows
    main.SiyadahEngine.list_connections = fake_list_connections
    main.SiyadahEngine.list_pieces = fake_list_pieces
    main.SiyadahEngine.get_piece = fake_get_piece
    main.SiyadahEngine.register_flow_as_mcp_tool = fake_mcp_register
    main.SiyadahEngine.forward_webhook = fake_forward_webhook

    if main._engine is None:
        main._engine = main.SiyadahEngine("http://fake-ap", "stub-token")

    return forward_log, _golden_orig


# ── reporting ───────────────────────────────────────────────

OUT = sys.stdout


def say(msg=""):
    OUT.write(msg + "\n")
    OUT.flush()


findings: list[str] = []


def finding(text: str):
    findings.append(text)
    say(f"  🔴 FINDING → {text}")


def ok(text: str):
    say(f"  ✅ {text}")


def hdr(t):
    return {"X-API-Key": t["key"], "X-Siyadah-Tenant": t["pid"]}


# ══════════════════════════════════════════════════════════════
# Scenario 1: Sarah — WhatsApp Order Taker (Webhook + HMAC)
# ══════════════════════════════════════════════════════════════

async def scenario_1(client):
    say("\n" + "═" * 70)
    say(" Scenario 1 — Sarah WhatsApp Orders (Webhook + HMAC + Replay)")
    say("═" * 70)
    t = TENANTS["A"]

    # Step 1: Build via template /v2/build-and-deploy
    r = await client.post(
        "/v2/build-and-deploy", headers=hdr(t),
        json={
            "template": "webhook_to_sheet_and_email",
            "config": {
                "spreadsheet_id": "SHEET_SARAH_ORDERS",
                "recipient_email": "chef@shawarma.sa",
            },
            "display_name": "Sarah — WhatsApp Orders",
        },
    )
    if r.status_code != 200:
        if r.status_code == 422 and "Missing connections" in r.text:
            ok("S1 expected: 422 Missing connections — connection guard "
               "working as designed (Gap 4 will give OAuth UX)")
        else:
            say(f"  ⚠ build returned {r.status_code}: {r.text[:200]}")
            finding(f"S1: unexpected build failure — {r.text[:180]}")
        return
    build_body = r.json()
    flow_id = build_body["flow_id"]
    ok(f"built flow_id={flow_id} template=webhook_to_sheet_and_email")

    # Step 2: Register as digital employee with secure_webhook=true
    r = await client.post(
        f"/v2/flows/{flow_id}/register-employee", headers=hdr(t),
        json={
            "display_name": "Sarah",
            "secure_webhook": True,
            "webhook_scheme": "siyadah",
        },
    )
    if r.status_code != 200:
        say(f"  ⚠ register returned {r.status_code}: {r.text[:200]}")
        return
    reg = r.json()
    webhook_secret = reg["webhook"].get("secret")
    webhook_url = reg["webhook"]["url"]
    ok(f"registered: mcp_tool={reg['mcp'].get('mcp_tool_name')!r}  secure={reg['webhook']['secure']}")
    ok(f"webhook proxy url: {webhook_url}")
    ok(f"secret derived len={len(webhook_secret) if webhook_secret else 0}")

    # Step 3: Send a real external webhook with valid HMAC
    payload = (
        '{"from":"+966501234567","message":"شاورما دجاج مع بطاطس",'
        '"timestamp":1714046400}'
    ).encode("utf-8")
    header = sign_header_value(payload, webhook_secret)
    r = await client.post(
        f"/v2/webhook/{flow_id}",
        content=payload,
        headers={
            "x-siyadah-signature": header,
            "content-type": "application/json",
            "x-webhook-id": "whatsapp-event-001",
        },
    )
    ok(f"1st POST → {r.status_code} (expected 200) · response_x-webhook-id={r.headers.get('x-webhook-id')}")

    # Step 4: REPLAY — send the EXACT same payload + signature again
    r2 = await client.post(
        f"/v2/webhook/{flow_id}",
        content=payload,
        headers={
            "x-siyadah-signature": header,
            "content-type": "application/json",
            "x-webhook-id": "whatsapp-event-001",  # SAME event id
        },
    )
    say(f"  replay POST (identical payload + sig + webhook-id) → {r2.status_code}")
    if r2.status_code == 200:
        finding(
            "S1 REPLAY ATTACK: Same payload + same signature was ACCEPTED TWICE. "
            "Need nonce/timestamp window (HMAC alone doesn't prevent replay). "
            "Suggested: store x-webhook-id in a short-TTL Redis set; reject seen ids; "
            "OR require timestamp header within ±5min window signed with body."
        )

    # Step 5: check the audit log — every request should be traceable
    async with async_session() as s:
        audit_rows = (await s.execute(
            select(models.TenantAuditLog)
            .where(models.TenantAuditLog.endpoint.like("%/v2/webhook/%"))
        )).scalars().all()
    say(f"  audit rows for /v2/webhook/*: {len(audit_rows)}")
    if len(audit_rows) == 0:
        finding(
            "S1 AUDIT GAP: /v2/webhook/{id} endpoint does NOT write to "
            "tenant_audit_log. External webhook executions are untraceable "
            "through our standard audit query. "
            "Fix: call _audit(request, status, None) explicitly in v2_webhook_receiver."
        )


# ══════════════════════════════════════════════════════════════
# Scenario 2: Cairo Tours — Morning Sales Report (Schedule + TZ)
# ══════════════════════════════════════════════════════════════

async def scenario_2(client):
    say("\n" + "═" * 70)
    say(" Scenario 2 — Morning Sales Report (Schedule + Timezone validation)")
    say("═" * 70)
    t = TENANTS["B"]

    # Build a scheduled flow via /v2/build-dynamic with a bogus timezone
    evil_tz = "Atlantis/Mu"  # not a real tz
    r = await client.post(
        "/v2/build-dynamic", headers=hdr(t),
        json={
            "display_name": "Morning Sales Report",
            "trigger": {
                "piece": "@activepieces/piece-schedule",
                "trigger_name": "every_day",
                "input": {
                    "hour_of_the_day": 8,
                    "timezone": evil_tz,    # ← should be rejected
                    "run_on_weekends": False,
                },
            },
            "actions": [
                {
                    "piece": "@activepieces/piece-gmail",
                    "action_name": "search_mail",
                    "input": {"query": "newer_than:1d subject:booking"},
                },
            ],
        },
    )
    if r.status_code != 200:
        say(f"  build-dynamic returned {r.status_code}: {r.text[:180]}")
        ok("bogus timezone REJECTED at build time — good")
        return
    build_body = r.json()
    flow_id = build_body.get("flow_id")
    ok(f"built flow_id={flow_id}")
    finding(
        f"S2 TIMEZONE VALIDATION: accepted timezone={evil_tz!r} without validation. "
        "AP may accept this and the schedule will NEVER fire, silently. "
        "Fix: validate timezone against zoneinfo.available_timezones() "
        "before passing to AP (reject at /v2/build-dynamic with 422)."
    )

    # Register as employee — secure_webhook on a SCHEDULE flow is meaningless
    r = await client.post(
        f"/v2/flows/{flow_id}/register-employee", headers=hdr(t),
        json={
            "display_name": "Morning Reporter",
            "secure_webhook": True,  # ← nonsensical for SCHEDULE
        },
    )
    reg = r.json()
    if reg.get("webhook", {}).get("secure") is True:
        finding(
            "S2 STATE MISMATCH: secure_webhook=True accepted on a SCHEDULE-triggered "
            "flow (no webhook endpoint exists for it). FlowRegistry row has "
            "misleading secure_webhook=True. "
            "Fix: in register-employee, if trigger_type != WEBHOOK, force "
            "secure_webhook=False and return a warning in response.webhook."
        )
    else:
        ok("secure_webhook was correctly downgraded for SCHEDULE flow")

    # Query the graph — does it handle schedule trigger?
    r = await client.get(f"/v2/flows/{flow_id}/graph", headers=hdr(t))
    if r.status_code == 200:
        g = r.json()
        ok(f"graph: trigger_type={g.get('trigger_type')}  nodes={len(g.get('nodes', []))}")
    else:
        finding(f"S2 GRAPH: schedule flow graph returned {r.status_code}")


# ══════════════════════════════════════════════════════════════
# Scenario 3: VIP Lead Router (deep branches + _flow_to_graph)
# ══════════════════════════════════════════════════════════════

async def scenario_3(client):
    say("\n" + "═" * 70)
    say(" Scenario 3 — VIP Lead Router (5 branches + deep recursion)")
    say("═" * 70)
    t = TENANTS["C"]

    # Build-complex with ROUTER + 5 branches (steps shape per main.py)
    steps = [{
        "type": "ROUTER",
        "name": "vip_decision",
        "branches": [
            {"name": "budget>500k",
             "condition": {"operator": "NUMBER_IS_GREATER_THAN",
                           "firstValue": "{{trigger.budget}}",
                           "secondValue": "500000"},
             "action": {"piece_name": "@activepieces/piece-slack",
                        "action_name": "send_channel_message",
                        "input_config": {"channel": "#vip-leads"}}},
            {"name": "budget>100k",
             "condition": {"operator": "NUMBER_IS_GREATER_THAN",
                           "firstValue": "{{trigger.budget}}",
                           "secondValue": "100000"},
             "action": {"piece_name": "@activepieces/piece-gmail",
                        "action_name": "send_email",
                        "input_config": {"to": "sales@realestate.sa"}}},
            {"name": "investor_kw",
             "condition": {"operator": "TEXT_CONTAINS",
                           "firstValue": "{{trigger.message}}",
                           "secondValue": "مستثمر"},
             "action": {"piece_name": "@activepieces/piece-slack",
                        "action_name": "send_channel_message",
                        "input_config": {"channel": "#vip-leads"}}},
            {"name": "known_agent",
             "condition": {"operator": "TEXT_CONTAINS",
                           "firstValue": "{{trigger.source}}",
                           "secondValue": "agent-"},
             "action": {"piece_name": "@activepieces/piece-slack",
                        "action_name": "send_channel_message",
                        "input_config": {"channel": "#agents"}}},
            {"name": "default",
             "condition": None,
             "action": {"piece_name": "@activepieces/piece-gmail",
                        "action_name": "send_email",
                        "input_config": {"to": "leads@realestate.sa"}}},
        ],
    }]
    r = await client.post(
        "/v2/build-complex", headers=hdr(t),
        json={
            "display_name": "VIP Lead Router",
            "steps": steps,
        },
    )
    if r.status_code != 200:
        say(f"  build-complex returned {r.status_code}: {r.text[:200]}")
        # Not a finding — user's complex body shape might not match
        return
    flow_id = r.json().get("flow_id")
    ok(f"built router flow_id={flow_id}")

    # Inspect the stored flow
    stored = FAKE_AP_FLOWS.get(flow_id, {})
    stored_trigger = (stored.get("version") or {}).get("trigger") or {}
    # Check how many branches are inside the trigger.nextAction (router)
    next_action = stored_trigger.get("nextAction") or {}
    branches = next_action.get("branches") or next_action.get("children") or []
    ok(f"router branches actually built: {len(branches)}")
    if len(branches) < 5:
        finding(
            f"S3 ROUTER LOSS: requested 5 branches, only {len(branches)} reached AP. "
            "build-complex may drop branches silently. "
            "Fix: add branch-count assertion in golden_build post-verify step."
        )

    # Register + graph
    r = await client.post(
        f"/v2/flows/{flow_id}/register-employee", headers=hdr(t), json={},
    )
    r = await client.get(f"/v2/flows/{flow_id}/graph", headers=hdr(t))
    if r.status_code == 200:
        g = r.json()
        ok(f"graph: {len(g['nodes'])} nodes, {len(g['edges'])} edges")
    else:
        finding(f"S3 GRAPH on router: {r.status_code} {r.text[:120]}")

    # Probe check-duplicate with the same signature
    r = await client.post(
        "/v2/flows/check-duplicate", headers=hdr(t),
        json={
            "trigger_type": "PIECE_TRIGGER",  # what build emitted
            "pieces": ["@activepieces/piece-webhook", "@activepieces/piece-slack",
                       "@activepieces/piece-gmail"],
        },
    )
    ok(f"check-duplicate on identical signature: kind={r.json().get('kind')}")


# ══════════════════════════════════════════════════════════════
# Scenario 4: Batch Payment Reminders (Schedule + Loop + edge)
# ══════════════════════════════════════════════════════════════

async def scenario_4(client):
    say("\n" + "═" * 70)
    say(" Scenario 4 — Batch Payment Reminders (Schedule + Loop + state)")
    say("═" * 70)
    t = TENANTS["A"]

    # Build a loop flow (correct shape per LoopBuildBody)
    r = await client.post(
        "/v2/build-loop", headers=hdr(t),
        json={
            "display_name": "Payment Reminder Loop",
            "items_expression": "{{read_sheet.output.unpaid_rows}}",
            "loop_actions": [{
                "piece_name": "@activepieces/piece-whatsapp",
                "action_name": "send_message",
                "input_config": {"to": "{{item.phone}}",
                                 "message": "تذكير بالدفعة"},
            }],
        },
    )
    if r.status_code != 200:
        say(f"  build-loop returned {r.status_code}: {r.text[:180]}")
        return
    flow_id = r.json().get("flow_id")
    ok(f"built loop flow_id={flow_id}")

    # Register the flow so it lands in FlowRegistry (default secure=False)
    await client.post(
        f"/v2/flows/{flow_id}/register-employee",
        headers=hdr(t), json={"display_name": "Reminder Loop"},
    )
    # Now /v2/webhook/{flow_id} should return 410 (registered but not
    # secure_webhook=True — wrong URL for this flow).
    r = await client.post(f"/v2/webhook/{flow_id}", content=b'{}')
    if r.status_code == 410:
        ok(f"/v2/webhook/{flow_id} on non-secure flow → 410 ✓")
    else:
        finding(
            f"S4 WRONG-TRIGGER PROXY: /v2/webhook/{flow_id} for a registered "
            f"non-secure flow returned {r.status_code} instead of 410."
        )

    # Register-employee idempotency under concurrent double-submit
    async def double_register():
        return await asyncio.gather(
            client.post(f"/v2/flows/{flow_id}/register-employee",
                        headers=hdr(t), json={"display_name": "Reminder-v1"}),
            client.post(f"/v2/flows/{flow_id}/register-employee",
                        headers=hdr(t), json={"display_name": "Reminder-v2"}),
        )
    r1, r2 = await double_register()
    codes = (r1.status_code, r2.status_code)
    ok(f"concurrent double-register → {codes}")
    if sorted(codes) != [200, 200]:
        finding(
            f"S4 CONCURRENCY: concurrent register-employee returned {codes}. "
            "Expected both to succeed (idempotent upsert)."
        )
    # Which display_name won?
    async with async_session() as s:
        row = (await s.execute(
            select(models.FlowRegistry).where(
                models.FlowRegistry.flow_id == flow_id,
            )
        )).scalar_one_or_none()
    if row:
        ok(f"winning display_name: {row.display_name!r}  (last-write-wins)")

    # Check rate-limit on /v2/webhook/* — burst 350 calls (limit is 300/min).
    # Flush redis window first so prior tests don't poison the counter.
    import redis.asyncio as aioredis
    _rr = aioredis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await _rr.flushdb()
    await _rr.aclose()

    statuses = []
    for i in range(350):
        resp = await client.get(f"/v2/webhook/{flow_id}?challenge=ping-{i}")
        statuses.append(resp.status_code)
    throttled = sum(1 for s in statuses if s == 429)
    ok(f"350 handshake GETs in burst → 429 count: {throttled} "
       f"(expected >=40 at 300/minute limit)")
    if throttled < 10:
        finding(
            "S4 DOS VECTOR: /v2/webhook/{id} handshake endpoint is NOT "
            "throttling — only %d × 429 out of 350. Rate limit may be "
            "missing or mis-configured." % throttled
        )


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════

async def main_run():
    await reset_db()
    await flush_redis()
    forward_log, _ = install_ap_stubs()

    transport = ASGITransport(app=main.app)
    async with AsyncClient(
        transport=transport, base_url="http://testclient", timeout=30.0,
    ) as client:
        async with main.app.router.lifespan_context(main.app):
            await scenario_1(client)
            await scenario_2(client)
            await scenario_3(client)
            await scenario_4(client)

    say("\n" + "═" * 70)
    say(" REPORT — new problems surfaced by the 4 scenarios")
    say("═" * 70)
    if not findings:
        say("  (no new problems — unusual; expand scenarios)")
    else:
        for i, f in enumerate(findings, 1):
            say(f"\n [{i}] {f}")
    say("\n" + "═" * 70)
    say(f" Total findings: {len(findings)}  ·  webhook-forwards: {len(forward_log)}")
    say("═" * 70)


if __name__ == "__main__":
    asyncio.run(main_run())
