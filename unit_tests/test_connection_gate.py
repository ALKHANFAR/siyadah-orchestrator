import asyncio

from services.connection_gate import classify_connection_requirements


def test_connection_gate_classifies_active_error_and_missing_connections():
    async def run():
        live_connections = [
            {
                "pieceName": "@activepieces/piece-gmail",
                "status": "ACTIVE",
                "externalId": "gmail-active",
                "displayName": "Gmail",
                "type": "OAUTH2",
            },
            {
                "pieceName": "@activepieces/piece-google-sheets",
                "status": "ERROR",
                "externalId": "sheets-error",
                "displayName": "Google Sheets",
                "type": "CLOUD_OAUTH2",
            },
            {
                "pieceName": "@activepieces/piece-openai",
                "status": "ACTIVE",
                "externalId": "openai-active",
                "displayName": "OpenAI",
                "type": "SECRET_TEXT",
            },
        ]

        schemas = {
            "@activepieces/piece-text-helper": {"auth": None},
            "@activepieces/piece-gmail": {"auth": {"required": True, "type": "OAUTH2"}},
            "@activepieces/piece-google-sheets": {"auth": {"required": True, "type": "CLOUD_OAUTH2"}},
            "@activepieces/piece-openai": {"auth": {"required": True, "type": "SECRET_TEXT"}},
            "@activepieces/piece-hubspot": {"auth": {"required": True, "type": "OAUTH2"}},
        }

        steps = [
            {"type": "PIECE", "piece": "@activepieces/piece-text-helper"},
            {"type": "PIECE", "piece": "@activepieces/piece-gmail"},
            {"type": "PIECE", "piece": "@activepieces/piece-google-sheets"},
            {"type": "PIECE", "piece": "@activepieces/piece-openai"},
            {"type": "PIECE", "piece": "@activepieces/piece-hubspot"},
        ]

        async def fetch_schema(piece):
            return schemas[piece]

        result = await classify_connection_requirements(
            steps=steps,
            live_connections=live_connections,
            fetch_schema=fetch_schema,
        )

        assert result["status"] == "PENDING_CONNECTIONS"
        assert result["runnable_count"] == 3
        assert result["blocked_count"] == 2
        assert result["connection_ids"] == {
            "gmail": "gmail-active",
            "openai": "openai-active",
        }

        blocked = {x["piece"]: x for x in result["blocked_pieces"]}
        assert "@activepieces/piece-google-sheets" in blocked
        assert blocked["@activepieces/piece-google-sheets"]["errored_connections"][0]["status"] == "ERROR"
        assert "@activepieces/piece-hubspot" in blocked

    asyncio.run(run())
