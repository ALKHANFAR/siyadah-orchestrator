"""
Stress Test Drive for Phase-8 Sniper Validator
================================================

Three "impossible" flow shapes the user challenged the new architecture to
survive. Run as a self-contained script (no pytest, no live Postgres):

    .venv_test/bin/python tests/stress_three_flows.py

Loads REAL piece schemas captured from AP production (gmail/slack/google-
sheets/http/webhook) into an in-memory registry, then runs the production
validator's logic — every helper (_walk_steps, _contains_handlebars,
_is_banned, _strip_version, _extract_auth_type) is imported 1:1 from
piece_validator.py and scripts/sync_pieces.py. Only the persistence layer
(SQLAlchemy session) is replaced with a dict, so the *logic* exercised
here is identical to what runs in golden_build().
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Empty DATABASE_URL → database.py builds engine=None, models still importable.
os.environ["DATABASE_URL"] = ""
os.environ["REDIS_URL"] = ""
os.environ["ORCHESTRATOR_ALLOWED_ORIGINS"] = "http://x"
os.environ["AP_BASE_URL"] = "http://x"
os.environ["AP_EMAIL"] = ""
os.environ["AP_PASSWORD"] = ""
os.environ["AP_PROJECT_ID"] = ""

# Import the production validator's pure helpers + error class.
from piece_validator import (  # noqa: E402
    ValidationError,
    _contains_handlebars,
    _is_banned,
    _strip_version,
    _walk_steps,
)
from scripts.sync_pieces import _extract_auth_type, _build_index  # noqa: E402


# ═══════════════════════════════════════════════════════════════
# In-memory registry — same shape as PieceRegistry rows
# ═══════════════════════════════════════════════════════════════

@dataclass
class PieceRow:
    name: str
    piece_version: str
    auth_type: Optional[str]
    actions_index: dict
    triggers_index: dict


REGISTRY: dict[tuple[str, str], PieceRow] = {}


def _lookup(name: str, version: str) -> Optional[PieceRow]:
    return REGISTRY.get((name, version))


def _known_versions(name: str) -> list[str]:
    return sorted(v for (n, v) in REGISTRY.keys() if n == name)


# ═══════════════════════════════════════════════════════════════
# In-memory port of validate_trigger — mirrors piece_validator.py 1:1
# Only difference: persistence is REGISTRY/lookup helpers above.
# ═══════════════════════════════════════════════════════════════

async def validate_trigger_inmem(trigger: dict) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for path, step in _walk_steps(trigger):
        stype = step.get("type", "")
        if stype not in ("PIECE", "PIECE_TRIGGER"):
            settings = step.get("settings") or {}
            if _is_banned(settings.get("input")):
                errors.append(ValidationError(
                    error_code="BANNED_PLACEHOLDER_VALUE",
                    message=f"{path}: placeholder/auto-fill value detected in input",
                    field=f"{path}.settings.input",
                ))
            continue

        settings = step.get("settings") or {}
        piece_name = settings.get("pieceName", "")
        raw_version = settings.get("pieceVersion", "")
        version = _strip_version(raw_version)

        if not piece_name:
            errors.append(ValidationError(
                error_code="PIECE_NAME_MISSING",
                message=f"{path}: settings.pieceName is required",
                field=f"{path}.settings.pieceName",
            ))
            continue
        if not version:
            errors.append(ValidationError(
                error_code="PIECE_VERSION_MISSING",
                message=f"{path}: settings.pieceVersion is required",
                field=f"{path}.settings.pieceVersion",
                piece=piece_name,
            ))
            continue

        piece = _lookup(piece_name, version)
        if piece is None:
            known = _known_versions(piece_name)
            if known:
                errors.append(ValidationError(
                    error_code="PIECE_VERSION_NOT_IN_REGISTRY",
                    message=f"{path}: {piece_name} version {version!r} not in registry",
                    field=f"{path}.settings.pieceVersion",
                    piece=piece_name, piece_version=version,
                    hint=f"Known versions: {known[-3:]}",
                ))
            else:
                errors.append(ValidationError(
                    error_code="PIECE_NOT_IN_REGISTRY",
                    message=f"{path}: unknown piece {piece_name!r}",
                    field=f"{path}.settings.pieceName",
                    piece=piece_name, piece_version=version,
                ))
            continue

        if stype == "PIECE_TRIGGER":
            name_key = settings.get("triggerName", "")
            index = piece.triggers_index or {}
            kind = "trigger"
            field_path = f"{path}.settings.triggerName"
        else:
            name_key = settings.get("actionName", "")
            index = piece.actions_index or {}
            kind = "action"
            field_path = f"{path}.settings.actionName"

        if not name_key:
            errors.append(ValidationError(
                error_code=f"{kind.upper()}_NAME_MISSING",
                message=f"{path}: settings.{kind}Name is required",
                field=field_path, piece=piece_name, piece_version=version,
            ))
            continue
        if name_key not in index:
            errors.append(ValidationError(
                error_code=f"{kind.upper()}_NOT_FOUND",
                message=f"{path}: {kind} {name_key!r} not found in {piece_name}",
                field=field_path, piece=piece_name, piece_version=version,
                hint=f"Available {kind}s: {sorted(index.keys())[:10]}",
            ))
            continue

        step_input = settings.get("input") or {}

        # ── Auth-Type Compatibility (Multi-Auth Gauntlet) ──
        if piece.auth_type and piece.auth_type not in ("NONE", ""):
            auth_val = step_input.get("auth")
            if auth_val in (None, "", [], {}):
                errors.append(ValidationError(
                    error_code="AUTH_REQUIRED_BUT_MISSING",
                    message=f"{path}: {piece_name} requires {piece.auth_type} auth "
                            "but input.auth is missing",
                    field=f"{path}.settings.input.auth",
                    piece=piece_name, piece_version=version,
                ))

        # ── Required-field presence (Handlebars-aware) ──
        required_props: list[str] = (index.get(name_key) or {}).get(
            "required_props", []
        )
        for req in required_props:
            if req == "auth":
                continue
            if req not in step_input:
                errors.append(ValidationError(
                    error_code="REQUIRED_FIELD_MISSING",
                    message=f"{path}: required field {req!r} missing from input",
                    field=f"{path}.settings.input.{req}",
                    piece=piece_name, piece_version=version,
                ))
                continue
            val = step_input[req]
            if _contains_handlebars(val):
                continue
            if val is None or val == "" or val == [] or val == {}:
                errors.append(ValidationError(
                    error_code="REQUIRED_FIELD_EMPTY",
                    message=f"{path}: required field {req!r} is empty",
                    field=f"{path}.settings.input.{req}",
                    piece=piece_name, piece_version=version,
                ))

        if _is_banned(step_input):
            errors.append(ValidationError(
                error_code="BANNED_PLACEHOLDER_VALUE",
                message=f"{path}: placeholder/auto-fill value detected",
                field=f"{path}.settings.input",
                piece=piece_name, piece_version=version,
            ))

    return errors


# ═══════════════════════════════════════════════════════════════
# Seed registry from real schemas captured at /tmp/probe_schemas
# ═══════════════════════════════════════════════════════════════

SCHEMA_DIR = Path("/tmp/probe_schemas")
NEEDED = ["webhook", "gmail", "slack", "google-sheets", "http"]


def seed_registry():
    for short in NEEDED:
        path = SCHEMA_DIR / f"{short}.json"
        if not path.exists():
            print(f"[ERR] missing {path}")
            sys.exit(1)
        d = json.loads(path.read_text())
        name = d.get("name", "")
        version = d.get("version", "")
        if not name or not version:
            print(f"[ERR] {short}: schema empty")
            sys.exit(1)
        REGISTRY[(name, version)] = PieceRow(
            name=name,
            piece_version=version,
            auth_type=_extract_auth_type(d.get("auth")),
            actions_index=_build_index(d.get("actions")),
            triggers_index=_build_index(d.get("triggers")),
        )
        row = REGISTRY[(name, version)]
        print(f"  seeded: {name:42s} v{version:8s} "
              f"auth_type={row.auth_type!r:>16s}  "
              f"actions={len(row.actions_index):>3d}  "
              f"triggers={len(row.triggers_index):>2d}")


# ═══════════════════════════════════════════════════════════════
# Builders for flow shapes
# ═══════════════════════════════════════════════════════════════

def piece_step(name, piece, version, action, input_cfg) -> dict:
    return {
        "name": name, "type": "PIECE", "valid": True,
        "settings": {
            "pieceName": piece, "pieceVersion": f"~{version}",
            "actionName": action, "input": input_cfg,
            "propertySettings": {},
        },
    }


def webhook_trigger(next_action=None) -> dict:
    t = {
        "name": "trigger", "type": "PIECE_TRIGGER", "valid": True,
        "settings": {
            "pieceName": "@activepieces/piece-webhook",
            "pieceVersion": "~0.1.32",
            "triggerName": "catch_webhook",
            "input": {"authType": "none"},
            "propertySettings": {},
        },
    }
    if next_action:
        t["nextAction"] = next_action
    return t


def router(branches, children) -> dict:
    return {
        "name": "rtr", "type": "ROUTER", "valid": True,
        "children": children,
        "settings": {"branches": branches, "executionType": "EXECUTE_FIRST_MATCH"},
    }


def loop(items_expr, first_loop_action) -> dict:
    return {
        "name": "lp", "type": "LOOP", "valid": True,
        "firstLoopAction": first_loop_action,
        "settings": {"items": items_expr},
    }


def banner(label):
    print(f"\n{'═' * 72}\n  {label}\n{'═' * 72}")


# ═══════════════════════════════════════════════════════════════
# Resolve real action names from the registry (don't hardcode)
# ═══════════════════════════════════════════════════════════════

def pick_action(piece_name: str, version: str, *candidates: str) -> str:
    p = REGISTRY[(piece_name, version)]
    for c in candidates:
        if c in p.actions_index:
            return c
    raise SystemExit(f"None of {candidates} in {piece_name} actions: "
                     f"{sorted(p.actions_index)[:10]}")


# ═══════════════════════════════════════════════════════════════
# TEST A — Inception (4-level recursion)
# ═══════════════════════════════════════════════════════════════

async def test_a_inception() -> bool:
    banner("TEST A — Inception Flow (Router → Loop → Router → Action, 4 levels deep)")

    GMAIL_VER = REGISTRY[("@activepieces/piece-gmail", "0.12.1")].piece_version
    SLACK_VER = next(v for (n, v) in REGISTRY if n == "@activepieces/piece-slack")
    SHEETS_VER = next(v for (n, v) in REGISTRY if n == "@activepieces/piece-google-sheets")

    gmail_send = pick_action("@activepieces/piece-gmail", GMAIL_VER, "send_email")
    slack_send = pick_action("@activepieces/piece-slack", SLACK_VER,
                             "send_channel_message", "send-channel-message",
                             "send_direct_message")
    sheets_insert = pick_action("@activepieces/piece-google-sheets", SHEETS_VER,
                                "insert_row")

    # Pull REAL required-prop lists from the registry — no hand-curated lists.
    gmail_req = REGISTRY[("@activepieces/piece-gmail", GMAIL_VER)] \
        .actions_index[gmail_send]["required_props"]
    slack_req = REGISTRY[("@activepieces/piece-slack", SLACK_VER)] \
        .actions_index[slack_send]["required_props"]
    sheets_req = REGISTRY[("@activepieces/piece-google-sheets", SHEETS_VER)] \
        .actions_index[sheets_insert]["required_props"]
    print(f"  schema-required: gmail.{gmail_send}={gmail_req}")
    print(f"  schema-required: slack.{slack_send}={slack_req}")
    print(f"  schema-required: google-sheets.{sheets_insert}={sheets_req}")

    def _fill_required(req: list, base: dict) -> dict:
        """Fill every required field with a handlebars ref unless overridden."""
        out = dict(base)
        for r in req:
            if r == "auth":
                continue
            if r not in out:
                out[r] = f"{{{{trigger['body']['{r}']}}}}"
        return out

    inner_ok_input = _fill_required(gmail_req, {
        "auth": "{{connections['gmail-conn']}}",
        "receiver": ["{{loop['item']['email']}}"],
        "subject":  "{{loop['item']['name']}}",
        "body":     "{{loop['item']['body']}}",
    })
    # The broken step intentionally OMITS `body`; validator must catch it
    # despite being 4 levels deep.
    inner_broken_input = _fill_required(gmail_req, {
        "auth": "{{connections['gmail-conn']}}",
        "receiver": ["x@x.com"],
        "subject": "x",
        # MISSING: body
    })
    inner_broken_input.pop("body", None)  # ensure it's gone if _fill added it

    inner_ok = piece_step("step_inner_ok",
        "@activepieces/piece-gmail", GMAIL_VER, gmail_send, inner_ok_input)
    inner_broken = piece_step("step_inner_broken",
        "@activepieces/piece-gmail", GMAIL_VER, gmail_send, inner_broken_input)
    inner_router = router(
        branches=[
            {"branchName": "ok", "branchType": "CONDITION", "conditions": []},
            {"branchName": "missing-body", "branchType": "FALLBACK"},
        ],
        children=[inner_ok, inner_broken],
    )
    inner_loop = loop("{{trigger['body']['items']}}", inner_router)

    slack_clean = piece_step("step_slack",
        "@activepieces/piece-slack", SLACK_VER, slack_send,
        _fill_required(slack_req, {
            "auth": "{{connections['slack-conn']}}",
            "channel": "{{trigger['body']['channel']}}",
            "text":    "{{trigger['body']['msg']}}",
        }))
    sheets_clean = piece_step("step_sheets",
        "@activepieces/piece-google-sheets", SHEETS_VER, sheets_insert,
        _fill_required(sheets_req, {
            "auth": "{{connections['sheets-conn']}}",
            "values": {"A": "{{trigger['body']['name']}}"},
        }))

    outer = router(
        branches=[
            {"branchName": "to-slack",  "branchType": "CONDITION", "conditions": []},
            {"branchName": "loop-it",   "branchType": "CONDITION", "conditions": []},
            {"branchName": "to-sheets", "branchType": "FALLBACK"},
        ],
        children=[slack_clean, inner_loop, sheets_clean],
    )
    trigger = webhook_trigger(outer)

    errors = await validate_trigger_inmem(trigger)
    print(f"\nValidator returned {len(errors)} error(s):")
    for e in errors:
        print(f"  • [{e.error_code}] @ {e.field}")
        print(f"      → {e.message}")

    expected_path = "trigger.nextAction.children[1].firstLoopAction.children[1]"
    matched = [e for e in errors
               if e.error_code == "REQUIRED_FIELD_MISSING"
               and e.field == f"{expected_path}.settings.input.body"]

    ok = len(errors) == 1 and len(matched) == 1
    print(f"\nExpected: 1 REQUIRED_FIELD_MISSING at {expected_path}.settings.input.body")
    print(f"Got:      {len(errors)} total, {len(matched)} matching the deep-nested path")
    print(f"\nRESULT: {'✓ PASS' if ok else '✗ FAIL'}")
    return ok


# ═══════════════════════════════════════════════════════════════
# TEST B — Multi-Auth Gauntlet
# ═══════════════════════════════════════════════════════════════

async def test_b_multiauth() -> bool:
    banner("TEST B — Multi-Auth Gauntlet (OAUTH2 / OAUTH2 / no-auth)")

    GMAIL_VER  = next(v for (n, v) in REGISTRY if n == "@activepieces/piece-gmail")
    SLACK_VER  = next(v for (n, v) in REGISTRY if n == "@activepieces/piece-slack")
    HTTP_VER   = next(v for (n, v) in REGISTRY if n == "@activepieces/piece-http")

    print("\nRegistry auth_type metadata:")
    for n, label in [
        ("@activepieces/piece-gmail", "Gmail"),
        ("@activepieces/piece-slack", "Slack"),
        ("@activepieces/piece-http",  "HTTP"),
    ]:
        row = next(r for (rn, _v), r in REGISTRY.items() if rn == n)
        print(f"  {label:10s} → auth_type={row.auth_type!r}")

    gmail_send = pick_action("@activepieces/piece-gmail", GMAIL_VER, "send_email")
    slack_send = pick_action("@activepieces/piece-slack", SLACK_VER,
                             "send_channel_message", "send-channel-message",
                             "send_direct_message")
    http_send  = pick_action("@activepieces/piece-http", HTTP_VER,
                             "send_request", "send-request")

    gmail_req = REGISTRY[("@activepieces/piece-gmail", GMAIL_VER)] \
        .actions_index[gmail_send]["required_props"]
    slack_req = REGISTRY[("@activepieces/piece-slack", SLACK_VER)] \
        .actions_index[slack_send]["required_props"]
    http_req  = REGISTRY[("@activepieces/piece-http",  HTTP_VER)] \
        .actions_index[http_send]["required_props"]
    print(f"\n  schema-required: gmail.{gmail_send}={gmail_req}")
    print(f"  schema-required: slack.{slack_send}={slack_req}")
    print(f"  schema-required: http.{http_send}={http_req}")

    def _fill(req, base):
        out = dict(base)
        for r in req:
            if r == "auth":
                continue
            if r not in out:
                out[r] = f"{{{{trigger['body']['{r}']}}}}"
        return out

    # ─── Phase B1: clean flow (all 3 pieces with correct auth posture) ───
    gmail_ok = piece_step("g", "@activepieces/piece-gmail", GMAIL_VER, gmail_send,
        _fill(gmail_req, {
            "auth": "{{connections['gmail-conn']}}",
            "receiver": ["x@y.com"], "subject": "s", "body": "b",
        }))
    slack_ok = piece_step("s", "@activepieces/piece-slack", SLACK_VER, slack_send,
        _fill(slack_req, {
            "auth": "{{connections['slack-conn']}}",
            "channel": "general", "text": "hi",
        }))
    # HTTP — auth_type is None in registry → no auth required
    http_ok = piece_step("h", "@activepieces/piece-http", HTTP_VER, http_send,
        _fill(http_req, {}))

    slack_ok["nextAction"] = http_ok
    gmail_ok["nextAction"] = slack_ok
    trigger_clean = webhook_trigger(gmail_ok)

    print("\n--- Phase B1: clean Multi-Auth flow ---")
    err_clean = await validate_trigger_inmem(trigger_clean)
    print(f"Validator → {len(err_clean)} error(s)")
    for e in err_clean:
        print(f"  • [{e.error_code}] @ {e.field}: {e.message}")
    clean_ok = len(err_clean) == 0

    # ─── Phase B2: break Gmail's auth — must flag AUTH_REQUIRED_BUT_MISSING ───
    print("\n--- Phase B2: removing Gmail's `auth` (mismatch test) ---")
    gmail_no_auth_input = _fill(gmail_req, {
        # NO auth key — everything else as handlebars
        "receiver": ["x@y.com"], "subject": "s", "body": "b",
    })
    gmail_no_auth_input.pop("auth", None)
    gmail_no_auth = piece_step("g", "@activepieces/piece-gmail",
                               GMAIL_VER, gmail_send, gmail_no_auth_input)
    gmail_no_auth["nextAction"] = slack_ok
    trigger_broken = webhook_trigger(gmail_no_auth)
    err_broken = await validate_trigger_inmem(trigger_broken)
    print(f"Validator → {len(err_broken)} error(s)")
    for e in err_broken:
        print(f"  • [{e.error_code}] @ {e.field}: {e.message}")

    auth_errs = [e for e in err_broken
                 if e.error_code == "AUTH_REQUIRED_BUT_MISSING"
                 and e.piece == "@activepieces/piece-gmail"]
    broken_caught = len(auth_errs) == 1 and len(err_broken) == 1

    print(f"\nB1 clean accepted: {clean_ok}")
    print(f"B2 missing-auth caught (exactly 1 error, on Gmail): {broken_caught}")
    ok = clean_ok and broken_caught
    print(f"\nRESULT: {'✓ PASS' if ok else '✗ FAIL'}")
    return ok


# ═══════════════════════════════════════════════════════════════
# TEST C — Dynamic Injection (100% handlebars)
# ═══════════════════════════════════════════════════════════════

async def test_c_dynamic() -> bool:
    banner("TEST C — Dynamic Injection (every Sheets field is a handlebars variable)")

    SHEETS_VER = next(v for (n, v) in REGISTRY if n == "@activepieces/piece-google-sheets")
    sheets_insert = pick_action("@activepieces/piece-google-sheets", SHEETS_VER, "insert_row")
    required = REGISTRY[("@activepieces/piece-google-sheets", SHEETS_VER)] \
        .actions_index[sheets_insert]["required_props"]
    print(f"\n  google-sheets.{sheets_insert} required props per registry: {required}")

    # Phase C1: every required prop present, ALL as handlebars
    full_dyn_input = {"auth": "{{connections['sheets-conn']}}"}
    for prop in required:
        if prop == "auth":
            continue
        full_dyn_input[prop] = f"{{{{trigger['body']['{prop}']}}}}"
    sheets_full = piece_step("s", "@activepieces/piece-google-sheets",
                             SHEETS_VER, sheets_insert, full_dyn_input)
    trigger_dyn = webhook_trigger(sheets_full)
    print("\n--- Phase C1: every field is {{...}} ---")
    err_dyn = await validate_trigger_inmem(trigger_dyn)
    print(f"Validator → {len(err_dyn)} error(s)")
    for e in err_dyn:
        print(f"  • [{e.error_code}] @ {e.field}: {e.message}")
    dyn_ok = len(err_dyn) == 0

    # Phase C2: drop ONE required field — handlebars don't excuse absence
    if not required or all(p == "auth" for p in required):
        print("[skip] no non-auth required props to drop")
        return dyn_ok
    drop = next(p for p in required if p != "auth")
    print(f"\n--- Phase C2: dropping required field {drop!r} entirely ---")
    missing_input = dict(full_dyn_input)
    del missing_input[drop]
    sheets_missing = piece_step("s", "@activepieces/piece-google-sheets",
                                SHEETS_VER, sheets_insert, missing_input)
    trigger_miss = webhook_trigger(sheets_missing)
    err_miss = await validate_trigger_inmem(trigger_miss)
    print(f"Validator → {len(err_miss)} error(s)")
    for e in err_miss:
        print(f"  • [{e.error_code}] @ {e.field}: {e.message}")

    miss_caught = any(e.error_code == "REQUIRED_FIELD_MISSING"
                      and e.field.endswith(f".{drop}")
                      for e in err_miss)
    print(f"\nC1 all-handlebars accepted: {dyn_ok}")
    print(f"C2 missing-{drop} flagged:    {miss_caught}")
    ok = dyn_ok and miss_caught
    print(f"\nRESULT: {'✓ PASS' if ok else '✗ FAIL'}")
    return ok


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

async def main():
    print("Seeding in-memory registry from REAL AP schemas (probed today):")
    seed_registry()

    results = [
        ("A_Inception_Flow",      await test_a_inception()),
        ("B_Multi_Auth_Gauntlet", await test_b_multiauth()),
        ("C_Dynamic_Injection",   await test_c_dynamic()),
    ]

    print("\n" + "═" * 72)
    print("  FINAL VERDICT")
    print("═" * 72)
    for name, ok in results:
        print(f"  {'✓ PASS' if ok else '✗ FAIL'}   {name}")
    n_pass = sum(1 for _, ok in results if ok)
    print(f"\n  {n_pass}/{len(results)} stress flows passed")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
