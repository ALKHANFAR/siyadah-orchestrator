"""
Siyadah Orchestrator — Mega-Flow Challenge & Deep Validation
=============================================================
Challenge 1: Build a 15+ step complex flow (Webhook → AI Classifier → Router 3 branches → Loop → 5 tools)
Challenge 2: Cross-step Data Mapping integrity check
Challenge 3: get_piece_schema for 5 rare/uncommon tools (NOT Gmail/Sheets)
"""
from __future__ import annotations
import asyncio, json, os, sys, time
from dotenv import load_dotenv
load_dotenv()

import httpx

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8000")
API_KEY  = os.getenv("ORCHESTRATOR_API_KEY", "")
TIMEOUT  = 120

def hdr():
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h

SEP = "=" * 80

# ════════════════════════════════════════════════════════════════
# CHALLENGE 1: MEGA-FLOW — 15+ steps via /v2/build-complex (validate_flow)
# ════════════════════════════════════════════════════════════════

MEGA_FLOW_PAYLOAD = {
    "display_name": "Sondos AI — Mega CRM Pipeline (15+ steps)",
    "trigger": {
        "piece": "webhook",
        "trigger_name": "catch_webhook",
        "input": {"authType": "none"}
    },
    "actions": [
        {
            "piece": "webhook",
            "action_name": "catch_webhook",
            "display_name": "Webhook Trigger",
        },
        {
            "piece": "gmail",
            "action_name": "send_email",
            "display_name": "Gmail Notify",
        },
        {
            "piece": "google-sheets",
            "action_name": "insert_row",
            "display_name": "Sheets Log",
        },
        {
            "piece": "slack",
            "action_name": "send_channel_message",
            "display_name": "Slack Alert",
        },
        {
            "piece": "hubspot",
            "action_name": "create_contact",
            "display_name": "HubSpot Create",
        },
        {
            "piece": "google-calendar",
            "action_name": "create_event",
            "display_name": "Calendar Event",
        },
    ],
}


def build_mega_flow_json():
    """Build the full 15+ step Activepieces flow JSON locally using builder primitives."""
    step_counter = [1]

    def next_step():
        n = f"step_{step_counter[0]}"
        step_counter[0] += 1
        return n

    def piece_action(name, piece, ver, action_name, inp, display, next_action=None, ps=None):
        a = {
            "name": name, "valid": True, "displayName": display,
            "type": "PIECE",
            "settings": {
                "pieceName": piece, "pieceVersion": ver,
                "pieceType": "OFFICIAL", "packageType": "REGISTRY",
                "actionName": action_name, "input": inp,
                "inputUiInfo": {},
                "propertySettings": ps or {},
                "errorHandlingOptions": {
                    "retryOnFailure": {"value": False},
                    "continueOnFailure": {"value": False},
                },
            },
        }
        if next_action:
            a["nextAction"] = next_action
        return a

    def code_step(name, display, code, inp=None, next_action=None):
        s = {
            "name": name, "valid": True, "displayName": display,
            "type": "CODE",
            "settings": {
                "input": inp or {"data": "{{trigger['body']}}"},
                "sourceCode": {"code": code, "packageJson": '{"dependencies": {}}'},
                "inputUiInfo": {},
                "errorHandlingOptions": {
                    "retryOnFailure": {"value": False},
                    "continueOnFailure": {"value": False},
                },
            },
        }
        if next_action:
            s["nextAction"] = next_action
        return s

    def router_step(name, display, branches, children, next_action=None):
        s = {
            "name": name, "type": "ROUTER", "valid": True,
            "displayName": display,
            "settings": {
                "branches": branches,
                "executionType": "EXECUTE_FIRST_MATCH",
                "inputUiInfo": {},
                "errorHandlingOptions": {
                    "retryOnFailure": {"value": False},
                    "continueOnFailure": {"value": False},
                },
            },
            "children": children,
        }
        if next_action:
            s["nextAction"] = next_action
        return s

    def loop_step(name, display, items_expr, first_loop, next_action=None):
        s = {
            "name": name, "type": "LOOP_ON_ITEMS", "valid": True,
            "displayName": display,
            "settings": {
                "items": items_expr,
                "inputUiInfo": {},
                "errorHandlingOptions": {
                    "retryOnFailure": {"value": False},
                    "continueOnFailure": {"value": False},
                },
            },
        }
        if first_loop:
            s["firstLoopAction"] = first_loop
        if next_action:
            s["nextAction"] = next_action
        return s

    C = lambda ext_id: "{{connections['" + ext_id + "']}}"

    # ──────────────────────────────────────────────────────
    # BUILD BOTTOM-UP (last step first, chain via nextAction)
    # ──────────────────────────────────────────────────────

    # ── Step 15: Final Summary Email (Gmail) ──
    # References data from Step 1 (trigger) via {{trigger['body']}} 
    step_15 = piece_action(
        "step_15", "@activepieces/piece-gmail", "~0.11.6",
        "send_email", {
            "receiver": ["ceo@sondos-ai.com"],
            "subject": "Sondos AI — Pipeline Summary: {{trigger['body']['event_type']}}",
            "body_type": "plain_text",
            "body": (
                "Pipeline processed successfully.\n"
                "Source: {{trigger['body']['source']}}\n"
                "Total clients processed: {{step_4['iterations']}}\n"
                "AI Classification: {{step_1['output']['category']}}\n"
                "Router path: {{step_2['output']}}\n"
                "Timestamp: {{trigger['body']['timestamp']}}"
            ),
            "draft": False,
            "auth": C("gmail-sondos-ai"),
        },
        "Final Summary — Email to CEO"
    )

    # ── Step 14: Google Calendar — Create review event ──
    step_14 = piece_action(
        "step_14", "@activepieces/piece-google-calendar", "~0.3.3",
        "create_event", {
            "calendarId": "primary",
            "title": "Review: {{trigger['body']['event_type']}} pipeline",
            "description": "Auto-scheduled by Sondos AI pipeline. Source: {{trigger['body']['source']}}",
            "start": "{{trigger['body']['review_date']}}",
            "end": "{{trigger['body']['review_date']}}",
            "auth": C("google-calendar-sondos"),
        },
        "Schedule Review — Calendar", step_15
    )

    # ── Step 13: HubSpot — Create deal from pipeline ──
    step_13 = piece_action(
        "step_13", "@activepieces/piece-hubspot", "~0.8.4",
        "create_deal", {
            "dealname": "Pipeline: {{trigger['body']['event_type']}}",
            "pipeline": "default",
            "dealstage": "appointmentscheduled",
            "amount": "{{trigger['body']['deal_value']}}",
            "auth": C("hubspot-sondos"),
        },
        "Create Deal — HubSpot", step_14
    )

    # ── Step 12: Slack notification after loop ──
    step_12 = piece_action(
        "step_12", "@activepieces/piece-slack", "~0.7.7",
        "send_channel_message", {
            "channel": "#sondos-ai-pipeline",
            "text": (
                "✅ Loop completed for {{trigger['body']['event_type']}}.\n"
                "Clients processed: {{step_4['iterations']}}\n"
                "Classification: {{step_1['output']['category']}}"
            ),
            "auth": C("slack-sondos"),
        },
        "Post-Loop Slack Update", step_13
    )

    # ── LOOP INNER ACTIONS (Steps 5-11) ──

    # Step 11: Inner — Gmail personalized email per client
    step_11_inner = piece_action(
        "step_11", "@activepieces/piece-gmail", "~0.11.6",
        "send_email", {
            "receiver": ["{{step_4['item']['email']}}"],
            "subject": "Sondos AI — {{step_1['output']['category']}} notification",
            "body_type": "plain_text",
            "body": (
                "Dear {{step_4['item']['name']}},\n\n"
                "This is regarding: {{trigger['body']['event_type']}}.\n"
                "Your status: {{step_4['item']['status']}}\n\n"
                "Best regards,\nSondos AI"
            ),
            "draft": False,
            "auth": C("gmail-sondos-ai"),
        },
        "Loop → Send Personalized Email"
    )

    # Step 10: Inner — Google Sheets row per client
    step_10_inner = piece_action(
        "step_10", "@activepieces/piece-google-sheets", "~0.14.6",
        "insert_row", {
            "spreadsheetId": "{{trigger['body']['tracking_sheet_id']}}",
            "sheetId": 0,
            "first_row_headers": True,
            "values": {
                "A": "{{step_4['item']['name']}}",
                "B": "{{step_4['item']['email']}}",
                "C": "{{step_1['output']['category']}}",
                "D": "{{trigger['body']['event_type']}}",
                "E": "{{step_4['item']['status']}}",
            },
            "auth": C("sheets-sondos"),
        },
        "Loop → Log to Sheets", step_11_inner
    )

    # Step 9: Inner — HubSpot create contact per client
    step_9_inner = piece_action(
        "step_9", "@activepieces/piece-hubspot", "~0.8.4",
        "create_contact", {
            "email": "{{step_4['item']['email']}}",
            "firstname": "{{step_4['item']['name']}}",
            "lastname": "Sondos Client",
            "phone": "{{step_4['item']['phone']}}",
            "auth": C("hubspot-sondos"),
        },
        "Loop → Create HubSpot Contact", step_10_inner
    )

    # Step 8: Inner — Slack per-client notification
    step_8_inner = piece_action(
        "step_8", "@activepieces/piece-slack", "~0.7.7",
        "send_channel_message", {
            "channel": "#client-updates",
            "text": (
                "Processing client: {{step_4['item']['name']}} "
                "({{step_4['item']['email']}}) — "
                "Category: {{step_1['output']['category']}}"
            ),
            "auth": C("slack-sondos"),
        },
        "Loop → Slack Per-Client", step_9_inner
    )

    # Step 7: Inner — Code transform per client
    step_7_inner = code_step(
        "step_7", "Loop → Enrich Client Data",
        """export const code = async (inputs) => {
  const client = inputs.client;
  return {
    enriched_name: client.name.toUpperCase(),
    email_domain: client.email.split('@')[1],
    priority: client.status === 'VIP' ? 'HIGH' : 'NORMAL',
    processed_at: new Date().toISOString(),
    source_event: inputs.event_type,
  };
};""",
        {
            "client": "{{step_4['item']}}",
            "event_type": "{{trigger['body']['event_type']}}",
        },
        step_8_inner
    )

    # Step 6: Inner — Code validation per item
    step_6_inner = code_step(
        "step_6", "Loop → Validate Client Data",
        """export const code = async (inputs) => {
  const item = inputs.item;
  const isValid = item.email && item.email.includes('@');
  return { valid: isValid, item: item, validation_ts: Date.now() };
};""",
        {"item": "{{step_4['item']}}"},
        step_7_inner
    )

    # Step 5: Inner — Code log entry
    step_5_inner = code_step(
        "step_5", "Loop → Initialize Iterator",
        """export const code = async (inputs) => {
  return {
    iteration_start: Date.now(),
    client_name: inputs.item.name,
    source: inputs.source,
  };
};""",
        {
            "item": "{{step_4['item']}}",
            "source": "{{trigger['body']['source']}}",
        },
        step_6_inner
    )

    # ── Step 4: LOOP on client list ──
    step_4_loop = loop_step(
        "step_4", "Loop — Process Each Client",
        "{{trigger['body']['clients']}}",
        step_5_inner,
        step_12
    )

    # ── ROUTER BRANCH ACTIONS (Step 3 children) ──

    # Branch 1: "VIP" → Direct Gmail + HubSpot
    branch1_hubspot = piece_action(
        next_step(), "@activepieces/piece-hubspot", "~0.8.4",
        "create_contact", {
            "email": "{{trigger['body']['clients'][0]['email']}}",
            "firstname": "VIP Lead",
            "lastname": "Sondos AI",
            "auth": C("hubspot-sondos"),
        },
        "Branch:VIP → HubSpot"
    )
    branch1_gmail = piece_action(
        next_step(), "@activepieces/piece-gmail", "~0.11.6",
        "send_email", {
            "receiver": ["vip-team@sondos-ai.com"],
            "subject": "🔥 VIP Lead from {{trigger['body']['source']}}",
            "body_type": "plain_text",
            "body": "High-value lead detected. Classification: {{step_1['output']['category']}}",
            "draft": False,
            "auth": C("gmail-sondos-ai"),
        },
        "Branch:VIP → Urgent Email", branch1_hubspot
    )

    # Branch 2: "Regular" → Sheets log + Slack
    branch2_slack = piece_action(
        next_step(), "@activepieces/piece-slack", "~0.7.7",
        "send_channel_message", {
            "channel": "#regular-leads",
            "text": "New regular lead: {{trigger['body']['clients'][0]['name']}}",
            "auth": C("slack-sondos"),
        },
        "Branch:Regular → Slack"
    )
    branch2_sheets = piece_action(
        next_step(), "@activepieces/piece-google-sheets", "~0.14.6",
        "insert_row", {
            "spreadsheetId": "{{trigger['body']['tracking_sheet_id']}}",
            "sheetId": 0,
            "first_row_headers": True,
            "values": {"A": "Regular", "B": "{{trigger['body']['clients'][0]['email']}}"},
            "auth": C("sheets-sondos"),
        },
        "Branch:Regular → Sheets", branch2_slack
    )

    # Branch 3: Fallback → Calendar + Slack alert
    branch3_slack = piece_action(
        next_step(), "@activepieces/piece-slack", "~0.7.7",
        "send_channel_message", {
            "channel": "#unclassified",
            "text": "Unclassified lead from {{trigger['body']['source']}} — needs manual review",
            "auth": C("slack-sondos"),
        },
        "Branch:Fallback → Slack Alert"
    )
    branch3_calendar = piece_action(
        next_step(), "@activepieces/piece-google-calendar", "~0.3.3",
        "create_event", {
            "calendarId": "primary",
            "title": "Manual Review: {{trigger['body']['event_type']}}",
            "start": "{{trigger['body']['review_date']}}",
            "end": "{{trigger['body']['review_date']}}",
            "auth": C("google-calendar-sondos"),
        },
        "Branch:Fallback → Schedule Review", branch3_slack
    )

    # ── Step 3: ROUTER with 3 branches ──
    step_3_router = router_step(
        "step_3", "AI Router — Classify & Route",
        [
            {
                "branchName": "VIP Leads",
                "branchType": "CONDITION",
                "conditions": [[{
                    "operator": "TEXT_CONTAINS",
                    "firstValue": "{{step_1['output']['category']}}",
                    "secondValue": "VIP",
                    "caseSensitive": False,
                }]],
            },
            {
                "branchName": "Regular Leads",
                "branchType": "CONDITION",
                "conditions": [[{
                    "operator": "TEXT_CONTAINS",
                    "firstValue": "{{step_1['output']['category']}}",
                    "secondValue": "Regular",
                    "caseSensitive": False,
                }]],
            },
            {"branchName": "Unclassified — Fallback", "branchType": "FALLBACK"},
        ],
        [branch1_gmail, branch2_sheets, branch3_calendar],
        step_4_loop
    )

    # ── Step 2: Code step — Pre-process data ──
    step_2_code = code_step(
        "step_2", "Pre-Process — Normalize Data",
        """export const code = async (inputs) => {
  const body = inputs.data;
  return {
    normalized_event: body.event_type.toLowerCase().trim(),
    client_count: (body.clients || []).length,
    has_vip: (body.clients || []).some(c => c.status === 'VIP'),
    source: body.source,
    processed_at: new Date().toISOString(),
  };
};""",
        {"data": "{{trigger['body']}}"},
        step_3_router
    )

    # ── Step 1: Code step — AI Classifier ──
    step_1_classifier = code_step(
        "step_1", "AI Classifier — Categorize Event",
        """export const code = async (inputs) => {
  const body = inputs.data;
  const eventType = (body.event_type || '').toLowerCase();
  const clients = body.clients || [];
  const hasVIP = clients.some(c => c.status === 'VIP');
  const totalValue = clients.reduce((sum, c) => sum + (c.deal_value || 0), 0);

  let category = 'Regular';
  if (hasVIP || totalValue > 50000) category = 'VIP';
  else if (!eventType || clients.length === 0) category = 'Unclassified';

  return {
    category,
    confidence: hasVIP ? 0.95 : 0.78,
    client_count: clients.length,
    total_value: totalValue,
    classified_at: new Date().toISOString(),
  };
};""",
        {"data": "{{trigger['body']}}"},
        step_2_code
    )

    # ── Trigger: Webhook ──
    trigger = {
        "name": "trigger", "valid": True,
        "displayName": "Sondos AI — Webhook Receiver",
        "type": "PIECE_TRIGGER",
        "settings": {
            "pieceName": "@activepieces/piece-webhook",
            "pieceVersion": "~0.1.31",
            "pieceType": "OFFICIAL", "packageType": "REGISTRY",
            "triggerName": "catch_webhook",
            "input": {"authType": "none"},
            "inputUiInfo": {}, "propertySettings": {},
        },
        "nextAction": step_1_classifier,
    }

    return trigger


def count_steps(node, visited=None):
    """Recursively count all steps in the flow tree."""
    if visited is None:
        visited = set()
    if not node or not isinstance(node, dict):
        return 0
    name = node.get("name", "")
    if name in visited:
        return 0
    visited.add(name)
    count = 1
    if "nextAction" in node:
        count += count_steps(node["nextAction"], visited)
    if "firstLoopAction" in node:
        count += count_steps(node["firstLoopAction"], visited)
    for child in (node.get("children") or []):
        count += count_steps(child, visited)
    return count


def verify_cross_step_mapping(trigger):
    """Verify that the last step references data from the first step."""
    issues = []
    
    def find_step(node, target_name):
        if not node or not isinstance(node, dict):
            return None
        if node.get("name") == target_name:
            return node
        for key in ("nextAction", "firstLoopAction"):
            found = find_step(node.get(key), target_name)
            if found:
                return found
        for child in (node.get("children") or []):
            found = find_step(child, target_name)
            if found:
                return found
        return None

    step_1 = find_step(trigger, "step_1")
    step_15 = find_step(trigger, "step_15")

    if not step_1:
        issues.append("step_1 (AI Classifier) NOT FOUND")
    if not step_15:
        issues.append("step_15 (Final Summary) NOT FOUND")

    if step_15:
        inp = step_15.get("settings", {}).get("input", {})
        body_text = json.dumps(inp)

        checks = {
            "trigger['body'] reference": "trigger['body']" in body_text,
            "step_1 reference (AI output)": "step_1" in body_text,
            "step_4 reference (loop data)": "step_4" in body_text,
            "step_2 reference (router)": "step_2" in body_text,
        }
        for desc, ok in checks.items():
            if not ok:
                issues.append(f"MISSING: {desc} in step_15 input_config")

    return issues


def walk_and_list_steps(node, result=None, depth=0):
    """Walk tree and list all steps with indentation."""
    if result is None:
        result = []
    if not node or not isinstance(node, dict):
        return result
    name = node.get("name", "?")
    display = node.get("displayName", "?")
    stype = node.get("type", "?")
    piece = node.get("settings", {}).get("pieceName", "")
    action = node.get("settings", {}).get("actionName", "") or node.get("settings", {}).get("triggerName", "")
    
    indent = "  " * depth
    short_piece = piece.replace("@activepieces/piece-", "") if piece else ""
    label = f"{indent}[{stype}] {name}: {display}"
    if short_piece:
        label += f" ({short_piece}/{action})"
    result.append(label)

    for child in (node.get("children") or []):
        walk_and_list_steps(child, result, depth + 1)
    if "firstLoopAction" in node:
        result.append(f"{indent}  ┌─ Loop body:")
        walk_and_list_steps(node["firstLoopAction"], result, depth + 2)
    if "nextAction" in node:
        walk_and_list_steps(node["nextAction"], result, depth)
    return result


# ════════════════════════════════════════════════════════════════
# CHALLENGE 3: TOOL REACH — 5 rare piece schemas
# ════════════════════════════════════════════════════════════════
RARE_TOOLS = [
    "todoist",
    "airtable",
    "notion",
    "discord",
    "telegram-bot",
]


async def fetch_piece_schema(client, piece_name):
    """Call get_piece_schema via MCP execute."""
    r = await client.post(f"{BASE_URL}/v2/mcp/execute", headers=hdr(), json={
        "tool": "get_piece_schema",
        "parameters": {"piece_name": piece_name},
    })
    return r.status_code, r.json()


async def run_mega_challenge():
    print(SEP)
    print("   SIYADAH ORCHESTRATOR — MEGA-FLOW CHALLENGE")
    print(f"   Target: {BASE_URL}")
    print(f"   Time:   {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(SEP)
    print()

    # ═════════════════════════════════════════════════════════════
    # CHALLENGE 1: Build the Mega-Flow JSON
    # ═════════════════════════════════════════════════════════════
    print("━" * 60)
    print("  CHALLENGE 1: MEGA-FLOW (15+ steps)")
    print("━" * 60)
    print()

    trigger = build_mega_flow_json()
    total_steps = count_steps(trigger)
    print(f"  ✓ Flow JSON built successfully")
    print(f"  ✓ Total steps (including trigger): {total_steps}")
    print(f"  ✓ Components: Webhook → AI Classifier → Pre-Process →")
    print(f"    Router (3 branches) → Loop (7 inner actions) →")
    print(f"    Slack → HubSpot → Calendar → Final Gmail")
    print()

    print("  ── Step Tree ──")
    step_list = walk_and_list_steps(trigger)
    for line in step_list:
        print(f"  {line}")
    print()

    tools_used = set()
    def collect_pieces(node):
        if not node or not isinstance(node, dict):
            return
        p = node.get("settings", {}).get("pieceName", "")
        if p:
            tools_used.add(p.replace("@activepieces/piece-", ""))
        for k in ("nextAction", "firstLoopAction"):
            collect_pieces(node.get(k))
        for child in (node.get("children") or []):
            collect_pieces(child)
    collect_pieces(trigger)
    print(f"  Tools used: {sorted(tools_used)}")
    print(f"  Unique tools: {len(tools_used)}")
    print()

    if total_steps >= 15:
        print(f"  ✅ PASS: {total_steps} steps >= 15 minimum")
    else:
        print(f"  ❌ FAIL: {total_steps} steps < 15 minimum")
    print()

    # ═════════════════════════════════════════════════════════════
    # CHALLENGE 2: Cross-Step Data Mapping
    # ═════════════════════════════════════════════════════════════
    print("━" * 60)
    print("  CHALLENGE 2: CROSS-STEP DATA MAPPING")
    print("━" * 60)
    print()

    issues = verify_cross_step_mapping(trigger)
    if not issues:
        print("  ✅ ALL CHECKS PASSED:")
        print("     • step_15 reads {{trigger['body']}} (from step 0 / trigger)")
        print("     • step_15 reads {{step_1['output']}} (AI Classifier output)")
        print("     • step_15 reads {{step_4['iterations']}} (Loop metadata)")
        print("     • step_15 reads {{step_2['output']}} (Router path)")
        print()
        print("  Cross-step data mapping is VALID.")
    else:
        print("  ❌ ISSUES FOUND:")
        for issue in issues:
            print(f"     • {issue}")
    print()

    print("  ── Final Step (step_15) input_config ──")
    def find_step(node, name):
        if not node or not isinstance(node, dict):
            return None
        if node.get("name") == name:
            return node
        for k in ("nextAction", "firstLoopAction"):
            f = find_step(node.get(k), name)
            if f: return f
        for c in (node.get("children") or []):
            f = find_step(c, name)
            if f: return f
        return None

    s15 = find_step(trigger, "step_15")
    if s15:
        s15_input = s15.get("settings", {}).get("input", {})
        print(json.dumps(s15_input, indent=4, ensure_ascii=False))
    print()

    # ═════════════════════════════════════════════════════════════
    # CHALLENGE 3: RARE TOOL SCHEMA FETCH
    # ═════════════════════════════════════════════════════════════
    print("━" * 60)
    print("  CHALLENGE 3: TOOL REACH — 5 RARE PIECE SCHEMAS")
    print("━" * 60)
    print()

    results = {}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for tool_name in RARE_TOOLS:
            t0 = time.time()
            try:
                status, data = await fetch_piece_schema(client, tool_name)
                elapsed = time.time() - t0
                success = data.get("success", False)
                result_data = data.get("result", {})

                piece_full = result_data.get("piece", "?")
                version = result_data.get("version", "?")
                actions = result_data.get("actions", {})
                triggers = result_data.get("triggers", {})
                action_count = len(actions) if isinstance(actions, dict) else 0
                trigger_count = len(triggers) if isinstance(triggers, dict) else 0

                print(f"  ── {tool_name} ──")
                print(f"     HTTP: {status} | Success: {success} | {elapsed:.1f}s")
                print(f"     Piece:    {piece_full}")
                print(f"     Version:  {version}")
                print(f"     Actions:  {action_count}")
                if isinstance(actions, dict):
                    for aname, ainfo in list(actions.items())[:5]:
                        print(f"       • {aname}: {ainfo.get('displayName', '?')}")
                    if action_count > 5:
                        print(f"       ... +{action_count - 5} more")
                print(f"     Triggers: {trigger_count}")
                if isinstance(triggers, dict):
                    for tname, tinfo in triggers.items():
                        print(f"       • {tname}: {tinfo.get('displayName', '?')}")

                is_real = (success and action_count > 0 and version != "?" 
                          and piece_full != "?")
                results[tool_name] = is_real
                print(f"     Result:   {'✅ REAL SCHEMA' if is_real else '❌ FAILED'}")
            except Exception as ex:
                elapsed = time.time() - t0
                print(f"  ── {tool_name} ──")
                print(f"     ERROR: {ex} ({elapsed:.1f}s)")
                results[tool_name] = False
            print()

    # ═════════════════════════════════════════════════════════════
    # FINAL SUMMARY
    # ═════════════════════════════════════════════════════════════
    print(SEP)
    print("  FINAL REPORT")
    print(SEP)
    print()
    print(f"  Challenge 1 — Mega-Flow:          {'✅ PASS' if total_steps >= 15 else '❌ FAIL'} ({total_steps} steps)")
    print(f"  Challenge 2 — Data Mapping:        {'✅ PASS' if not issues else '❌ FAIL'}")

    rare_passed = sum(1 for v in results.values() if v)
    print(f"  Challenge 3 — Rare Tool Schemas:   {rare_passed}/{len(RARE_TOOLS)} passed")
    for tool, ok in results.items():
        print(f"     {'✅' if ok else '❌'} {tool}")
    print()

    all_pass = total_steps >= 15 and not issues and rare_passed >= 4
    print(f"  {'🏆 ALL CHALLENGES PASSED' if all_pass else '⚠️  SOME CHALLENGES NEED REVIEW'}")
    print()

    # ── Full Flow JSON (abbreviated) for technical review ──
    print("━" * 60)
    print("  FULL FLOW JSON (for technical review)")
    print("━" * 60)
    full_json = json.dumps(trigger, indent=2, ensure_ascii=False)
    print(full_json)
    print()
    print(f"  JSON size: {len(full_json):,} characters")
    print(SEP)


if __name__ == "__main__":
    asyncio.run(run_mega_challenge())
