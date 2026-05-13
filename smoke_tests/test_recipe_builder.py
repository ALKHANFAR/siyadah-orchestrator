import pytest
from fastapi import HTTPException

from services.recipe_builder import (
    RECIPE_WEBHOOK_TO_SHEETS_AND_GMAIL,
    build_recipe,
)


def base_config():
    return {
        "spreadsheetId": "sheet_123",
        "sheetId": 0,
        "columns": ["name", "email", "phone", "timestamp"],
        "gmail_to": "{{trigger.body.email}}",
    }


def test_recipe_builds_complete_actions_without_llm():
    out = build_recipe(RECIPE_WEBHOOK_TO_SHEETS_AND_GMAIL, base_config())

    assert out["trigger"]["piece"] == "@activepieces/piece-webhook"
    assert len(out["actions"]) == 2

    sheets = out["actions"][0]
    assert sheets["piece"] == "@activepieces/piece-google-sheets"
    assert sheets["action_name"] == "insert_row"
    assert sheets["input"]["spreadsheetId"] == "sheet_123"
    assert sheets["input"]["sheetId"] == 0
    assert sheets["input"]["first_row_headers"] is True
    assert sheets["input"]["values"]["email"] == "{{trigger.body.email}}"

    gmail = out["actions"][1]
    assert gmail["piece"] == "@activepieces/piece-gmail"
    assert gmail["action_name"] == "send_email"
    assert gmail["input"]["receiver"] == "{{trigger.body.email}}"
    assert gmail["input"]["body_type"] == "plain_text"
    assert gmail["input"]["draft"] is False


def test_recipe_rejects_missing_spreadsheet_id():
    cfg = base_config()
    cfg.pop("spreadsheetId")

    with pytest.raises(HTTPException) as ex:
        build_recipe(RECIPE_WEBHOOK_TO_SHEETS_AND_GMAIL, cfg)

    assert ex.value.status_code == 422
    assert ex.value.detail["error"] == "RECIPE_REQUIRED_CONFIG_MISSING"
    assert "spreadsheetId" in ex.value.detail["missing_fields"]


def test_recipe_rejects_invalid_columns():
    cfg = base_config()
    cfg["columns"] = []

    with pytest.raises(HTTPException) as ex:
        build_recipe(RECIPE_WEBHOOK_TO_SHEETS_AND_GMAIL, cfg)

    assert ex.value.status_code == 422
    assert ex.value.detail["error"] == "RECIPE_REQUIRED_CONFIG_MISSING"


def test_unknown_recipe_rejected():
    with pytest.raises(HTTPException) as ex:
        build_recipe("not_real_recipe", base_config())

    assert ex.value.status_code == 400
    assert ex.value.detail["error"] == "UNKNOWN_RECIPE"
