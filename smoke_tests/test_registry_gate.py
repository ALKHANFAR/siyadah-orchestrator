import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.registry_reader import validate_action_against_registry


def _err_code(exc: HTTPException) -> str:
    return exc.detail["error"]


def test_google_sheets_insert_row_missing_required_fields_is_blocked():
    action = {
        "type": "PIECE",
        "piece": "@activepieces/piece-google-sheets",
        "action_name": "insert_row",
        "input": {},
    }

    with pytest.raises(HTTPException) as e:
        validate_action_against_registry(action)

    assert e.value.status_code == 422
    assert e.value.detail["piece"] == "@activepieces/piece-google-sheets"
    assert e.value.detail["action_name"] == "insert_row"
    assert _err_code(e.value) in {
        "REGISTRY_REQUIRED_FIELD_MISSING",
        "PENDING_CONFIGURATION_DROPDOWN",
        "PENDING_CONFIGURATION_DYNAMIC",
    }


def test_gmail_send_email_missing_draft_is_blocked():
    action = {
        "type": "PIECE",
        "piece": "@activepieces/piece-gmail",
        "action_name": "send_email",
        "input": {
            "receiver": "test@example.com",
            "subject": "Hello",
            "body": "Body",
            "body_type": "plain_text",
        },
    }

    with pytest.raises(HTTPException) as e:
        validate_action_against_registry(action)

    assert e.value.status_code == 422
    assert "draft" in (
        e.value.detail["missing_required_fields"]
        + e.value.detail["missing_dropdown_fields"]
        + e.value.detail["missing_dynamic_fields"]
    )


def test_gmail_send_email_with_required_fields_passes():
    action = {
        "type": "PIECE",
        "piece": "@activepieces/piece-gmail",
        "action_name": "send_email",
        "input": {
            "receiver": "test@example.com",
            "subject": "Hello",
            "body": "Body",
            "body_type": "plain_text",
            "draft": False,
        },
    }

    validate_action_against_registry(action)


def test_unknown_action_is_blocked():
    action = {
        "type": "PIECE",
        "piece": "@activepieces/piece-gmail",
        "action_name": "not_real_action",
        "input": {},
    }

    with pytest.raises(HTTPException) as e:
        validate_action_against_registry(action)

    assert e.value.status_code == 422
    assert _err_code(e.value) == "REGISTRY_ACTION_NOT_FOUND"
