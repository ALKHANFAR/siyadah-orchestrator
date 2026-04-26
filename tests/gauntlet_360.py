"""
Siyadah 360° Gauntlet — three hostile scenarios against the live
production piece_registry that was just harvested (688 pieces).

Each scenario:
  • Constructs a deliberately broken trigger tree
  • Runs piece_validator.validate_trigger / assert_trigger
  • Captures the structured 422 detail
  • Then constructs a CORRECTED version and pushes it to AP

Execution proves the registry data is reading from Postgres (raw SQL
queries shown alongside ORM calls), the validator tunnels arbitrary
depth, and the No-Ghost contract holds — no orphan flows on failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import async_session, engine
from models import PieceRegistry
from piece_validator import (
    ValidationError,
    assert_trigger,
    validate_trigger,
)


AP = "https://activepieces-production-2499.up.railway.app"
PID_AP = "ou4jOTA4KMnDrzOVsKWvd"
TOK_FILE = "/tmp/ap_token.txt"


def banner(title: str):
    line = "═" * 76
    print(f"\n{line}\n  {title}\n{line}")


# ═══════════════════════════════════════════════════════════════
# AP client — minimal, direct
# ═══════════════════════════════════════════════════════════════

async def ap_token() -> str:
    """Refresh token; cache to disk so re-runs don't burn auth calls."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{AP}/api/v1/authentication/sign-in",
            json={
                "email": os.environ["AP_EMAIL"],
                "password": os.environ["AP_PASSWORD"],
            },
        )
        r.raise_for_status()
        tok = r.json().get("token", "")
        if not tok:
            raise RuntimeError("AP sign-in returned no token")
        Path(TOK_FILE).write_text(tok)
        return tok


async def ap_create_and_import(name: str, trigger: dict, *, publish: bool = False) -> dict:
    """Push a flow to AP. Returns the AP-side state.

    No-Ghost guarantee: if any step after create_flow fails, we delete
    the flow before raising — never leave orphaned drafts on AP.
    """
    tok = await ap_token()
    async with httpx.AsyncClient(timeout=60) as c:
        cr = await c.post(
            f"{AP}/api/v1/flows",
            headers={"Authorization": f"Bearer {tok}"},
            json={"projectId": PID_AP, "displayName": name},
        )
        cr.raise_for_status()
        fid = cr.json()["id"]
        try:
            ir = await c.post(
                f"{AP}/api/v1/flows/{fid}",
                headers={"Authorization": f"Bearer {tok}"},
                json={"type": "IMPORT_FLOW",
                      "request": {"displayName": name, "trigger": trigger}},
            )
            if ir.status_code >= 400:
                print(f"  [AP error body] {ir.text[:1500]}")
                ir.raise_for_status()
            if publish:
                pr = await c.post(
                    f"{AP}/api/v1/flows/{fid}",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"type": "LOCK_AND_PUBLISH", "request": {}},
                )
                pr.raise_for_status()
            final = await c.get(
                f"{AP}/api/v1/flows/{fid}",
                headers={"Authorization": f"Bearer {tok}"},
            )
            final.raise_for_status()
            return final.json()
        except Exception as e:
            # No-Ghost: delete the partially-built flow
            try:
                await c.delete(
                    f"{AP}/api/v1/flows/{fid}",
                    headers={"Authorization": f"Bearer {tok}"},
                )
                print(f"  [no-ghost] cleaned up {fid} on failure")
            except Exception:
                print(f"  [no-ghost] FAILED to cleanup {fid} — manual delete required")
            raise RuntimeError(f"AP push failed: {e}") from e


# ═══════════════════════════════════════════════════════════════
# Tree builders
# ═══════════════════════════════════════════════════════════════

def webhook_trigger(next_action=None) -> dict:
    t = {
        "name": "trigger", "type": "PIECE_TRIGGER", "valid": True,
        "displayName": "Gauntlet Webhook",          # ← AP requires this on trigger
        "settings": {
            "pieceName": "@activepieces/piece-webhook",
            "pieceVersion": "~0.1.32",
            "pieceType": "OFFICIAL",
            "packageType": "REGISTRY",
            "triggerName": "catch_webhook",
            "input": {"authType": "none"},
            "inputUiInfo": {},
            "propertySettings": {"authType": {"type": "MANUAL"}},
        },
    }
    if next_action:
        t["nextAction"] = next_action
    return t


def piece_step(name, piece, version, action, input_cfg, display=None) -> dict:
    return {
        "name": name, "type": "PIECE", "valid": True,
        "displayName": display or f"{piece.split('-')[-1]}.{action}",
        "settings": {
            "pieceName": piece, "pieceVersion": f"~{version}",
            "pieceType": "OFFICIAL", "packageType": "REGISTRY",
            "actionName": action, "input": input_cfg,
            "inputUiInfo": {}, "propertySettings": {},
            "errorHandlingOptions": {
                "retryOnFailure": {"value": False},
                "continueOnFailure": {"value": False},
            },
        },
    }


def router(branches, children, name="step_router") -> dict:
    return {
        "name": name, "type": "ROUTER", "valid": True,
        "displayName": "Router",
        "children": children,
        "settings": {
            "branches": branches,
            "executionType": "EXECUTE_FIRST_MATCH",
            "errorHandlingOptions": {
                "retryOnFailure": {"value": False},
                "continueOnFailure": {"value": False},
            },
        },
    }


def loop(items_expr, first_loop_action, name="step_loop") -> dict:
    return {
        "name": name, "type": "LOOP", "valid": True,
        "displayName": "Loop",
        "firstLoopAction": first_loop_action,
        "settings": {"items": items_expr},
    }


def print_errors(errs: list[ValidationError]):
    for e in errs:
        print(f"    ✗ [{e.error_code}]")
        print(f"        field   = {e.field}")
        print(f"        message = {e.message}")
        if e.piece:
            print(f"        piece   = {e.piece} v{e.piece_version}")
        if e.hint:
            print(f"        hint    = {e.hint}")


# ═══════════════════════════════════════════════════════════════
# SCENARIO 1 — HubSpot Dynamic Dependency Trap
# ═══════════════════════════════════════════════════════════════

async def scenario_1_hubspot_dependency_trap():
    banner("SCENARIO 1 — HubSpot Dynamic Dependency Trap")

    # Read create-deal contract from Postgres (raw SQL for evidence)
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT name, piece_version, auth_type, "
            "actions_index->'create-deal'->'required_props' AS required_props "
            "FROM piece_registry "
            "WHERE name = '@activepieces/piece-hubspot'"
        ))).all()
    print("\n  [SQL] Reading HubSpot contract from piece_registry:")
    print("        SELECT name, piece_version, auth_type,")
    print("               actions_index->'create-deal'->'required_props'")
    print("        FROM   piece_registry WHERE name = '@activepieces/piece-hubspot';")
    for r in rows:
        print(f"  [SQL row] {r}")

    HUB = "@activepieces/piece-hubspot"
    HUB_VER = rows[0][1]

    # ─── ATTACK: pass Gmail's `subject` field; type-o pipelineId; missing dealname ───
    print("\n  ATTACK 1.A — using Gmail field 'subject' on HubSpot, typo'd 'pipelineid':")
    bad_step = piece_step("step_1", HUB, HUB_VER, "create-deal", {
        "auth": "{{connections['hubspot-conn']}}",
        "subject": "this is a Gmail field, not HubSpot",   # irrelevant
        "body": "extra Gmail noise",                        # irrelevant
        "pipelineid": "{{trigger.body.pipe}}",              # typo: should be pipelineId
        "pipelineStageId": "{{trigger.body.stage}}",
        # MISSING: dealname (required), pipelineId (typo'd)
    })
    trigger = webhook_trigger(bad_step)
    async with async_session() as s:
        errs = await validate_trigger(s, trigger)
    print(f"\n  Validator → {len(errs)} error(s):")
    print_errors(errs)
    expected_codes = {"REQUIRED_FIELD_MISSING"}
    actual_codes = {e.error_code for e in errs}
    print(f"\n  expected codes ⊆ actual: {expected_codes.issubset(actual_codes)}")
    # Verify dealname & pipelineId both flagged
    fields_missing = {e.field.split(".")[-1] for e in errs
                      if e.error_code == "REQUIRED_FIELD_MISSING"}
    assert "dealname" in fields_missing, f"dealname not flagged: {fields_missing}"
    assert "pipelineId" in fields_missing, f"pipelineId not flagged: {fields_missing}"
    print(f"  ✓ both required fields flagged: {sorted(fields_missing)}")

    # ─── CORRECTION: use the real fields with handlebars ───
    print("\n  CORRECTION — using dealname + pipelineId + pipelineStageId (correct):")
    good_step = piece_step("step_1", HUB, HUB_VER, "create-deal", {
        "auth": "{{connections['hubspot-conn']}}",
        "dealname": "{{trigger.body.deal_name}}",
        "pipelineId": "{{trigger.body.pipeline_id}}",
        "pipelineStageId": "{{trigger.body.stage_id}}",
    })
    trigger = webhook_trigger(good_step)
    async with async_session() as s:
        errs = await validate_trigger(s, trigger)
    print(f"\n  Validator → {len(errs)} error(s) (expecting 0)")
    assert not errs, f"corrected flow should be clean: {errs}"

    # Push to AP — handlebars auth means AP will accept the IMPORT but the
    # flow won't actually run without a real HubSpot connection. That's fine:
    # the user wants to SEE it in the UI.
    name = "SIYADAH_GAUNTLET_S1_HUBSPOT_DEAL"
    print(f"\n  Pushing to AP as DRAFT '{name}' …")
    flow = await ap_create_and_import(name, trigger, publish=False)
    fid = flow["id"]
    print(f"  ✓ Flow ID = {fid}")
    print(f"    UI URL  = {AP}/projects/{PID_AP}/flows/{fid}")
    return fid


# ═══════════════════════════════════════════════════════════════
# SCENARIO 2 — Salesforce Multi-Object Stress
# ═══════════════════════════════════════════════════════════════

async def scenario_2_salesforce_unknown_action():
    banner("SCENARIO 2 — Salesforce Multi-Object Stress")

    # Print the FIRST 20 actions from the freshly-harvested Salesforce row.
    # Raw SQL — proof we're reading from Postgres, not in-memory cache.
    print("\n  [SQL] Listing Salesforce actions from piece_registry (raw query):")
    print("        SELECT piece_version, jsonb_object_keys(actions_index)")
    print("        FROM   piece_registry WHERE name = '@activepieces/piece-salesforce';")
    async with engine.connect() as conn:
        ver = (await conn.execute(text(
            "SELECT piece_version FROM piece_registry "
            "WHERE name = '@activepieces/piece-salesforce'"
        ))).scalar_one()
        actions = [r[0] for r in (await conn.execute(text(
            "SELECT jsonb_object_keys(actions_index) FROM piece_registry "
            "WHERE name = '@activepieces/piece-salesforce' "
            "ORDER BY 1 LIMIT 20"
        ))).all()]
    print(f"\n  Salesforce v{ver} — first 20 actions in registry:")
    for i, a in enumerate(actions, 1):
        print(f"    {i:2d}. {a}")

    # ─── ATTACK: invoke an action that doesn't exist ───
    SF = "@activepieces/piece-salesforce"
    bad = piece_step("step_1", SF, ver, "summon_dragon", {
        "auth": "{{connections['salesforce-conn']}}",
    })
    trigger = webhook_trigger(bad)
    async with async_session() as s:
        errs = await validate_trigger(s, trigger)
    print(f"\n  ATTACK — calling 'summon_dragon' on Salesforce:")
    print(f"  Validator → {len(errs)} error(s):")
    print_errors(errs)
    codes = {e.error_code for e in errs}
    assert "ACTION_NOT_FOUND" in codes, f"ACTION_NOT_FOUND missing: {codes}"
    print(f"  ✓ ACTION_NOT_FOUND raised")

    # ─── ATTACK 2: piece that doesn't exist at all ───
    bad2 = piece_step("step_1", "@activepieces/piece-totally-fake", "1.0.0",
                      "anything", {})
    trigger2 = webhook_trigger(bad2)
    async with async_session() as s:
        errs2 = await validate_trigger(s, trigger2)
    print(f"\n  ATTACK 2.A — calling totally fake piece:")
    print(f"  Validator → {len(errs2)} error(s):")
    print_errors(errs2)
    codes2 = {e.error_code for e in errs2}
    assert "PIECE_NOT_IN_REGISTRY" in codes2, f"PIECE_NOT_IN_REGISTRY missing: {codes2}"
    print(f"  ✓ PIECE_NOT_IN_REGISTRY raised")


# ═══════════════════════════════════════════════════════════════
# SCENARIO 3 — Deep Inception (Webhook → Router → Loop → HubSpot, depth 4)
# ═══════════════════════════════════════════════════════════════

async def scenario_3_deep_inception():
    banner("SCENARIO 3 — Deep Inception (4 levels)")

    # Pull HubSpot version from registry
    async with async_session() as s:
        hub = (await s.execute(
            select(PieceRegistry).where(PieceRegistry.name == "@activepieces/piece-hubspot")
        )).scalar_one()
    HUB = hub.name
    HUB_VER = hub.piece_version

    # ─── ATTACK: deeply-nested HubSpot step missing pipelineStageId ───
    bad_hub = piece_step("step_inner_hub", HUB, HUB_VER, "create-deal", {
        "auth": "{{connections['hubspot-conn']}}",
        "dealname": "{{loop.item.dealname}}",
        "pipelineId": "{{loop.item.pipe}}",
        # MISSING: pipelineStageId  ← 4 levels deep, expect Sniper to find it
    })
    inner_router = router(
        branches=[
            {"branchName": "ok", "branchType": "CONDITION", "conditions": []},
            {"branchName": "fallback", "branchType": "FALLBACK"},
        ],
        children=[bad_hub, bad_hub],  # both branches reference the broken step
        name="step_inner_router",
    )
    inner_loop = loop("{{trigger.body.deals}}", inner_router, name="step_loop")
    outer_router = router(
        branches=[
            {"branchName": "process", "branchType": "CONDITION", "conditions": []},
            {"branchName": "skip", "branchType": "FALLBACK"},
        ],
        children=[inner_loop, None],
        name="step_outer_router",
    )
    trigger_bad = webhook_trigger(outer_router)

    print("\n  Tree: Webhook → outer Router → branch[0] = Loop → inner Router → "
          "broken HubSpot")
    async with async_session() as s:
        errs = await validate_trigger(s, trigger_bad)
    print(f"\n  Validator → {len(errs)} error(s) at depth ≥ 4:")
    print_errors(errs)
    deep_misses = [
        e for e in errs
        if e.error_code == "REQUIRED_FIELD_MISSING"
        and ".firstLoopAction." in e.field
        and ".children[" in e.field
        and e.field.endswith(".pipelineStageId")
    ]
    assert deep_misses, f"Sniper failed to tunnel to depth 4: {errs}"
    print(f"  ✓ Sniper found {len(deep_misses)} deep-nested miss(es) "
          f"(both router branches → both flagged)")
    for e in deep_misses:
        depth = e.field.count(".children[") + e.field.count(".firstLoopAction") \
              + e.field.count(".nextAction")
        print(f"    depth={depth}  field={e.field}")

    # ─── CORRECTION: same shape, all fields present ───
    print("\n  CORRECTION — adding pipelineStageId, keeping the 4-level structure:")
    good_hub = piece_step("step_inner_hub", HUB, HUB_VER, "create-deal", {
        "auth": "{{connections['hubspot-conn']}}",
        "dealname": "{{loop.item.dealname}}",
        "pipelineId": "{{loop.item.pipe}}",
        "pipelineStageId": "{{loop.item.stage}}",
    })
    inner_router2 = router(
        branches=[
            {"branchName": "ok", "branchType": "CONDITION", "conditions": []},
            {"branchName": "fallback", "branchType": "FALLBACK"},
        ],
        children=[good_hub, good_hub],
        name="step_inner_router",
    )
    inner_loop2 = loop("{{trigger.body.deals}}", inner_router2, name="step_loop")
    outer_router2 = router(
        branches=[
            {"branchName": "process", "branchType": "CONDITION", "conditions": []},
            {"branchName": "skip", "branchType": "FALLBACK"},
        ],
        children=[inner_loop2, None],
        name="step_outer_router",
    )
    trigger_good = webhook_trigger(outer_router2)
    async with async_session() as s:
        errs_g = await validate_trigger(s, trigger_good)
    print(f"  Validator → {len(errs_g)} error(s) (expecting 0)")
    assert not errs_g, f"corrected deep tree should be clean: {errs_g}"

    # ─── PROOF: assert_trigger raises 422 on the broken one BEFORE AP touch ───
    print("\n  Re-running through assert_trigger to demonstrate 422 contract:")
    from fastapi import HTTPException
    try:
        async with async_session() as s:
            await assert_trigger(s, trigger_bad)
        raise AssertionError("assert_trigger should have raised")
    except HTTPException as he:
        assert he.status_code == 422
        print(f"  ✓ assert_trigger raised HTTPException(422)")
        print(f"    detail.total_errors = {he.detail['total_errors']}")
        print(f"    detail.errors[0].error_code = {he.detail['errors'][0]['error_code']}")

    # ─── PUSH the corrected version to AP ───
    name = "SIYADAH_SOVEREIGN_HUBSPOT_TEST"
    print(f"\n  Pushing CORRECTED deep-inception flow to AP as '{name}' …")
    flow = await ap_create_and_import(name, trigger_good, publish=False)
    fid = flow["id"]
    print(f"  ✓ Flow ID = {fid}")
    print(f"    UI URL  = {AP}/projects/{PID_AP}/flows/{fid}")
    return fid


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

async def main():
    print("Authenticating to AP …")
    await ap_token()

    s1_fid = await scenario_1_hubspot_dependency_trap()
    await scenario_2_salesforce_unknown_action()
    s3_fid = await scenario_3_deep_inception()

    print("\n" + "═" * 76)
    print("  GAUNTLET — FINAL SUMMARY")
    print("═" * 76)
    print(f"  Scenario 1 (HubSpot trap)        → flow: {s1_fid}")
    print(f"  Scenario 2 (Salesforce unknown)  → validator-only (no AP push)")
    print(f"  Scenario 3 (Deep Inception)      → flow: {s3_fid}")
    print(f"\n  AP UI: {AP}/projects/{PID_AP}/flows")
    print(f"\n  Both pushed flows are DRAFT (not enabled) — they need a real")
    print(f"  HubSpot connection to actually run, but they are visible in the UI.")
    await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
