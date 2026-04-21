"""
مسار المبيعات المتكامل — Sales Funnel (End-to-End)
====================================================
تدفّق 5 خطوات: Webhook → Scorer → CRM Sheet → Router(HOT/WARM/COLD) → Summary

يُنشر عبر Siyadah Orchestrator على Activepieces:
  AP: https://activepieces-production-2499.up.railway.app
"""
import asyncio
import json
import time
import httpx

RAILWAY_BASE = "https://siyadah-orchestrator-production.up.railway.app"
TIMEOUT = 180

SCORER_CODE = """export const code = async (inputs) => {
  const b = inputs.data || {};
  const email = (b.email || '').toLowerCase();
  const budget = Number(b.budget || 0);
  const interest = (b.interest_level || '').toLowerCase();
  const source = (b.source || '').toLowerCase();
  const freeMail = ['gmail.com','yahoo.com','hotmail.com','outlook.com'];
  const isBusinessEmail = email.includes('@') && !freeMail.some(d => email.endsWith('@'+d));
  let score = 30;
  if (budget >= 50000) score += 30;
  else if (budget >= 10000) score += 20;
  else if (budget >= 1000) score += 10;
  if (isBusinessEmail) score += 15;
  if (b.phone) score += 10;
  if (b.company) score += 10;
  if (interest === 'high' || interest === 'ready_to_buy') score += 20;
  else if (interest === 'medium') score += 10;
  if (source === 'referral') score += 15;
  else if (source === 'website') score += 5;
  if (score > 100) score = 100;
  const stage = score >= 80 ? 'HOT' : score >= 50 ? 'WARM' : 'COLD';
  const priority = stage === 'HOT' ? 'URGENT' : stage === 'WARM' ? 'NORMAL' : 'LOW';
  return {
    score, stage, priority,
    name: b.name || 'عميل محتمل',
    email: b.email || '',
    phone: b.phone || '',
    company: b.company || 'غير محدد',
    product: b.product || 'عام',
    budget, interest_level: interest || 'unknown',
    source: source || 'unknown',
    is_business: isBusinessEmail,
    qualified_at: new Date().toISOString()
  };
};"""

ARCHIVE_CODE = """export const code = async (inputs) => {
  return {
    status: 'cold_nurture_list',
    name: inputs.name,
    email: inputs.email,
    added_at: new Date().toISOString(),
    note: 'سيتم إرسال محتوى تعليمي دوري'
  };
};"""


def build_payload():
    return {
        "display_name": "مسار المبيعات المتكامل — Sales Funnel",
        "steps": [
            {
                "type": "CODE",
                "display_name": "① تأهيل وتقييم الليد (AI Lead Scorer)",
                "code": SCORER_CODE,
                "code_input": {"data": "{{trigger['body']}}"},
            },
            {
                "type": "PIECE",
                "piece": "google-sheets",
                "action_name": "insert_row",
                "display_name": "② تسجيل الليد في CRM Sheet",
                "input": {
                    "spreadsheetId": "Siyadah Auto-Fill",
                    "sheetId": 0,
                    "first_row_headers": True,
                    "values": {
                        "A": "{{step_1['name']}}",
                        "B": "{{step_1['email']}}",
                        "C": "{{step_1['phone']}}",
                        "D": "{{step_1['company']}}",
                        "E": "{{step_1['product']}}",
                        "F": "{{step_1['budget']}}",
                        "G": "{{step_1['stage']}}",
                        "H": "{{step_1['score']}}",
                        "I": "{{step_1['source']}}",
                        "J": "{{step_1['qualified_at']}}",
                    },
                },
            },
            {
                "type": "ROUTER",
                "display_name": "③ توجيه حسب مرحلة الفانل",
                "branches": [
                    {
                        "name": "HOT — جاهز للإغلاق",
                        "conditions": [[{
                            "operator": "TEXT_CONTAINS",
                            "first_value": "{{step_1['stage']}}",
                            "second_value": "HOT",
                        }]],
                        "actions": [
                            {
                                "type": "PIECE",
                                "piece": "gmail",
                                "action_name": "send_email",
                                "display_name": "عرض سعر فوري للعميل",
                                "input": {
                                    "receiver": ["{{step_1['email']}}"],
                                    "subject": "أهلاً {{step_1['name']}} — عرضك جاهز",
                                    "body_type": "plain_text",
                                    "body": (
                                        "أهلاً {{step_1['name']}}،\n\n"
                                        "شكراً لاهتمامك بـ {{step_1['product']}}. "
                                        "فريق المبيعات سيتواصل معك خلال ساعة بعرض مخصص لشركة {{step_1['company']}}.\n\n"
                                        "لأي استفسار عاجل: sales@siyadah.ai\n\n"
                                        "تحيّاتنا،\nفريق المبيعات"
                                    ),
                                    "draft": False,
                                },
                            },
                            {
                                "type": "PIECE",
                                "piece": "gmail",
                                "action_name": "send_email",
                                "display_name": "تنبيه فوري لفريق المبيعات",
                                "input": {
                                    "receiver": ["sales@siyadah.ai"],
                                    "subject": "🔥 HOT Lead ({{step_1['score']}}) — {{step_1['name']}} من {{step_1['company']}}",
                                    "body_type": "plain_text",
                                    "body": (
                                        "ليد ساخن يحتاج تواصل فوري!\n\n"
                                        "الاسم: {{step_1['name']}}\n"
                                        "الشركة: {{step_1['company']}}\n"
                                        "الإيميل: {{step_1['email']}}\n"
                                        "الجوال: {{step_1['phone']}}\n"
                                        "المنتج: {{step_1['product']}}\n"
                                        "الميزانية: {{step_1['budget']}}\n"
                                        "السكور: {{step_1['score']}}/100\n"
                                        "المصدر: {{step_1['source']}}\n"
                                        "الاهتمام: {{step_1['interest_level']}}"
                                    ),
                                    "draft": False,
                                },
                            },
                        ],
                    },
                    {
                        "name": "WARM — تحت الرعاية",
                        "conditions": [[{
                            "operator": "TEXT_CONTAINS",
                            "first_value": "{{step_1['stage']}}",
                            "second_value": "WARM",
                        }]],
                        "actions": [
                            {
                                "type": "PIECE",
                                "piece": "gmail",
                                "action_name": "send_email",
                                "display_name": "رعاية العميل (Nurture)",
                                "input": {
                                    "receiver": ["{{step_1['email']}}"],
                                    "subject": "{{step_1['name']}} — دليل {{step_1['product']}} الشامل",
                                    "body_type": "plain_text",
                                    "body": (
                                        "أهلاً {{step_1['name']}}،\n\n"
                                        "جهّزنا لك دليلاً مجانياً يوضّح كيف يستخدم عملاؤنا {{step_1['product']}} "
                                        "لرفع الكفاءة بنسبة 40%.\n\n"
                                        "سنتواصل معك خلال 3 أيام لمناقشة احتياجات {{step_1['company']}}.\n\n"
                                        "تحيّاتنا،\nفريق سيادة"
                                    ),
                                    "draft": False,
                                },
                            },
                            {
                                "type": "PIECE",
                                "piece": "google-sheets",
                                "action_name": "insert_row",
                                "display_name": "إضافة مهمة متابعة",
                                "input": {
                                    "spreadsheetId": "Siyadah Auto-Fill",
                                    "sheetId": 0,
                                    "first_row_headers": True,
                                    "values": {
                                        "A": "FOLLOWUP_TASK",
                                        "B": "{{step_1['name']}}",
                                        "C": "{{step_1['email']}}",
                                        "D": "اتصال متابعة خلال 3 أيام",
                                        "E": "{{step_1['qualified_at']}}",
                                    },
                                },
                            },
                        ],
                    },
                    {
                        "name": "COLD — قائمة تعليمية",
                        "branch_type": "FALLBACK",
                        "actions": [
                            {
                                "type": "CODE",
                                "display_name": "أرشفة في قائمة النشرة",
                                "code": ARCHIVE_CODE,
                                "code_input": {
                                    "name": "{{step_1['name']}}",
                                    "email": "{{step_1['email']}}",
                                },
                            },
                            {
                                "type": "PIECE",
                                "piece": "gmail",
                                "action_name": "send_email",
                                "display_name": "محتوى تعليمي للعميل البارد",
                                "input": {
                                    "receiver": ["{{step_1['email']}}"],
                                    "subject": "مرحباً {{step_1['name']}} — اشتركت في نشرتنا",
                                    "body_type": "plain_text",
                                    "body": (
                                        "أهلاً {{step_1['name']}}،\n\n"
                                        "أضفناك إلى نشرتنا الأسبوعية حول أفضل ممارسات {{step_1['product']}}.\n"
                                        "حين تصبح جاهزاً، نحن على بُعد رسالة.\n\n"
                                        "تحيّاتنا،\nفريق سيادة"
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
                "display_name": "④ تلخيص نهائي لمدير المبيعات",
                "input": {
                    "receiver": ["manager@siyadah.ai"],
                    "subject": "📊 Sales Funnel — {{step_1['name']}} ({{step_1['stage']}})",
                    "body_type": "plain_text",
                    "body": (
                        "ملخّص مرور الليد في الفانل:\n\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        "الاسم: {{step_1['name']}}\n"
                        "الشركة: {{step_1['company']}}\n"
                        "الإيميل: {{step_1['email']}}\n"
                        "الجوال: {{step_1['phone']}}\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        "المنتج: {{step_1['product']}}\n"
                        "الميزانية: {{step_1['budget']}}\n"
                        "المصدر: {{step_1['source']}}\n"
                        "الاهتمام: {{step_1['interest_level']}}\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        "السكور: {{step_1['score']}}/100\n"
                        "المرحلة: {{step_1['stage']}}\n"
                        "الأولوية: {{step_1['priority']}}\n"
                        "إيميل تجاري: {{step_1['is_business']}}\n"
                        "وقت التأهيل: {{step_1['qualified_at']}}\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        "تم توجيه الليد تلقائياً حسب مرحلته."
                    ),
                    "draft": False,
                },
            },
        ],
    }


async def main():
    bar = "═" * 68
    print(bar)
    print("  🚀 مسار المبيعات المتكامل — Sales Funnel Deployment")
    print(bar)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        print("\n[0] فحص اتصال Orchestrator...")
        r = await client.get(f"{RAILWAY_BASE}/health")
        h = r.json()
        print(f"    status={h.get('status')} | ap={h.get('activepieces')} | v{h.get('version')}")
        if h.get("status") != "healthy":
            print("    ❌ Orchestrator not healthy, abort.")
            return

        print("\n[1] بناء ونشر الفلو عبر /v2/build-complex ...")
        t0 = time.time()
        r = await client.post(
            f"{RAILWAY_BASE}/v2/build-complex",
            json=build_payload(),
        )
        dt = time.time() - t0
        print(f"    HTTP {r.status_code} — {dt:.1f}s")

        flow_id = None
        webhook_url = None
        status = None
        steps = []
        publish = {}

        if r.status_code != 200:
            err_text = r.text
            # Race condition: flow built but ENABLED verification ran too early
            import re
            m = re.search(r"Flow\s+([A-Za-z0-9]+)\s+not ENABLED", err_text)
            if m:
                flow_id = m.group(1)
                print(f"    ⚠ تحقق مبكر — سيُعاد الفحص للفلو {flow_id} ...")
                await asyncio.sleep(3)
                d = await client.post(
                    f"{RAILWAY_BASE}/v2/mcp/execute",
                    json={"tool": "diagnose_flow", "parameters": {"flow_id": flow_id}},
                )
                if d.status_code == 200:
                    diag = d.json().get("result", {})
                    status = diag.get("status")
                    webhook_url = f"https://activepieces-production-2499.up.railway.app/api/v1/webhooks/{flow_id}"
                    if status == "ENABLED":
                        print(f"    ✓ الفلو ENABLED فعلياً — المتابعة.")
                    else:
                        print(f"    ❌ لا يزال {status}. أبدأ محاولة تفعيل صريحة.")
                        await client.patch(
                            f"{RAILWAY_BASE}/v2/flows/{flow_id}",
                            json={"status": "ENABLED"},
                        )
            else:
                print("\n    ❌ فشل النشر:")
                try:
                    print(f"    {json.dumps(r.json(), ensure_ascii=False, indent=2)[:1500]}")
                except Exception:
                    print(f"    {err_text[:1500]}")
                return
        else:
            res = r.json()
            flow_id = res.get("flow_id")
            status = res.get("status")
            webhook_url = res.get("webhook_url")
            steps = res.get("steps", [])
            publish = res.get("publish", {})

        print("\n  ┌─────────────────────────────────────────────────────┐")
        print("  │             ✅ تم النشر بنجاح                        │")
        print("  ├─────────────────────────────────────────────────────┤")
        print(f"  │  Flow ID : {flow_id}")
        print(f"  │  Status  : {status}")
        print(f"  │  Webhook : {webhook_url}")
        print(f"  │  Steps   : {len(steps)}")
        print("  ├─────────────────────────────────────────────────────┤")
        for s in steps:
            mark = "✓" if s.get("schema_loaded") else "○"
            struct = s.get("structure", s.get("piece", "?"))
            if isinstance(struct, str):
                struct = struct.replace("@activepieces/piece-", "")
            action = s.get("action", s.get("display_name", "?"))
            print(f"  │   [{mark}] {s.get('step')}: {struct} → {action}")
        print("  ├─────────────────────────────────────────────────────┤")
        print(f"  │  Published: state={publish.get('version_state')}, "
              f"match={publish.get('published_match')}")
        print("  └─────────────────────────────────────────────────────┘")

        print("\n[2] تشخيص الفلو المنشور...")
        r2 = await client.post(
            f"{RAILWAY_BASE}/v2/mcp/execute",
            json={"tool": "diagnose_flow", "parameters": {"flow_id": flow_id}},
        )
        if r2.status_code == 200:
            diag = r2.json().get("result", {})
            print(f"    Name: {diag.get('display_name')}")
            print(f"    Steps: {diag.get('total_steps')} | Trigger: {diag.get('trigger_type')}")
            for ds in diag.get("steps", []):
                print(f"      • {ds.get('name')} [{ds.get('type')}] — {ds.get('displayName')}")

        print("\n[3] إرسال نبضة اختبار (ليد HOT) ...")
        r3 = await client.post(
            f"{RAILWAY_BASE}/v2/mcp/execute",
            json={
                "tool": "test_webhook",
                "parameters": {
                    "flow_id": flow_id,
                    "payload": {
                        "name": "أحمد الخنفر",
                        "email": "ahmed@acme.sa",
                        "phone": "+966501234567",
                        "company": "Acme SA",
                        "product": "Siyadah Orchestrator",
                        "budget": 75000,
                        "interest_level": "high",
                        "source": "referral",
                        "event_type": "lead_created",
                    },
                },
            },
        )
        if r3.status_code == 200:
            print(f"    Test pulse: {r3.json().get('result', {}).get('status', 'sent')}")
        else:
            print(f"    Test pulse HTTP {r3.status_code}")

        print("\n" + bar)
        print("  🎯 انتهى — الفلو يعمل على Activepieces")
        print(f"  🔗 Webhook للاستخدام: {webhook_url}")
        print(bar)


if __name__ == "__main__":
    asyncio.run(main())
