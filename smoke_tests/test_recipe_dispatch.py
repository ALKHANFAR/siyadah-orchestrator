import pytest
from unittest.mock import AsyncMock, patch

import main


class FakeEngine:
    pass


@pytest.mark.asyncio
async def test_build_recipe_flow_reaches_golden_build_without_llm():
    fake_engine = FakeEngine()

    async def fake_auto_resolve_piece(engine, piece):
        return piece, {
            "name": piece,
            "version": "~test",
            "actions": {
                "insert_row": {"displayName": "Insert Row", "props": {}},
                "send_email": {"displayName": "Send Email", "props": {}},
            },
            "triggers": {
                "catch_webhook": {"displayName": "Catch Webhook", "props": {}},
            },
        }

    async def fake_build_action_chain(specs, counter, engine):
        assert len(specs) == 2
        assert specs[0]["piece"] == "@activepieces/piece-google-sheets"
        assert specs[0]["action_name"] == "insert_row"
        assert specs[0]["input"]["spreadsheetId"] == "sheet_123"
        assert specs[0]["input"]["sheetId"] == 0
        assert specs[0]["input"]["first_row_headers"] is True
        assert specs[0]["input"]["values"]["email"] == "{{trigger.body.email}}"

        assert specs[1]["piece"] == "@activepieces/piece-gmail"
        assert specs[1]["action_name"] == "send_email"
        assert specs[1]["input"]["receiver"] == "{{trigger.body.email}}"
        assert specs[1]["input"]["draft"] is False

        return {"name": "step_1", "type": "PIECE"}

    async def fake_golden_build(engine, pid, name, trigger, **kwargs):
        assert name == "سيادة — موظف استقبال Lead"
        assert trigger["type"] == "PIECE_TRIGGER"
        return {"flow_id": "FLOW_RECIPE_TEST", "status": "ENABLED"}

    with patch.object(main, "auto_resolve_piece", side_effect=fake_auto_resolve_piece), \
         patch.object(main, "_build_action_chain", side_effect=fake_build_action_chain), \
         patch.object(main, "golden_build", side_effect=fake_golden_build):

        out = await main._mcp_dispatch(
            fake_engine,
            "build_recipe_flow",
            {
                "recipe": "webhook_to_google_sheets_and_gmail",
                "config": {
                    "spreadsheetId": "sheet_123",
                    "sheetId": 0,
                    "columns": ["name", "email", "phone", "timestamp"],
                    "gmail_to": "{{trigger.body.email}}",
                },
            },
            "pid-test",
            {},
            owner_email="test@siyadah.ai",
        )

    assert out["flow_id"] == "FLOW_RECIPE_TEST"
    assert out["status"] == "ENABLED"
