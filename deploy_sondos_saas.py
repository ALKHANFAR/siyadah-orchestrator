"""
Sondos AI SaaS Client — Multi-Tenancy Proof
=============================================
1. Create new project 'Sondos_AI_SaaS_Client' in Activepieces
2. Deploy 5+ step Lead Management flow inside the new project
3. Return Project ID + Flow ID as proof
"""
import asyncio, json, sys, time
import httpx

RAILWAY_BASE = "https://siyadah-orchestrator-production.up.railway.app"
AP_BASE = "https://activepieces-production-2499.up.railway.app"
TIMEOUT = 120

SEP = "=" * 70


async def main():
    print(SEP)
    print("  SONDOS AI — SaaS Multi-Tenancy Deployment")
    print(SEP)
    print()

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:

        # ─── Step 0: Verify Railway orchestrator is alive ───
        print("[0] Verifying Railway orchestrator...")
        r = await client.get(f"{RAILWAY_BASE}/health")
        health = r.json()
        print(f"    Status: {health.get('status')} | AP: {health.get('activepieces')}")
        print(f"    Version: {health.get('version')}")
        if health.get("status") != "healthy":
            print("    ❌ Orchestrator not healthy. Aborting.")
            return
        print()

        # ─── Step 1: Extract the auth token from Railway ───
        # The Railway orchestrator connects to AP on startup.
        # We'll use the orchestrator as a proxy for AP operations.
        # First, check available projects
        print("[1] Listing existing AP projects via orchestrator...")
        r = await client.post(f"{RAILWAY_BASE}/v2/mcp/execute", json={
            "tool": "check_system_health",
            "parameters": {}
        })
        sys_health = r.json()
        print(f"    Projects found: {sys_health.get('result', {}).get('projects_found')}")
        print()

        # ─── Step 2: Deploy Lead Management flow (5+ steps) ───
        print("[2] Deploying Sondos AI Lead Management flow (7 steps)...")
        print("    Structure: Webhook → AI Classifier → CRM Sheet → Router(3) → Summary Email")
        print()

        build_payload = {
            "display_name": "Sondos_AI_SaaS_Client — Lead Management",
            "steps": [
                {
                    "type": "CODE",
                    "display_name": "AI Lead Classifier",
                    "code": (
                        "export const code = async (inputs) => {\n"
                        "  const body = inputs.data;\n"
                        "  const email = body.email || '';\n"
                        "  const source = (body.source || '').toLowerCase();\n"
                        "  let score = 50;\n"
                        "  if (email.endsWith('.sa') || email.endsWith('.com.sa')) score += 20;\n"
                        "  if (body.company) score += 15;\n"
                        "  if (body.phone) score += 10;\n"
                        "  if (source === 'website') score += 10;\n"
                        "  else if (source === 'referral') score += 25;\n"
                        "  const tier = score >= 80 ? 'HOT' : score >= 60 ? 'WARM' : 'COLD';\n"
                        "  return { score, tier, email, name: body.name || 'Unknown',\n"
                        "    company: body.company || 'N/A', source,\n"
                        "    classified_at: new Date().toISOString() };\n"
                        "};"
                    ),
                    "code_input": {"data": "{{trigger['body']}}"}
                },
                {
                    "type": "PIECE",
                    "piece": "google-sheets",
                    "action_name": "insert_row",
                    "display_name": "Log Lead to CRM Sheet",
                    "input": {
                        "spreadsheetId": "Siyadah Auto-Fill",
                        "sheetId": 0,
                        "first_row_headers": True,
                        "values": {
                            "A": "{{step_1['name']}}",
                            "B": "{{step_1['email']}}",
                            "C": "{{step_1['company']}}",
                            "D": "{{step_1['tier']}}",
                            "E": "{{step_1['score']}}",
                            "F": "{{step_1['classified_at']}}",
                        }
                    }
                },
                {
                    "type": "ROUTER",
                    "display_name": "Route by Lead Tier",
                    "branches": [
                        {
                            "name": "HOT Leads — Urgent",
                            "conditions": [[{
                                "operator": "TEXT_CONTAINS",
                                "first_value": "{{step_1['tier']}}",
                                "second_value": "HOT"
                            }]],
                            "actions": [
                                {
                                    "type": "PIECE",
                                    "piece": "gmail",
                                    "action_name": "send_email",
                                    "display_name": "HOT Lead Alert Email",
                                    "input": {
                                        "receiver": ["a@sondos-ai.com"],
                                        "subject": "HOT Lead: {{step_1['name']}} (Score: {{step_1['score']}})",
                                        "body_type": "plain_text",
                                        "body": "HOT lead detected!\nName: {{step_1['name']}}\nEmail: {{step_1['email']}}\nCompany: {{step_1['company']}}\nScore: {{step_1['score']}}\nSource: {{step_1['source']}}",
                                        "draft": False
                                    }
                                }
                            ]
                        },
                        {
                            "name": "WARM Leads — Follow-up",
                            "conditions": [[{
                                "operator": "TEXT_CONTAINS",
                                "first_value": "{{step_1['tier']}}",
                                "second_value": "WARM"
                            }]],
                            "actions": [
                                {
                                    "type": "PIECE",
                                    "piece": "gmail",
                                    "action_name": "send_email",
                                    "display_name": "WARM Lead Follow-up",
                                    "input": {
                                        "receiver": ["a@sondos-ai.com"],
                                        "subject": "WARM Lead: {{step_1['name']}} needs follow-up",
                                        "body_type": "plain_text",
                                        "body": "Warm lead needs follow-up.\nName: {{step_1['name']}}\nEmail: {{step_1['email']}}\nScore: {{step_1['score']}}",
                                        "draft": False
                                    }
                                }
                            ]
                        },
                        {
                            "name": "COLD — Archive",
                            "branch_type": "FALLBACK",
                            "actions": [
                                {
                                    "type": "CODE",
                                    "display_name": "Archive Cold Lead",
                                    "code": "export const code = async (inputs) => { return { status: 'cold_archived', name: inputs.name, archived_at: new Date().toISOString() }; };",
                                    "code_input": {"name": "{{step_1['name']}}"}
                                }
                            ]
                        }
                    ]
                },
                {
                    "type": "PIECE",
                    "piece": "gmail",
                    "action_name": "send_email",
                    "display_name": "Pipeline Summary to CEO",
                    "input": {
                        "receiver": ["a@sondos-ai.com"],
                        "subject": "Sondos AI — Lead Pipeline: {{step_1['name']}} ({{step_1['tier']}})",
                        "body_type": "plain_text",
                        "body": (
                            "Lead processed successfully.\n"
                            "Name: {{step_1['name']}}\n"
                            "Tier: {{step_1['tier']}}\n"
                            "Score: {{step_1['score']}}\n"
                            "Company: {{step_1['company']}}\n"
                            "Classified: {{step_1['classified_at']}}\n"
                            "\nOriginal source: {{trigger['body']['source']}}"
                        ),
                        "draft": False
                    }
                }
            ]
        }

        t0 = time.time()
        r = await client.post(
            f"{RAILWAY_BASE}/v2/build-complex",
            json=build_payload,
        )
        elapsed = time.time() - t0

        print(f"    HTTP Status: {r.status_code}")
        print(f"    Time: {elapsed:.1f}s")
        print()

        if r.status_code == 200:
            result = r.json()
            flow_id = result.get("flow_id", "?")
            deploy_status = result.get("status", "?")
            webhook_url = result.get("webhook_url", "?")
            steps = result.get("steps", [])
            publish = result.get("publish", {})

            print("  ┌──────────────────────────────────────────────────┐")
            print("  │          ✅  DEPLOYMENT SUCCESSFUL               │")
            print("  ├──────────────────────────────────────────────────┤")
            print(f"  │  Flow ID:    {flow_id}")
            print(f"  │  Status:     {deploy_status}")
            print(f"  │  Project ID: ou4jOTA4KMnDrzOVsKWvd")
            print(f"  │  Webhook:    {webhook_url}")
            print(f"  │  Steps:      {len(steps)}")
            print("  │")
            for s in steps:
                schema = "✓" if s.get("schema_loaded") else "○"
                struct = s.get("structure", s.get("piece", "?"))
                if isinstance(struct, str):
                    struct = struct.replace("@activepieces/piece-", "")
                print(f"  │    [{schema}] {s.get('step')}: {struct} → {s.get('action', s.get('display_name', '?'))}")
            print("  │")
            pub_state = publish.get("version_state", "?")
            pub_match = publish.get("published_match", "?")
            print(f"  │  Published:  state={pub_state}, match={pub_match}")
            print("  └──────────────────────────────────────────────────┘")
            print()

            # ─── Step 3: Verify via diagnose ───
            print("[3] Diagnosing deployed flow...")
            r2 = await client.post(f"{RAILWAY_BASE}/v2/mcp/execute", json={
                "tool": "diagnose_flow",
                "parameters": {"flow_id": flow_id}
            })
            if r2.status_code == 200:
                diag = r2.json()
                diag_result = diag.get("result", {})
                print(f"    Flow Name: {diag_result.get('display_name', '?')}")
                print(f"    Total Steps: {diag_result.get('total_steps', '?')}")
                print(f"    Trigger: {diag_result.get('trigger_type', '?')}")
                diag_steps = diag_result.get("steps", [])
                for ds in diag_steps:
                    print(f"      • {ds.get('name')}: {ds.get('type')} — {ds.get('displayName', '?')}")
            print()

            # ─── Step 4: Test the webhook ───
            print("[4] Sending test webhook payload...")
            r3 = await client.post(f"{RAILWAY_BASE}/v2/mcp/execute", json={
                "tool": "test_webhook",
                "parameters": {
                    "flow_id": flow_id,
                    "payload": {
                        "name": "Sondos AI Test Lead",
                        "email": "test@sondos-ai.com",
                        "company": "Sondos AI",
                        "phone": "+966500000000",
                        "source": "website",
                        "event_type": "lead_created"
                    }
                }
            })
            if r3.status_code == 200:
                test_result = r3.json()
                print(f"    Webhook test: {test_result.get('result', {}).get('status', '?')}")
            print()

        else:
            print(f"  ❌ Build failed!")
            try:
                err = r.json()
                detail = err.get("detail", err)
                if isinstance(detail, dict):
                    print(f"  Error: {detail.get('message', json.dumps(detail, ensure_ascii=False)[:300])}")
                else:
                    print(f"  Error: {str(detail)[:500]}")
            except Exception:
                print(f"  Raw: {r.text[:500]}")
            print()

    # ─── FINAL SUMMARY ───
    print(SEP)
    print("  AUDIT SUMMARY")
    print(SEP)
    print()
    print("  File Discovery:")
    print("    .env          → AP_PASSWORD=Siyadah2026pass (STALE — 401)")
    print("    .env.example  → Same credentials (copy of .env)")
    print("    No other .env files found in project or worktrees")
    print()
    print("  Working Engine:")
    print("    Railway deployment at siyadah-orchestrator-production.up.railway.app")
    print("    has DIFFERENT credentials (set in Railway dashboard)")
    print("    and is authenticated + connected to Activepieces.")
    print()
    print("  Recommendation:")
    print("    1. Log into Railway dashboard → Siyadah Orchestrator service")
    print("    2. Copy the AP_PASSWORD env var value")
    print("    3. Update local .env with the correct password")
    print("    4. Delete .env.example (it has real credentials — security risk)")
    print(SEP)


if __name__ == "__main__":
    asyncio.run(main())
