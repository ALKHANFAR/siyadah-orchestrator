#!/usr/bin/env bash
# Sandbox deploy — uploads simulation_recovery.json and prints the
# webhook_url. SAFE: fails fast if env vars are missing (no silent
# prod deploy).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD_FILE="$SCRIPT_DIR/simulation_recovery.json"

: "${SIYADAH_BFF_URL:?missing — e.g. https://app.siyadah.ai}"
: "${SIYADAH_API_KEY:?missing — the tenant's API key (X-API-Key value)}"
: "${TENANT_ID:?missing — the X-Siyadah-Tenant value}"

PAYLOAD="$(jq 'del(._meta)' "$PAYLOAD_FILE")"

echo "→ Sandbox deploy to tenant=$TENANT_ID"
RESPONSE="$(curl -sS -X POST \
  "$SIYADAH_BFF_URL/api/orchestrator/v2/build-dynamic" \
  -H "Content-Type: application/json" \
  -H "X-Siyadah-Tenant: $TENANT_ID" \
  -H "X-API-Key: $SIYADAH_API_KEY" \
  -d "$PAYLOAD")"

echo "── Response ──"
echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"

WEBHOOK="$(echo "$RESPONSE" | jq -r '.webhook_url // empty' 2>/dev/null)"
[[ -n "$WEBHOOK" ]] && echo -e "\n✅ webhook_url:\n   $WEBHOOK"
