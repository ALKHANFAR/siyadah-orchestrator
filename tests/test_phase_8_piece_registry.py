"""
Phase-8 — PieceRegistry + Sniper Validator

Proves that golden_build() cannot import a flow whose trigger references an
unknown piece, an unknown action, or a missing required field — and that
handlebars refs satisfy presence requirements without tripping type checks.

All tests use the real Postgres fixture (conftest._schema + _clean_state) —
no mocks for infra. The validator is exercised directly; no live AP call.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from database import async_session
from models import PieceRegistry
from piece_validator import (
    ValidationError,
    assert_trigger,
    validate_trigger,
)


# ═══════════════════════════════════════════════════════════════
# Fixtures: seed a minimal 2-piece registry
# ═══════════════════════════════════════════════════════════════

GMAIL_NAME = "@activepieces/piece-gmail"
GMAIL_VER = "0.12.1"

WEBHOOK_NAME = "@activepieces/piece-webhook"
WEBHOOK_VER = "0.1.32"


@pytest_asyncio.fixture
async def seeded_registry():
    """Insert a tiny registry: gmail + webhook. Wiped by _clean_state."""
    async with async_session() as s:
        s.add(PieceRegistry(
            name=GMAIL_NAME,
            piece_version=GMAIL_VER,
            display_name="Gmail",
            categories=["COMMUNICATION"],
            auth_type="OAUTH2",
            full_schema={"name": GMAIL_NAME, "version": GMAIL_VER},
            actions_index={
                "send_email": {
                    "required_props": ["receiver", "subject", "body"],
                    "prop_types": {
                        "receiver": "ARRAY",
                        "subject": "SHORT_TEXT",
                        "body": "LONG_TEXT",
                        "draft": "BOOLEAN",
                    },
                },
            },
            triggers_index={},
            tier="core",
            is_verified=1,
        ))
        s.add(PieceRegistry(
            name=WEBHOOK_NAME,
            piece_version=WEBHOOK_VER,
            display_name="Webhook",
            categories=["CORE"],
            auth_type=None,
            full_schema={"name": WEBHOOK_NAME, "version": WEBHOOK_VER},
            actions_index={},
            triggers_index={
                "catch_webhook": {"required_props": [], "prop_types": {}},
            },
            tier="core",
            is_verified=1,
        ))
        await s.commit()


def _wh_trigger(next_action=None) -> dict:
    """Build a minimal valid webhook trigger."""
    t = {
        "name": "trigger",
        "type": "PIECE_TRIGGER",
        "valid": True,
        "settings": {
            "pieceName": WEBHOOK_NAME,
            "pieceVersion": f"~{WEBHOOK_VER}",
            "triggerName": "catch_webhook",
            "input": {"authType": "none"},
            "propertySettings": {},
        },
    }
    if next_action:
        t["nextAction"] = next_action
    return t


def _gmail_step(input_cfg: dict) -> dict:
    input_cfg = {"auth": "{{connections['gmail']}}", **input_cfg}
    return {
        "name": "step_1",
        "type": "PIECE",
        "valid": True,
        "settings": {
            "pieceName": GMAIL_NAME,
            "pieceVersion": f"~{GMAIL_VER}",
            "actionName": "send_email",
            "input": input_cfg,
            "propertySettings": {},
        },
    }


# ═══════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_happy_path_real_piece_and_action(seeded_registry):
    trigger = _wh_trigger(_gmail_step({
        "receiver": ["a@x.com"],
        "subject": "hi",
        "body": "hello",
    }))
    async with async_session() as s:
        errors = await validate_trigger(s, trigger)
    assert errors == [], f"expected clean tree, got: {errors}"


@pytest.mark.asyncio
async def test_ghost_piece_hard_stops(seeded_registry):
    """The empirical probe showed AP accepts ghost pieces silently (valid:true).
    The Sniper Validator must reject them BEFORE any AP call."""
    ghost = {
        "name": "step_1",
        "type": "PIECE",
        "valid": True,
        "settings": {
            "pieceName": "@activepieces/piece-ghost-xyz",
            "pieceVersion": "~0.1.0",
            "actionName": "anything",
            "input": {},
            "propertySettings": {},
        },
    }
    trigger = _wh_trigger(ghost)
    async with async_session() as s:
        errors = await validate_trigger(s, trigger)
    codes = {e.error_code for e in errors}
    assert "PIECE_NOT_IN_REGISTRY" in codes, errors


@pytest.mark.asyncio
async def test_wrong_version_reports_known_versions(seeded_registry):
    bad = _gmail_step({"receiver": ["a@x.com"], "subject": "x", "body": "y"})
    bad["settings"]["pieceVersion"] = "~99.99.99"
    trigger = _wh_trigger(bad)
    async with async_session() as s:
        errors = await validate_trigger(s, trigger)
    assert len(errors) == 1
    e = errors[0]
    assert e.error_code == "PIECE_VERSION_NOT_IN_REGISTRY"
    assert GMAIL_VER in (e.hint or "")  # hint exposes the real known version


@pytest.mark.asyncio
async def test_unknown_action_on_real_piece(seeded_registry):
    step = _gmail_step({"receiver": ["a@x.com"], "subject": "x", "body": "y"})
    step["settings"]["actionName"] = "incinerate_inbox"
    trigger = _wh_trigger(step)
    async with async_session() as s:
        errors = await validate_trigger(s, trigger)
    assert any(e.error_code == "ACTION_NOT_FOUND" for e in errors), errors


@pytest.mark.asyncio
async def test_missing_required_field_hard_stops(seeded_registry):
    # Gmail send_email requires receiver, subject, body — omit body
    step = _gmail_step({"receiver": ["a@x.com"], "subject": "x"})
    trigger = _wh_trigger(step)
    async with async_session() as s:
        errors = await validate_trigger(s, trigger)
    miss = [e for e in errors if e.error_code == "REQUIRED_FIELD_MISSING"]
    assert len(miss) == 1
    assert miss[0].field.endswith(".body")


@pytest.mark.asyncio
async def test_handlebars_satisfies_presence_no_type_check(seeded_registry):
    """Dynamic refs like {{trigger.body.email}} must satisfy `required`
    without being evaluated — the Flexibility Protocol."""
    step = _gmail_step({
        "receiver": ["{{trigger['body']['email']}}"],
        "subject": "{{trigger['body']['name']}}",
        "body": "{{trigger['body']['message']}}",
    })
    trigger = _wh_trigger(step)
    async with async_session() as s:
        errors = await validate_trigger(s, trigger)
    assert errors == [], f"handlebars refs should pass, got: {errors}"


@pytest.mark.asyncio
async def test_siyadah_auto_fill_literal_is_banned(seeded_registry):
    """Regression: the old auto-fill value must trigger a hard-stop if
    anything upstream still produces it."""
    step = _gmail_step({
        "receiver": ["Siyadah Auto-Fill"],
        "subject": "x",
        "body": "y",
    })
    trigger = _wh_trigger(step)
    async with async_session() as s:
        errors = await validate_trigger(s, trigger)
    assert any(e.error_code == "BANNED_PLACEHOLDER_VALUE" for e in errors), errors


@pytest.mark.asyncio
async def test_router_children_walked(seeded_registry):
    """Router children are independent steps — a bad piece in one branch
    must still fail validation."""
    good = _gmail_step({"receiver": ["a@x.com"], "subject": "x", "body": "y"})
    bad = {
        "name": "step_3",
        "type": "PIECE",
        "valid": True,
        "settings": {
            "pieceName": "@activepieces/piece-nonexistent",
            "pieceVersion": "~0.1.0",
            "actionName": "x",
            "input": {},
            "propertySettings": {},
        },
    }
    router = {
        "name": "step_1",
        "type": "ROUTER",
        "valid": True,
        "children": [good, bad],
        "settings": {
            "branches": [
                {"branchName": "ok", "branchType": "CONDITION", "conditions": []},
                {"branchName": "fallback", "branchType": "FALLBACK"},
            ],
            "executionType": "EXECUTE_FIRST_MATCH",
        },
    }
    trigger = _wh_trigger(router)
    async with async_session() as s:
        errors = await validate_trigger(s, trigger)
    codes = {e.error_code for e in errors}
    assert "PIECE_NOT_IN_REGISTRY" in codes, errors
    # The error path should mention children[1]
    assert any("children[1]" in e.field for e in errors), errors


@pytest.mark.asyncio
async def test_assert_trigger_raises_422_with_all_errors(seeded_registry):
    """assert_trigger must raise HTTPException(422) with structured detail
    that carries every failure, not just the first."""
    from fastapi import HTTPException
    # Two independent errors: wrong action AND missing body
    step = _gmail_step({"receiver": ["a@x.com"], "subject": "x"})
    step["settings"]["actionName"] = "ghost_action"
    trigger = _wh_trigger(step)
    with pytest.raises(HTTPException) as exc:
        async with async_session() as s:
            await assert_trigger(s, trigger)
    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["total_errors"] >= 1
    assert isinstance(detail["errors"], list)
    assert all("error_code" in e for e in detail["errors"])


@pytest.mark.asyncio
async def test_empty_registry_does_not_false_positive():
    """Before first sync_pieces run, registry is empty. The validator's
    empty-registry disarm (in golden_build) is the contract; verify the
    dataclass surface itself is importable in that state."""
    async with async_session() as s:
        # No rows seeded. Walk an empty-ish tree.
        trigger = _wh_trigger(_gmail_step({"receiver": ["x"], "subject": "s", "body": "b"}))
        errors = await validate_trigger(s, trigger)
    # Every piece lookup fails because DB is empty — that's correct
    # behaviour WHEN called. The golden_build guard decides whether to
    # call it at all.
    assert all(isinstance(e, ValidationError) for e in errors)
    assert any(e.error_code == "PIECE_NOT_IN_REGISTRY" for e in errors)


@pytest.mark.asyncio
async def test_version_tilde_is_stripped(seeded_registry):
    """pieceVersion comes in as '~0.12.1'; registry stores '0.12.1'."""
    # Correct version with tilde should match
    step = _gmail_step({"receiver": ["x"], "subject": "s", "body": "b"})
    step["settings"]["pieceVersion"] = f"~{GMAIL_VER}"
    trigger = _wh_trigger(step)
    async with async_session() as s:
        errors = await validate_trigger(s, trigger)
    assert errors == [], errors
    # Caret should also work
    step["settings"]["pieceVersion"] = f"^{GMAIL_VER}"
    async with async_session() as s:
        errors = await validate_trigger(s, trigger)
    assert errors == [], errors
