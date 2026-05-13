from __future__ import annotations

from typing import Any, Dict, List

from fastapi import HTTPException

from services.registry_reader import validate_action_against_registry


RECIPE_WEBHOOK_TO_SHEETS_AND_GMAIL = "webhook_to_google_sheets_and_gmail"


def _required(config: Dict[str, Any], fields: List[str]) -> None:
    missing = [f for f in fields if config.get(f) in (None, "", {}, [])]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "RECIPE_REQUIRED_CONFIG_MISSING",
                "recipe": RECIPE_WEBHOOK_TO_SHEETS_AND_GMAIL,
                "missing_fields": missing,
                "message": "Recipe config is missing required deterministic fields.",
            },
        )


def _sheet_values(columns: List[str]) -> Dict[str, str]:
    return {col: "{{trigger.body." + col + "}}" for col in columns}


def build_webhook_to_google_sheets_and_gmail_recipe(config: Dict[str, Any]) -> Dict[str, Any]:
    _required(config, ["spreadsheetId", "sheetId", "columns", "gmail_to"])

    columns = config.get("columns")
    if not isinstance(columns, list) or not columns or not all(isinstance(c, str) and c.strip() for c in columns):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "RECIPE_INVALID_COLUMNS",
                "recipe": RECIPE_WEBHOOK_TO_SHEETS_AND_GMAIL,
                "message": "columns must be a non-empty list of strings.",
            },
        )

    subject = config.get("subject") or "Lead جديد من سيادة"
    body = config.get("body") or "وصل lead جديد: {{trigger.body.name}} - {{trigger.body.email}} - {{trigger.body.phone}}"

    actions = [
        {
            "type": "PIECE",
            "piece": "@activepieces/piece-google-sheets",
            "action_name": "insert_row",
            "display_name": "حفظ lead في Google Sheets",
            "input": {
                "spreadsheetId": config["spreadsheetId"],
                "sheetId": config["sheetId"],
                "first_row_headers": True,
                "values": _sheet_values([c.strip() for c in columns]),
            },
        },
        {
            "type": "PIECE",
            "piece": "@activepieces/piece-gmail",
            "action_name": "send_email",
            "display_name": "إرسال إيميل ترحيبي",
            "input": {
                "receiver": config["gmail_to"],
                "subject": subject,
                "body": body,
                "body_type": "plain_text",
                "draft": False,
            },
        },
    ]

    for action in actions:
        validate_action_against_registry(action)

    return {
        "display_name": config.get("display_name") or "سيادة — موظف استقبال Lead",
        "trigger": {
            "piece": "@activepieces/piece-webhook",
            "trigger_name": "catch_webhook",
            "input": {"authType": "none"},
        },
        "actions": actions,
    }


def build_recipe(recipe: str, config: Dict[str, Any]) -> Dict[str, Any]:
    if recipe == RECIPE_WEBHOOK_TO_SHEETS_AND_GMAIL:
        return build_webhook_to_google_sheets_and_gmail_recipe(config)

    raise HTTPException(
        status_code=400,
        detail={
            "error": "UNKNOWN_RECIPE",
            "recipe": recipe,
            "available_recipes": [RECIPE_WEBHOOK_TO_SHEETS_AND_GMAIL],
        },
    )
