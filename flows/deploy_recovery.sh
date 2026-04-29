#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# deploy_recovery.sh — One-shot deploy of abdulsamad_recovery_v1
#
# Usage:
#   export SIYADAH_BFF_URL=https://app.siyadah.ai
#   export SIYADAH_API_KEY=<the tenant API key>
#   export TENANT_ID=<the X-Siyadah-Tenant value>
#   ./deploy_recovery.sh
#
# What it does:
#   1. Reads abdulsamad_recovery_v1.json
#   2. Strips the _meta + _post_deploy_checklist keys (orchestrator
#      doesn't accept underscore-prefixed keys in the build payload)
#   3. POSTs to /api/orchestrator/v2/build-dynamic
#   4. Prints the resulting flow_id + webhook_url
#
# Safety: this script does NOT enable the flow automatically. The
# Orchestrator's golden_build() handles publish/enable per the
# Sovereign Tightening protocol. After the flow_id is returned,
# verify it manually in the AP UI before pointing live cart events
# at the webhook URL.
# ─────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD_FILE="$SCRIPT_DIR/abdulsamad_recovery_v1.json"

: "${SIYADAH_BFF_URL:?missing — e.g. https://app.siyadah.ai}"
: "${SIYADAH_API_KEY:?missing — the tenant's API key}"
: "${TENANT_ID:?missing — the X-Siyadah-Tenant value}"

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required to strip meta keys. Install via: brew install jq" >&2
  exit 1
fi

if [[ ! -f "$PAYLOAD_FILE" ]]; then
  echo "ERROR: payload file not found: $PAYLOAD_FILE" >&2
  exit 1
fi

PAYLOAD="$(jq 'del(._meta) | del(._post_deploy_checklist)' "$PAYLOAD_FILE")"

echo "→ Deploying recovery flow to tenant=$TENANT_ID"
echo "→ Endpoint: $SIYADAH_BFF_URL/api/orchestrator/v2/build-dynamic"
echo

RESPONSE="$(curl -sS -X POST \
  "$SIYADAH_BFF_URL/api/orchestrator/v2/build-dynamic" \
  -H "Content-Type: application/json" \
  -H "X-Siyadah-Tenant: $TENANT_ID" \
  -H "X-API-Key: $SIYADAH_API_KEY" \
  -d "$PAYLOAD")"

echo "── Orchestrator response ──"
echo "$RESPONSE" | jq . 2>/dev/null || echo "$RESPONSE"

FLOW_ID="$(echo "$RESPONSE" | jq -r '.flow_id // empty' 2>/dev/null)"
WEBHOOK_URL="$(echo "$RESPONSE" | jq -r '.webhook_url // empty' 2>/dev/null)"

if [[ -n "$FLOW_ID" ]]; then
  echo
  echo "✅ Flow deployed."
  echo "   flow_id:     $FLOW_ID"
  echo "   webhook_url: $WEBHOOK_URL"
  echo
  echo "Next steps:"
  echo "  1. Configure the Qurashi backend to POST cart-abandonment events to webhook_url above."
  echo "  2. Test with 1 internal phone number BEFORE enabling for all customers."
  echo "  3. Verify on https://activepieces-production-2499.up.railway.app that flow status = ENABLED."
else
  echo
  echo "❌ Deploy failed — no flow_id in response."
  exit 2
fi
