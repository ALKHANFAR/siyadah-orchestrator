import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.registry_reader import (
    canonical_piece_name,
    get_piece_action_schema,
    load_registry,
    validate_action_against_registry,
    validate_actions_against_registry,
)


def code(exc: HTTPException) -> str:
    return exc.detail["error"]


def detail(exc: HTTPException) -> dict:
    return exc.detail


def test_registry_loads_expected_core_pieces():
    reg = load_registry()
    assert "@activepieces/piece-google-sheets" in reg["pieces_by_name"]
    assert "@activepieces/piece-gmail" in reg["pieces_by_name"]
    assert "google-sheets" in reg["pieces_by_slug"]
    assert "gmail" in reg["pieces_by_slug"]


def test_canonical_piece_names():
    assert canonical_piece_name("gmail") == "@activepieces/piece-gmail"
    assert canonical_piece_name("@activepieces/piece-gmail") == "@activepieces/piece-gmail"


def test_get_existing_action_schema_from_registry():
    schema = get_piece_action_schema("@activepieces/piece-google-sheets", "insert_row")
    assert "required_fields" in schema
    assert "dropdown_fields" in schema
    assert "dynamic_fields" in schema
    assert "spreadsheetId" in schema["required_fields"]
    assert "sheetId" in schema["required_fields"]


def test_unknown_piece_blocked():
    with pytest.raises(HTTPException) as e:
        validate_action_against_registry({
            "type": "PIECE",
            "piece": "@activepieces/piece-not-real",
            "action_name": "send_email",
            "input": {},
        })
    assert e.value.status_code == 422
    assert code(e.value) == "REGISTRY_PIECE_NOT_FOUND"


def test_unknown_action_blocked():
    with pytest.raises(HTTPException) as e:
        validate_action_against_registry({
            "type": "PIECE",
            "piece": "@activepieces/piece-gmail",
            "action_name": "not_real_action",
            "input": {},
        })
    assert e.value.status_code == 422
    assert code(e.value) == "REGISTRY_ACTION_NOT_FOUND"


def test_missing_piece_or_action_shape_blocked():
    with pytest.raises(HTTPException) as e:
        validate_action_against_registry({
            "type": "PIECE",
            "display_name": "Bad action",
            "input": {},
        })
    assert e.value.status_code == 422
    assert code(e.value) == "REGISTRY_REQUIRED_FIELD_MISSING"
    assert "piece" in detail(e.value)["missing_required_fields"]
    assert "action_name" in detail(e.value)["missing_required_fields"]


def test_non_piece_action_is_ignored():
    validate_action_against_registry({
        "type": "CODE",
        "code": "return input",
        "input": {},
    })


def test_gmail_missing_all_required_blocked():
    with pytest.raises(HTTPException) as e:
        validate_action_against_registry({
            "type": "PIECE",
            "piece": "gmail",
            "action_name": "send_email",
            "input": {},
        })
    assert code(e.value) == "REGISTRY_REQUIRED_FIELD_MISSING"
    for field in ["receiver", "subject", "body", "body_type", "draft"]:
        assert field in detail(e.value)["missing_required_fields"]


def test_gmail_missing_draft_blocked():
    with pytest.raises(HTTPException) as e:
        validate_action_against_registry({
            "type": "PIECE",
            "piece": "@activepieces/piece-gmail",
            "action_name": "send_email",
            "input": {
                "receiver": "test@example.com",
                "subject": "Hello",
                "body": "Body",
                "body_type": "plain_text",
            },
        })
    assert code(e.value) == "REGISTRY_REQUIRED_FIELD_MISSING"
    assert "draft" in detail(e.value)["missing_required_fields"]


def test_gmail_complete_passes():
    validate_action_against_registry({
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
    })


def test_google_sheets_empty_input_blocked_required_fields():
    with pytest.raises(HTTPException) as e:
        validate_action_against_registry({
            "type": "PIECE",
            "piece": "@activepieces/piece-google-sheets",
            "action_name": "insert_row",
            "input": {},
        })
    assert code(e.value) == "REGISTRY_REQUIRED_FIELD_MISSING"
    assert "spreadsheetId" in detail(e.value)["missing_required_fields"]
    assert "sheetId" in detail(e.value)["missing_required_fields"]
    assert "values" in detail(e.value)["missing_required_fields"]
    assert "first_row_headers" in detail(e.value)["missing_required_fields"]


def test_google_sheets_missing_dropdown_only_blocked_as_dropdown():
    with pytest.raises(HTTPException) as e:
        validate_action_against_registry({
            "type": "PIECE",
            "piece": "@activepieces/piece-google-sheets",
            "action_name": "insert_row",
            "input": {
                "first_row_headers": True,
                "values": {"name": "{{trigger.body.name}}"},
            },
        })
    assert code(e.value) == "REGISTRY_REQUIRED_FIELD_MISSING"
    assert "spreadsheetId" in detail(e.value)["missing_required_fields"]
    assert "sheetId" in detail(e.value)["missing_required_fields"]


def test_google_sheets_missing_dynamic_values_blocked():
    with pytest.raises(HTTPException) as e:
        validate_action_against_registry({
            "type": "PIECE",
            "piece": "@activepieces/piece-google-sheets",
            "action_name": "insert_row",
            "input": {
                "spreadsheetId": "sheet_123",
                "sheetId": "0",
                "first_row_headers": True,
            },
        })
    assert code(e.value) == "REGISTRY_REQUIRED_FIELD_MISSING"
    assert "values" in detail(e.value)["missing_required_fields"]


def test_google_sheets_complete_passes():
    validate_action_against_registry({
        "type": "PIECE",
        "piece": "@activepieces/piece-google-sheets",
        "action_name": "insert_row",
        "input": {
            "spreadsheetId": "sheet_123",
            "sheetId": "0",
            "first_row_headers": True,
            "values": {"name": "{{trigger.body.name}}"},
        },
    })


def test_settings_input_shape_supported():
    validate_action_against_registry({
        "type": "PIECE",
        "settings": {
            "pieceName": "@activepieces/piece-gmail",
            "actionName": "send_email",
            "input": {
                "receiver": "test@example.com",
                "subject": "Hello",
                "body": "Body",
                "body_type": "plain_text",
                "draft": False,
            },
        },
    })


def test_recursive_actions_validate_nested_branch_failure():
    actions = [
        {
            "type": "ROUTER",
            "branches": [
                {
                    "actions": [
                        {
                            "type": "PIECE",
                            "piece": "@activepieces/piece-gmail",
                            "action_name": "not_real_action",
                            "input": {},
                        }
                    ]
                }
            ],
        }
    ]

    with pytest.raises(HTTPException) as e:
        validate_actions_against_registry(actions)

    assert code(e.value) == "REGISTRY_ACTION_NOT_FOUND"
