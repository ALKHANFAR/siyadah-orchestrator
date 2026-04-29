#!/usr/bin/env bash
# Sandbox test fire — POSTs a fake "abandoned cart" event to the
# webhook_url returned by deploy_sandbox.sh.
#
# Usage:
#   1. First run deploy_sandbox.sh — it prints a webhook_url
#   2. Then:
#        export WEBHOOK_URL="https://activepieces-production-2499.up.railway.app/api/v1/webhooks/<flow_id>"
#        export TEST_PHONE="+9665XXXXXXXX"   # YOUR phone (W1 arrives in 30 min)
#        ./test_fire.sh
#
# What happens after firing:
#   • Now      → AP webhook received, S01 stores the cart
#   • +30 min  → W1 WhatsApp lands on TEST_PHONE
#   • +60 min  → W2 (Claude-composed persuasion)
#   • +90 min  → W3 (Sovereign discount)
#
# Cancel: open AP UI → flows → "محاكاة استرجاع السلّة — Sandbox v1"
# → disable. Disabled flows stop pending delays.
set -euo pipefail

: "${WEBHOOK_URL:?missing — paste from deploy_sandbox.sh output}"
: "${TEST_PHONE:?missing — your real WhatsApp number, e.g. +966501234567}"

CART_ID="sandbox-$(date +%s)-$RANDOM"

PAYLOAD=$(cat <<EOF
{
  "cart_id": "$CART_ID",
  "customer_name": "عبدالرحمن",
  "customer_phone": "$TEST_PHONE",
  "product_name": "مسك الغزال",
  "price": 450,
  "checkout_url": "https://sa.abdulsamadalqurashi.com/cart/resume?token=test_$CART_ID",
  "abandoned_at": "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
}
EOF
)

echo "→ Firing test event:"
echo "$PAYLOAD" | jq .

echo
echo "→ POST $WEBHOOK_URL"
curl -sS -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD"

echo
echo "✅ Event fired. Check AP runs panel for execution status."
echo "   Expected: W1 message lands on $TEST_PHONE in ~30 minutes."
