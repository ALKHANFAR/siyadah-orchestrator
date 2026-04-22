"""
Sales Funnel — Incremental Deployment in Doses
================================================
Until main.py's _build_action_chain fix (commit 44cd78a) is merged to main
and Railway redeploys, `{{step_N[...]}}` references inside multi-step
flows map to BUILD order, not INPUT order. Doses 1-3 use the correct
reverse-order step_N refs so they run on the current live orchestrator.

Run a single dose:   python3 deploy_sales_funnel_doses.py --dose=2
Run all:             python3 deploy_sales_funnel_doses.py --all
"""
import argparse
import asyncio
import json
import sys
import time

import httpx

BASE = "https://siyadah-orchestrator-production.up.railway.app"
TIMEOUT = 120


SCORER_CODE = """export const code = async (inputs) => {
  const d = inputs.data || {};
  const budget = Number(d.budget || 0);
  const email = (d.email || '').toLowerCase();
  const freeMail = ['gmail.com','yahoo.com','hotmail.com','outlook.com'];
  const isBusinessEmail = email.includes('@') &&
    !freeMail.some(fm => email.endsWith('@'+fm));
  let score = 30;
  if (budget >= 50000) score += 30;
  else if (budget >= 10000) score += 20;
  else if (budget >= 1000) score += 10;
  if (isBusinessEmail) score += 15;
  if (d.phone) score += 10;
  if (d.company) score += 10;
  const interest = (d.interest_level || '').toLowerCase();
  if (interest === 'high') score += 20; else if (interest === 'medium') score += 10;
  if (score > 100) score = 100;
  const tier = score >= 75 ? 'HOT' : score >= 55 ? 'WARM' : 'COLD';
  return {
    name: d.name || 'Unknown', email, phone: d.phone || '',
    company: d.company || 'N/A', product: d.product || '',
    budget, score, tier,
    qualified_at: new Date().toISOString()
  };
};"""


# ────────────────────────────────────────────────────────────
# DOSE 2 — Linear 3 steps: CODE → Sheet → Gmail
# Reversed build: Gmail=step_1, Sheet=step_2, CODE=step_3
# All refs to scorer output use `{{step_3[...]}}`.
# ────────────────────────────────────────────────────────────
def dose_2_payload():
    return {
        "display_name": "Sales Dose 2 — CODE → Sheet → Gmail",
        "steps": [
            {
                "type": "CODE",
                "display_name": "① Lead Scorer",
                "code": SCORER_CODE,
                "code_input": {"data": "{{trigger['body']}}"},
            },
            {
                "type": "PIECE",
                "piece": "google-sheets",
                "action_name": "insert_row",
                "display_name": "② Log to CRM",
                "input": {
                    "spreadsheetId": "Siyadah Auto-Fill",
                    "sheetId": 0,
                    "first_row_headers": True,
                    "values": {
                        "A": "{{step_3['name']}}",
                        "B": "{{step_3['email']}}",
                        "C": "{{step_3['phone']}}",
                        "D": "{{step_3['company']}}",
                        "E": "{{step_3['tier']}}",
                        "F": "{{step_3['score']}}",
                        "G": "{{step_3['qualified_at']}}",
                    },
                },
            },
            {
                "type": "PIECE",
                "piece": "gmail",
                "action_name": "send_email",
                "display_name": "③ Notify Sales",
                "input": {
                    "receiver": ["a@siyadah-ai.com"],
                    "subject": "Dose 2: {{step_3['name']}} [{{step_3['tier']}}] — {{step_3['score']}}",
                    "body_type": "plain_text",
                    "body": (
                        "Name: {{step_3['name']}}\n"
                        "Company: {{step_3['company']}}\n"
                        "Email: {{step_3['email']}}\n"
                        "Budget: {{step_3['budget']}}\n"
                        "Tier: {{step_3['tier']}}\n"
                        "Score: {{step_3['score']}}\n"
                        "Qualified: {{step_3['qualified_at']}}"
                    ),
                    "draft": False,
                },
            },
        ],
    }


# ────────────────────────────────────────────────────────────
# DOSE 3 — Add ROUTER with 1 condition branch (HOT) + FALLBACK
# Input: [CODE, Sheet, ROUTER(HOT: 1 action, FALLBACK: 1 action), Gmail]
# Reverse build counter trace:
#   Gmail=step_1 (counter→2)
#   ROUTER=step_2 (counter→3)
#     Branch 1 HOT (1 action): step_3 (counter→4)
#     Branch 2 FALLBACK (1 action): step_4 (counter→5)
#   Sheet=step_5 (counter→6)
#   CODE=step_6 (counter→7)
# All refs to scorer = {{step_6[...]}}
# ────────────────────────────────────────────────────────────
def dose_3_payload():
    return {
        "display_name": "Sales Dose 3 — + ROUTER(2 branches × 1 action)",
        "steps": [
            {
                "type": "CODE",
                "display_name": "① Lead Scorer",
                "code": SCORER_CODE,
                "code_input": {"data": "{{trigger['body']}}"},
            },
            {
                "type": "PIECE",
                "piece": "google-sheets",
                "action_name": "insert_row",
                "display_name": "② Log to CRM",
                "input": {
                    "spreadsheetId": "Siyadah Auto-Fill",
                    "sheetId": 0,
                    "first_row_headers": True,
                    "values": {
                        "A": "{{step_6['name']}}",
                        "B": "{{step_6['email']}}",
                        "C": "{{step_6['company']}}",
                        "D": "{{step_6['tier']}}",
                        "E": "{{step_6['score']}}",
                    },
                },
            },
            {
                "type": "ROUTER",
                "display_name": "③ Route by Tier",
                "branches": [
                    {
                        "name": "HOT",
                        "conditions": [[{
                            "operator": "TEXT_CONTAINS",
                            "first_value": "{{step_6['tier']}}",
                            "second_value": "HOT",
                        }]],
                        "actions": [
                            {
                                "type": "PIECE",
                                "piece": "gmail",
                                "action_name": "send_email",
                                "display_name": "HOT Alert",
                                "input": {
                                    "receiver": ["sales@siyadah-ai.com"],
                                    "subject": "🔥 HOT Lead — {{step_6['name']}}",
                                    "body_type": "plain_text",
                                    "body": (
                                        "Urgent! Close within 1h:\n"
                                        "{{step_6['name']}} / {{step_6['company']}}\n"
                                        "Score: {{step_6['score']}}/100"
                                    ),
                                    "draft": False,
                                },
                            }
                        ],
                    },
                    {
                        "name": "Other",
                        "branch_type": "FALLBACK",
                        "actions": [
                            {
                                "type": "CODE",
                                "display_name": "Archive Non-HOT",
                                "code": (
                                    "export const code = async (i) => "
                                    "({ status: 'archived', name: i.name, "
                                    "at: new Date().toISOString() });"
                                ),
                                "code_input": {"name": "{{step_6['name']}}"},
                            }
                        ],
                    },
                ],
            },
            {
                "type": "PIECE",
                "piece": "gmail",
                "action_name": "send_email",
                "display_name": "④ Summary to Manager",
                "input": {
                    "receiver": ["manager@siyadah-ai.com"],
                    "subject": "📊 Funnel: {{step_6['name']}} ({{step_6['tier']}})",
                    "body_type": "plain_text",
                    "body": (
                        "Name: {{step_6['name']}}\n"
                        "Company: {{step_6['company']}}\n"
                        "Tier: {{step_6['tier']}} | Score: {{step_6['score']}}"
                    ),
                    "draft": False,
                },
            },
        ],
    }


# ────────────────────────────────────────────────────────────
# DOSE 4 — Full funnel: CODE → Sheet → ROUTER(3 branches × 2 actions) → Summary
# Input: [CODE, Sheet, ROUTER{HOT,WARM,COLD with 2 actions each}, Gmail summary]
# Reverse build trace:
#   Gmail summary=step_1 (counter→2)
#   ROUTER=step_2 (counter→3)
#     HOT (2 actions reversed): step_3 (action2), step_4 (action1); counter→5
#     WARM: step_5, step_6; counter→7
#     COLD: step_7, step_8; counter→9
#   Sheet=step_9 (counter→10)
#   CODE=step_10 (counter→11)
# All refs to scorer = {{step_10[...]}}
# ────────────────────────────────────────────────────────────
def dose_4_payload():
    return {
        "display_name": "Sales Dose 4 — Full Funnel",
        "steps": [
            {
                "type": "CODE",
                "display_name": "① Lead Scorer",
                "code": SCORER_CODE,
                "code_input": {"data": "{{trigger['body']}}"},
            },
            {
                "type": "PIECE",
                "piece": "google-sheets",
                "action_name": "insert_row",
                "display_name": "② Log to CRM",
                "input": {
                    "spreadsheetId": "Siyadah Auto-Fill",
                    "sheetId": 0,
                    "first_row_headers": True,
                    "values": {
                        "A": "{{step_10['name']}}",
                        "B": "{{step_10['email']}}",
                        "C": "{{step_10['phone']}}",
                        "D": "{{step_10['company']}}",
                        "E": "{{step_10['product']}}",
                        "F": "{{step_10['budget']}}",
                        "G": "{{step_10['tier']}}",
                        "H": "{{step_10['score']}}",
                        "I": "{{step_10['qualified_at']}}",
                    },
                },
            },
            {
                "type": "ROUTER",
                "display_name": "③ Route by Funnel Tier",
                "branches": [
                    {
                        "name": "HOT — Close Now",
                        "conditions": [[{
                            "operator": "TEXT_CONTAINS",
                            "first_value": "{{step_10['tier']}}",
                            "second_value": "HOT",
                        }]],
                        "actions": [
                            {
                                "type": "PIECE",
                                "piece": "gmail",
                                "action_name": "send_email",
                                "display_name": "Quote to Lead",
                                "input": {
                                    "receiver": ["{{step_10['email']}}"],
                                    "subject": "أهلاً {{step_10['name']}} — عرضك جاهز",
                                    "body_type": "plain_text",
                                    "body": (
                                        "أهلاً {{step_10['name']}}،\n\n"
                                        "سنتواصل معك خلال ساعة بعرض لشركة {{step_10['company']}}.\n\n"
                                        "— فريق المبيعات"
                                    ),
                                    "draft": False,
                                },
                            },
                            {
                                "type": "PIECE",
                                "piece": "gmail",
                                "action_name": "send_email",
                                "display_name": "Alert Sales Team",
                                "input": {
                                    "receiver": ["sales@siyadah-ai.com"],
                                    "subject": "🔥 HOT ({{step_10['score']}}) — {{step_10['name']}}",
                                    "body_type": "plain_text",
                                    "body": (
                                        "Name: {{step_10['name']}}\n"
                                        "Company: {{step_10['company']}}\n"
                                        "Budget: {{step_10['budget']}}\n"
                                        "Score: {{step_10['score']}}/100"
                                    ),
                                    "draft": False,
                                },
                            },
                        ],
                    },
                    {
                        "name": "WARM — Nurture",
                        "conditions": [[{
                            "operator": "TEXT_CONTAINS",
                            "first_value": "{{step_10['tier']}}",
                            "second_value": "WARM",
                        }]],
                        "actions": [
                            {
                                "type": "PIECE",
                                "piece": "gmail",
                                "action_name": "send_email",
                                "display_name": "Nurture Email",
                                "input": {
                                    "receiver": ["{{step_10['email']}}"],
                                    "subject": "{{step_10['name']}} — دليل {{step_10['product']}}",
                                    "body_type": "plain_text",
                                    "body": (
                                        "أهلاً {{step_10['name']}}،\n\n"
                                        "جهّزنا لك دليلاً شاملاً. سنتواصل بعد 3 أيام.\n\n"
                                        "— سيادة"
                                    ),
                                    "draft": False,
                                },
                            },
                            {
                                "type": "PIECE",
                                "piece": "google-sheets",
                                "action_name": "insert_row",
                                "display_name": "Follow-up Task",
                                "input": {
                                    "spreadsheetId": "Siyadah Auto-Fill",
                                    "sheetId": 0,
                                    "first_row_headers": True,
                                    "values": {
                                        "A": "FOLLOWUP",
                                        "B": "{{step_10['name']}}",
                                        "C": "{{step_10['email']}}",
                                        "D": "متابعة خلال 3 أيام",
                                    },
                                },
                            },
                        ],
                    },
                    {
                        "name": "COLD — Newsletter",
                        "branch_type": "FALLBACK",
                        "actions": [
                            {
                                "type": "CODE",
                                "display_name": "Archive Cold",
                                "code": (
                                    "export const code = async (i) => "
                                    "({ status: 'nurture_list', name: i.name, "
                                    "added_at: new Date().toISOString() });"
                                ),
                                "code_input": {"name": "{{step_10['name']}}"},
                            },
                            {
                                "type": "PIECE",
                                "piece": "gmail",
                                "action_name": "send_email",
                                "display_name": "Educational Content",
                                "input": {
                                    "receiver": ["{{step_10['email']}}"],
                                    "subject": "مرحباً {{step_10['name']}} — النشرة",
                                    "body_type": "plain_text",
                                    "body": (
                                        "أهلاً {{step_10['name']}}،\n\n"
                                        "أضفناك لنشرتنا الأسبوعية. حين تصبح جاهزاً، نحن على بُعد رسالة.\n\n"
                                        "— سيادة"
                                    ),
                                    "draft": False,
                                },
                            },
                        ],
                    },
                ],
            },
            {
                "type": "PIECE",
                "piece": "gmail",
                "action_name": "send_email",
                "display_name": "④ Summary to Manager",
                "input": {
                    "receiver": ["manager@siyadah-ai.com"],
                    "subject": "📊 Funnel — {{step_10['name']}} ({{step_10['tier']}})",
                    "body_type": "plain_text",
                    "body": (
                        "━━━━━━━━━━━━━━━━━━━\n"
                        "Name: {{step_10['name']}}\n"
                        "Company: {{step_10['company']}}\n"
                        "Email: {{step_10['email']}}\n"
                        "Phone: {{step_10['phone']}}\n"
                        "━━━━━━━━━━━━━━━━━━━\n"
                        "Product: {{step_10['product']}}\n"
                        "Budget: {{step_10['budget']}}\n"
                        "━━━━━━━━━━━━━━━━━━━\n"
                        "Score: {{step_10['score']}}/100\n"
                        "Tier: {{step_10['tier']}}\n"
                        "Time: {{step_10['qualified_at']}}\n"
                        "━━━━━━━━━━━━━━━━━━━"
                    ),
                    "draft": False,
                },
            },
        ],
    }


DOSES = {2: dose_2_payload, 3: dose_3_payload, 4: dose_4_payload}


async def wait_alive(client: httpx.AsyncClient, max_tries: int = 20) -> bool:
    for i in range(max_tries):
        try:
            r = await client.get(f"{BASE}/health")
            if r.status_code == 200:
                print(f"  orchestrator alive ({r.json().get('version')})")
                return True
        except Exception:
            pass
        await asyncio.sleep(8 + i)
    print("  orchestrator unreachable after retries — aborting")
    return False


async def deploy_and_verify(dose_num: int) -> str | None:
    payload = DOSES[dose_num]()
    bar = "═" * 68
    print(f"\n{bar}\n  DOSE {dose_num} — {payload['display_name']}\n{bar}")

    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        if not await wait_alive(c):
            return None

        t0 = time.time()
        r = await c.post(f"{BASE}/v2/build-complex", json=payload)
        dt = time.time() - t0
        print(f"\n[build] HTTP {r.status_code} — {dt:.1f}s")

        fid = None
        if r.status_code == 200:
            fid = r.json().get("flow_id")
            print(f"[build] Flow ID: {fid}")
            steps = r.json().get("steps", [])
            for s in steps:
                struct = s.get("structure") or (
                    s.get("piece", "") or "").replace("@activepieces/piece-", "")
                print(f"        {s.get('step'):8s} {struct}")
        else:
            txt = r.text[:1200]
            # race-condition recovery: extract flow id from error
            import re
            m = re.search(r"Flow\s+([A-Za-z0-9]+)\s+not ENABLED", txt)
            if m:
                fid = m.group(1)
                print(f"[build] ⚠ ENABLED verification raced, flow_id={fid}, re-checking...")
                await asyncio.sleep(3)
            else:
                print(f"[build] error: {txt}")
                return None

        if not fid:
            return None

        rd = await c.post(
            f"{BASE}/v2/mcp/execute",
            json={"tool": "diagnose_flow", "parameters": {"flow_id": fid}},
        )
        if rd.status_code == 200:
            d = rd.json().get("result", {})
            print(f"\n[diagnose] status={d.get('status')} "
                  f"total_steps={d.get('total_steps')}")
            for s in d.get("steps", []):
                if isinstance(s, dict):
                    print(f"           {s.get('name'):8s} "
                          f"[{s.get('type'):14s}] — "
                          f"{(s.get('displayName') or '')[:55]}")

        rt = await c.post(
            f"{BASE}/v2/test-webhook/{fid}",
            json={
                "name": "أحمد التجربة", "email": "ahmed@acme.sa",
                "phone": "+966501234567", "company": "Acme SA",
                "product": "Siyadah Orchestrator", "budget": 75000,
                "interest_level": "high",
            },
        )
        print(f"\n[test]   webhook HTTP {rt.status_code}: "
              f"{rt.json().get('status') if rt.status_code == 200 else rt.text[:200]}")

        print(f"\n[done]   webhook URL:")
        print(f"         https://activepieces-production-2499.up.railway.app"
              f"/api/v1/webhooks/{fid}")
        return fid


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dose", type=int, choices=[2, 3, 4])
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if args.all:
        for n in [2, 3, 4]:
            await deploy_and_verify(n)
    elif args.dose:
        await deploy_and_verify(args.dose)
    else:
        print("usage: --dose=2|3|4  or  --all")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
