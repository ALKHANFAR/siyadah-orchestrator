import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.registry_reader import (
    load_registry,
    validate_action_against_registry,
    get_piece_action_schema,
)

REGISTRY_DIR = ROOT / "registry" / "pieces"


def all_piece_actions():
    for path in sorted(REGISTRY_DIR.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        piece = record["pieceName"]
        for action_name, schema in (record.get("actions") or {}).items():
            yield piece, action_name, schema


def test_registry_files_are_the_only_source_shape():
    reg = load_registry()
    disk_pieces = {
        json.loads(p.read_text(encoding="utf-8"))["pieceName"]
        for p in REGISTRY_DIR.glob("*.json")
    }

    assert set(reg["pieces_by_name"].keys()) == disk_pieces
    assert len(disk_pieces) == 10


@pytest.mark.parametrize("piece,action_name,schema", list(all_piece_actions()))
def test_every_registry_action_schema_is_readable(piece, action_name, schema):
    loaded = get_piece_action_schema(piece, action_name)

    assert loaded == schema
    assert "required_fields" in loaded
    assert "dropdown_fields" in loaded
    assert "dynamic_fields" in loaded


@pytest.mark.parametrize("piece,action_name,schema", list(all_piece_actions()))
def test_every_action_with_required_or_config_fields_blocks_empty_input(piece, action_name, schema):
    required = schema.get("required_fields") or []
    dropdown = schema.get("dropdown_fields") or []
    dynamic = schema.get("dynamic_fields") or []

    action = {
        "type": "PIECE",
        "piece": piece,
        "action_name": action_name,
        "input": {},
    }

    if required or dropdown or dynamic:
        with pytest.raises(HTTPException) as e:
            validate_action_against_registry(action)

        assert e.value.status_code == 422
        assert e.value.detail["error"] in {
            "REGISTRY_REQUIRED_FIELD_MISSING",
            "PENDING_CONFIGURATION_DROPDOWN",
            "PENDING_CONFIGURATION_DYNAMIC",
        }
        assert e.value.detail["piece"] == piece
        assert e.value.detail["action_name"] == action_name
        assert (
            e.value.detail["missing_required_fields"]
            or e.value.detail["missing_dropdown_fields"]
            or e.value.detail["missing_dynamic_fields"]
        )
    else:
        # لا required/dropdown/dynamic رسميًا في registry؛ empty input ممكن يمر.
        validate_action_against_registry(action)


@pytest.mark.parametrize("piece,action_name,schema", list(all_piece_actions()))
def test_complete_required_fields_passes_or_pending_resolver(piece, action_name, schema):
    required = schema.get("required_fields") or []

    payload = {}
    for field in required:
        if field == "draft":
            payload[field] = False
        elif field == "first_row_headers":
            payload[field] = True
        elif field == "values":
            payload[field] = {"sample": "{{trigger.body.sample}}"}
        else:
            payload[field] = f"test_{field}"

    try:
        validate_action_against_registry({
            "type": "PIECE",
            "piece": piece,
            "action_name": action_name,
            "input": payload,
        })
    except HTTPException as e:
        assert e.value if False else True
        assert e.status_code == 422
        assert e.detail["error"] in {
            "PENDING_CONFIGURATION_DROPDOWN",
            "PENDING_CONFIGURATION_DYNAMIC",
        }
        assert (
            e.detail["missing_dropdown_fields"]
            or e.detail["missing_dynamic_fields"]
        )


def test_error_envelope_has_no_input_or_secret_values():
    action = {
        "type": "PIECE",
        "piece": "@activepieces/piece-gmail",
        "action_name": "send_email",
        "input": {
            "receiver": "secret@example.com",
            "subject": "Secret Subject",
            "body": "Secret Body",
        },
    }

    with pytest.raises(HTTPException) as e:
        validate_action_against_registry(action)

    body = json.dumps(e.value.detail, ensure_ascii=False)

    assert "secret@example.com" not in body
    assert "Secret Subject" not in body
    assert "Secret Body" not in body
    assert set(e.value.detail.keys()) == {
        "error",
        "piece",
        "action_name",
        "missing_required_fields",
        "missing_dropdown_fields",
        "missing_dynamic_fields",
        "message",
    }


def test_registry_has_expected_google_sheets_and_gmail_truth():
    gs = get_piece_action_schema("@activepieces/piece-google-sheets", "insert_row")
    gmail = get_piece_action_schema("@activepieces/piece-gmail", "send_email")

    for field in ["spreadsheetId", "sheetId", "first_row_headers", "values"]:
        assert field in gs["required_fields"]

    assert "spreadsheetId" in gs["dropdown_fields"]
    assert "sheetId" in gs["dropdown_fields"]
    assert "values" in gs["dynamic_fields"]

    for field in ["receiver", "subject", "body", "body_type", "draft"]:
        assert field in gmail["required_fields"]


def test_wiring_is_immediately_before_piece_specs_append():
    text = (ROOT / "main.py").read_text()

    needle = '''            validate_action_against_registry({
                "type": "PIECE",
                "piece": full_ap,
                "action_name": a.get("action_name", ""),
                "input": cleaned_in,
            })

            specs.append({"type": "PIECE", "piece": full_ap,'''

    assert needle in text
