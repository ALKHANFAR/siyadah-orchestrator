"""
Deploy SAICO Collection flows to project ou4jOTA4KMnDrzOVsKWvd.

Two flows are pushed via /v2/build-complex on the live Siyadah Orchestrator:
  1. SAICO Collection — Trigger    (lead webhook → enrich → log → WA warmup → Sondos call)
  2. SAICO Collection — Outcome    (Sondos webhook → parse → log → ROUTER(5))

Placeholders are set for Sondos/WhatsApp/Spreadsheet/Supervisor — replace in
the AP UI after deploy. CODE steps gracefully no-op if creds are missing.
"""
import json
import sys
import time

import httpx

ORCH = "https://siyadah-orchestrator-production.up.railway.app"
PROJECT_ID = "ou4jOTA4KMnDrzOVsKWvd"
GMAIL_EXT = "MKlKHKfL6OwZ7oqt0nt5h"
SHEETS_EXT = "TtUKW8AMWsMBlY7ayqocf"

SHEET_PLACEHOLDER = "TODO_REPLACE_WITH_SPREADSHEET_ID"
SUPERVISOR_EMAIL = "collections@saico.com.sa"
PAID_EMAIL = "treasury@saico.com.sa"
SONDOS_BASE = "TODO_REPLACE_https://api.sondos.ai"
SONDOS_KEY = "TODO_REPLACE_SONDOS_API_KEY"
SONDOS_ASSIST = "TODO_REPLACE_SONDOS_ASSISTANT_ID"
WA_URL = "TODO_REPLACE_https://graph.facebook.com/v19.0/PHONE_ID/messages"
WA_TOKEN = "TODO_REPLACE_WA_BEARER_TOKEN"
IBAN = "SA28 0500 0068 2000 4310 0002"
BANK = "مصرف الإنماء"
PAYMENT_LINK = "https://pay.saico.com.sa/POL"
COMPLAINT_CHANNEL = "8001242002"
TRANSFER_NUM = "+966112345678"

ENRICH_CODE = """
export const code = async (inputs) => {
  const b = inputs.body || {};
  const amount = Number(b.outstanding_amount || 0);
  const days = Number(b.days_overdue || 0);
  const attempt = Number(b.contact_attempt || 1);
  let urgency = 'NORMAL';
  if (amount >= 20000 || days >= 60 || attempt >= 3) urgency = 'HIGH';
  if (amount >= 50000 || days >= 120) urgency = 'CRITICAL';
  const min_down = Number(inputs.min_down_payment_pct || 30);
  const max_inst = Number(inputs.max_installments || 6);
  const disc_pct = Number(inputs.discount_authority_pct || 10);
  const down_payment = Math.round(amount * (min_down / 100));
  const monthly = max_inst > 0 ? Math.round((amount - down_payment) / max_inst) : 0;
  const discounted = Math.round(amount * (1 - disc_pct / 100));
  return {
    ok: true,
    customer_name: b.customer_name || '',
    customer_phone: b.customer_phone || '',
    id_last4: b.id_last4 || '',
    debt_type: b.debt_type || 'recourse',
    outstanding_amount: amount,
    policy_number: b.policy_number || '',
    claim_number: b.claim_number || 'N/A',
    incident_date: b.incident_date || '',
    vehicle_plate: b.vehicle_plate || '',
    days_overdue: days,
    contact_attempt: attempt,
    previous_promise: b.previous_promise || 'none',
    iban: inputs.iban || b.iban || '',
    bank_name: inputs.bank_name || b.bank_name || '',
    max_installments: max_inst,
    min_down_payment_pct: min_down,
    discount_authority_pct: disc_pct,
    down_payment_amount: down_payment,
    monthly_installment: monthly,
    discounted_amount: discounted,
    whatsapp_link: inputs.whatsapp_link || '',
    payment_link: inputs.payment_link || '',
    agent_transfer_number: inputs.agent_transfer_number || '',
    complaint_channel: inputs.complaint_channel || '8001242002',
    call_time_window: inputs.call_time_window || '09:00-21:00',
    call_datetime: new Date().toISOString(),
    language: 'ar-SA',
    urgency,
    lead_id: b.lead_id || (b.policy_number + '-' + Date.now())
  };
};
"""

WA_WARMUP_CODE = """
export const code = async (inputs) => {
  if (!inputs.whatsapp_api_url || !inputs.whatsapp_token ||
      String(inputs.whatsapp_api_url).startsWith('TODO')) {
    return { skipped: true, reason: 'no_whatsapp_config' };
  }
  const msg = 'أستاذ/ة ' + inputs.customer_name + '، سيتم التواصل معك خلال دقائق من سايكو ' +
              'بخصوص الوثيقة ' + inputs.policy_number + '. — سايكو 8001242002';
  try {
    const r = await fetch(inputs.whatsapp_api_url, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + inputs.whatsapp_token,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        to: inputs.customer_phone,
        type: 'template',
        template: {
          name: inputs.template_name || 'saico_precall_warmup',
          language: { code: 'ar' },
          components: [{ type: 'body', parameters: [
            { type: 'text', text: inputs.customer_name },
            { type: 'text', text: inputs.policy_number }
          ]}]
        },
        fallback_text: msg
      })
    });
    return { sent: r.ok, status: r.status, to: inputs.customer_phone };
  } catch (e) { return { sent: false, error: String(e) }; }
};
"""

SONDOS_CALL_CODE = """
export const code = async (inputs) => {
  if (!inputs.sondos_base_url || !inputs.sondos_api_key || !inputs.sondos_assistant_id ||
      String(inputs.sondos_base_url).startsWith('TODO')) {
    return { queued: false, error: 'missing_sondos_config' };
  }
  const payload = {
    assistant_id: inputs.sondos_assistant_id,
    phone_number: inputs.customer_phone,
    language: 'ar-SA',
    max_duration_sec: 420,
    metadata: { lead_id: inputs.lead_id, urgency: inputs.urgency },
    variables: {
      customer_name: inputs.customer_name,
      customer_phone: inputs.customer_phone,
      id_last4: inputs.id_last4,
      debt_type: inputs.debt_type,
      outstanding_amount: String(inputs.outstanding_amount),
      policy_number: inputs.policy_number,
      claim_number: inputs.claim_number,
      incident_date: inputs.incident_date,
      vehicle_plate: inputs.vehicle_plate,
      days_overdue: String(inputs.days_overdue),
      contact_attempt: String(inputs.contact_attempt),
      previous_promise: inputs.previous_promise,
      iban: inputs.iban,
      bank_name: inputs.bank_name,
      max_installments: String(inputs.max_installments),
      min_down_payment_pct: String(inputs.min_down_payment_pct),
      discount_authority_pct: String(inputs.discount_authority_pct),
      whatsapp_link: inputs.whatsapp_link,
      payment_link: inputs.payment_link,
      agent_transfer_number: inputs.agent_transfer_number,
      complaint_channel: inputs.complaint_channel,
      call_time_window: inputs.call_time_window,
      call_datetime: inputs.call_datetime,
      language: inputs.language
    }
  };
  try {
    const r = await fetch(inputs.sondos_base_url + '/v1/calls/outbound', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + inputs.sondos_api_key,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(payload)
    });
    const data = await r.json().catch(() => ({}));
    return {
      queued: r.ok,
      status: r.status,
      call_id: data.call_id || data.id || null,
      lead_id: inputs.lead_id,
      urgency: inputs.urgency
    };
  } catch (e) { return { queued: false, error: String(e) }; }
};
"""

OUTCOME_PARSE_CODE = """
export const code = async (inputs) => {
  const b = inputs.body || {};
  const o = (b.outcome || '').toLowerCase();
  const allowed = ['paid_full','paid_partial','plan_agreed','promise_to_pay',
                   'callback_scheduled','transferred','no_contact','refused'];
  const normalized = allowed.includes(o) ? o : 'no_contact';
  return {
    call_id: b.call_id || '',
    lead_id: b.customer_id || b.lead_id || '',
    policy_number: b.policy_number || '',
    customer_name: b.customer_name || '',
    customer_phone: b.customer_phone || '',
    outcome: normalized,
    committed_amount: Number(b.committed_amount || 0),
    committed_date: b.committed_date || '',
    ladder_reached: Number(b.commitment_ladder_reached || 0),
    objections: Array.isArray(b.objections_raised) ? b.objections_raised.join(' | ') : '',
    duration_sec: Number(b.call_duration_sec || 0),
    transcript_url: b.transcript_url || '',
    iban: inputs.iban || '',
    bank_name: inputs.bank_name || '',
    payment_link: inputs.payment_link || '',
    recorded_at: new Date().toISOString()
  };
};
"""

WA_PAYMENT_CODE = """
export const code = async (inputs) => {
  if (!inputs.whatsapp_api_url || !inputs.whatsapp_token ||
      String(inputs.whatsapp_api_url).startsWith('TODO')) {
    return { skipped: true, reason: 'no_whatsapp_config' };
  }
  const body = 'أستاذ/ة ' + inputs.customer_name + '، شكراً لوقتك. ' +
               'الالتزام: ' + inputs.outcome + ' بمبلغ ' + inputs.committed_amount + ' ريال' +
               (inputs.committed_date ? ' بتاريخ ' + inputs.committed_date : '') + '\\n' +
               'للسداد: آيبان ' + inputs.iban + ' - ' + inputs.bank_name +
               (inputs.payment_link ? '\\nرابط مباشر: ' + inputs.payment_link : '') +
               '\\nالمرجع: ' + inputs.policy_number + ' - سايكو 8001242002';
  try {
    const r = await fetch(inputs.whatsapp_api_url, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + inputs.whatsapp_token,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        to: inputs.customer_phone,
        type: 'text',
        text: { body }
      })
    });
    return { sent: r.ok, status: r.status };
  } catch (e) { return { sent: false, error: String(e) }; }
};
"""


def trigger_payload():
    return {
        "display_name": "SAICO Collection — Trigger v1.0 (CONFIGURE BEFORE USE)",
        "project_id": PROJECT_ID,
        "connection_ids": {"gmail": GMAIL_EXT, "google-sheets": SHEETS_EXT},
        "steps": [
            {
                "type": "CODE",
                "display_name": "إثراء الليد + حساب الضغط",
                "code": ENRICH_CODE,
                "code_input": {
                    "body": "{{trigger['body']}}",
                    "iban": IBAN,
                    "bank_name": BANK,
                    "max_installments": 6,
                    "min_down_payment_pct": 30,
                    "discount_authority_pct": 10,
                    "whatsapp_link": "",
                    "payment_link": PAYMENT_LINK,
                    "agent_transfer_number": TRANSFER_NUM,
                    "complaint_channel": COMPLAINT_CHANNEL,
                    "call_time_window": "09:00-21:00",
                },
            },
            {
                "type": "PIECE",
                "piece": "google-sheets",
                "action_name": "insert_row",
                "display_name": "تسجيل محاولة التحصيل",
                "input": {
                    "spreadsheetId": SHEET_PLACEHOLDER,
                    "sheetId": 0,
                    "first_row_headers": True,
                    "values": {
                        "A": "{{step_1['lead_id']}}",
                        "B": "{{step_1['customer_name']}}",
                        "C": "{{step_1['customer_phone']}}",
                        "D": "{{step_1['policy_number']}}",
                        "E": "{{step_1['outstanding_amount']}}",
                        "F": "{{step_1['days_overdue']}}",
                        "G": "{{step_1['contact_attempt']}}",
                        "H": "{{step_1['urgency']}}",
                        "I": "{{step_1['call_datetime']}}",
                        "J": "queued",
                    },
                },
            },
            {
                "type": "CODE",
                "display_name": "وتساب تمهيدي",
                "code": WA_WARMUP_CODE,
                "code_input": {
                    "whatsapp_api_url": WA_URL,
                    "whatsapp_token": WA_TOKEN,
                    "template_name": "saico_precall_warmup",
                    "customer_name": "{{step_1['customer_name']}}",
                    "customer_phone": "{{step_1['customer_phone']}}",
                    "policy_number": "{{step_1['policy_number']}}",
                },
            },
            {
                "type": "CODE",
                "display_name": "إطلاق مكالمة سندس",
                "code": SONDOS_CALL_CODE,
                "code_input": {
                    "sondos_base_url": SONDOS_BASE,
                    "sondos_api_key": SONDOS_KEY,
                    "sondos_assistant_id": SONDOS_ASSIST,
                    "lead_id": "{{step_1['lead_id']}}",
                    "urgency": "{{step_1['urgency']}}",
                    "customer_name": "{{step_1['customer_name']}}",
                    "customer_phone": "{{step_1['customer_phone']}}",
                    "id_last4": "{{step_1['id_last4']}}",
                    "debt_type": "{{step_1['debt_type']}}",
                    "outstanding_amount": "{{step_1['outstanding_amount']}}",
                    "policy_number": "{{step_1['policy_number']}}",
                    "claim_number": "{{step_1['claim_number']}}",
                    "incident_date": "{{step_1['incident_date']}}",
                    "vehicle_plate": "{{step_1['vehicle_plate']}}",
                    "days_overdue": "{{step_1['days_overdue']}}",
                    "contact_attempt": "{{step_1['contact_attempt']}}",
                    "previous_promise": "{{step_1['previous_promise']}}",
                    "iban": "{{step_1['iban']}}",
                    "bank_name": "{{step_1['bank_name']}}",
                    "max_installments": "{{step_1['max_installments']}}",
                    "min_down_payment_pct": "{{step_1['min_down_payment_pct']}}",
                    "discount_authority_pct": "{{step_1['discount_authority_pct']}}",
                    "whatsapp_link": "{{step_1['whatsapp_link']}}",
                    "payment_link": "{{step_1['payment_link']}}",
                    "agent_transfer_number": "{{step_1['agent_transfer_number']}}",
                    "complaint_channel": "{{step_1['complaint_channel']}}",
                    "call_time_window": "{{step_1['call_time_window']}}",
                    "call_datetime": "{{step_1['call_datetime']}}",
                    "language": "{{step_1['language']}}",
                },
            },
        ],
    }


def outcome_payload():
    plan_wh_input = {
        "whatsapp_api_url": WA_URL,
        "whatsapp_token": WA_TOKEN,
        "customer_name": "{{step_1['customer_name']}}",
        "customer_phone": "{{step_1['customer_phone']}}",
        "policy_number": "{{step_1['policy_number']}}",
        "outcome": "{{step_1['outcome']}}",
        "committed_amount": "{{step_1['committed_amount']}}",
        "committed_date": "{{step_1['committed_date']}}",
        "iban": "{{step_1['iban']}}",
        "bank_name": "{{step_1['bank_name']}}",
        "payment_link": "{{step_1['payment_link']}}",
    }

    paid_action = {
        "type": "PIECE",
        "piece": "gmail",
        "action_name": "send_email",
        "display_name": "تأكيد سداد للخزينة",
        "input": {
            "receiver": [PAID_EMAIL],
            "subject": "سداد كامل: {{step_1['customer_name']}} — {{step_1['policy_number']}}",
            "body_type": "plain_text",
            "body": (
                "تم السداد الكامل عبر المكالمة.\n"
                "العميل: {{step_1['customer_name']}}\n"
                "الوثيقة: {{step_1['policy_number']}}\n"
                "المبلغ: {{step_1['committed_amount']}} ريال\n"
                "معرف المكالمة: {{step_1['call_id']}}\n"
                "التسجيل: {{step_1['transcript_url']}}\n\n— سايكو"
            ),
            "draft": False,
        },
    }

    plan_action = {
        "type": "CODE",
        "display_name": "وتساب تفاصيل السداد",
        "code": WA_PAYMENT_CODE,
        "code_input": plan_wh_input,
    }

    callback_action = {
        "type": "PIECE",
        "piece": "google-sheets",
        "action_name": "insert_row",
        "display_name": "حفظ موعد رد الاتصال",
        "input": {
            "spreadsheetId": SHEET_PLACEHOLDER,
            "sheetId": 0,
            "first_row_headers": True,
            "values": {
                "A": "{{step_1['lead_id']}}",
                "B": "{{step_1['customer_name']}}",
                "C": "{{step_1['customer_phone']}}",
                "D": "{{step_1['policy_number']}}",
                "E": "{{step_1['committed_date']}}",
                "F": "callback_scheduled",
                "G": "{{step_1['call_id']}}",
            },
        },
    }

    transfer_action = {
        "type": "PIECE",
        "piece": "gmail",
        "action_name": "send_email",
        "display_name": "تنبيه تحويل بشري عاجل",
        "input": {
            "receiver": [SUPERVISOR_EMAIL],
            "subject": "تحويل للبشري: {{step_1['customer_name']}} — {{step_1['policy_number']}}",
            "body_type": "plain_text",
            "body": (
                "تم تحويل المكالمة لمختص بشري.\n"
                "العميل: {{step_1['customer_name']}}\n"
                "الجوال: {{step_1['customer_phone']}}\n"
                "الوثيقة: {{step_1['policy_number']}}\n"
                "الاعتراضات: {{step_1['objections']}}\n"
                "التسجيل: {{step_1['transcript_url']}}\n\nيرجى الاتصال خلال ساعة."
            ),
            "draft": False,
        },
    }

    fallback_alert = {
        "type": "PIECE",
        "piece": "gmail",
        "action_name": "send_email",
        "display_name": "بلا التزام — تنبيه",
        "input": {
            "receiver": [SUPERVISOR_EMAIL],
            "subject": "بلا التزام: {{step_1['customer_name']}} — إعادة محاولة مطلوبة",
            "body_type": "plain_text",
            "body": (
                "المكالمة انتهت بلا التزام واضح.\n"
                "العميل: {{step_1['customer_name']}}\n"
                "الجوال: {{step_1['customer_phone']}}\n"
                "الوثيقة: {{step_1['policy_number']}}\n"
                "النتيجة: {{step_1['outcome']}}\n"
                "الاعتراضات: {{step_1['objections']}}\n"
                "التسجيل: {{step_1['transcript_url']}}\n\nيرجى جدولة محاولة ثانية."
            ),
            "draft": False,
        },
    }

    return {
        "display_name": "SAICO Collection — Outcome Router v1.0 (CONFIGURE BEFORE USE)",
        "project_id": PROJECT_ID,
        "connection_ids": {"gmail": GMAIL_EXT, "google-sheets": SHEETS_EXT},
        "steps": [
            {
                "type": "CODE",
                "display_name": "قراءة نتيجة سندس",
                "code": OUTCOME_PARSE_CODE,
                "code_input": {
                    "body": "{{trigger['body']}}",
                    "iban": IBAN,
                    "bank_name": BANK,
                    "payment_link": PAYMENT_LINK,
                },
            },
            {
                "type": "PIECE",
                "piece": "google-sheets",
                "action_name": "insert_row",
                "display_name": "تسجيل نتيجة المكالمة",
                "input": {
                    "spreadsheetId": SHEET_PLACEHOLDER,
                    "sheetId": 0,
                    "first_row_headers": True,
                    "values": {
                        "A": "{{step_1['call_id']}}",
                        "B": "{{step_1['lead_id']}}",
                        "C": "{{step_1['customer_name']}}",
                        "D": "{{step_1['policy_number']}}",
                        "E": "{{step_1['outcome']}}",
                        "F": "{{step_1['committed_amount']}}",
                        "G": "{{step_1['committed_date']}}",
                        "H": "{{step_1['ladder_reached']}}",
                        "I": "{{step_1['duration_sec']}}",
                        "J": "{{step_1['objections']}}",
                        "K": "{{step_1['transcript_url']}}",
                        "L": "{{step_1['recorded_at']}}",
                    },
                },
            },
            {
                "type": "ROUTER",
                "display_name": "توجيه حسب نتيجة المكالمة",
                "branches": [
                    {
                        "name": "سداد كامل",
                        "conditions": [[{
                            "operator": "TEXT_EXACTLY_MATCHES",
                            "first_value": "{{step_1['outcome']}}",
                            "second_value": "paid_full",
                        }]],
                        "actions": [paid_action],
                    },
                    {
                        "name": "خطة أو وعد",
                        "conditions": [
                            [{"operator": "TEXT_CONTAINS",
                              "first_value": "{{step_1['outcome']}}",
                              "second_value": "plan_agreed"}],
                            [{"operator": "TEXT_CONTAINS",
                              "first_value": "{{step_1['outcome']}}",
                              "second_value": "promise_to_pay"}],
                            [{"operator": "TEXT_CONTAINS",
                              "first_value": "{{step_1['outcome']}}",
                              "second_value": "paid_partial"}],
                        ],
                        "actions": [plan_action],
                    },
                    {
                        "name": "رد اتصال مجدول",
                        "conditions": [[{
                            "operator": "TEXT_EXACTLY_MATCHES",
                            "first_value": "{{step_1['outcome']}}",
                            "second_value": "callback_scheduled",
                        }]],
                        "actions": [callback_action],
                    },
                    {
                        "name": "تحويل بشري",
                        "conditions": [[{
                            "operator": "TEXT_EXACTLY_MATCHES",
                            "first_value": "{{step_1['outcome']}}",
                            "second_value": "transferred",
                        }]],
                        "actions": [transfer_action],
                    },
                    {
                        "name": "بلا التزام",
                        "branch_type": "FALLBACK",
                        "actions": [fallback_alert],
                    },
                ],
            },
        ],
    }


def deploy(name: str, payload: dict, client: httpx.Client) -> dict:
    print(f"\n[deploy] {name} ...")
    t0 = time.time()
    r = client.post(f"{ORCH}/v2/build-complex", json=payload, timeout=180)
    dt = time.time() - t0
    print(f"  HTTP {r.status_code} in {dt:.1f}s")
    try:
        out = r.json()
    except Exception:
        out = {"raw": r.text[:600]}
    if r.status_code == 200:
        print(f"  flow_id    : {out.get('flow_id')}")
        print(f"  webhook_url: {out.get('webhook_url')}")
        steps = out.get("steps") or []
        print(f"  steps      : {len(steps)}")
        pub = out.get("publish") or {}
        print(f"  publish    : state={pub.get('version_state')} match={pub.get('published_match')}")
    else:
        print(f"  ERROR: {json.dumps(out, ensure_ascii=False)[:600]}")
    return out


def main():
    headers = {"Accept": "application/json", "User-Agent": "siyadah-deploy/1.0"}
    with httpx.Client(headers=headers, timeout=180, follow_redirects=True) as client:
        hr = client.get(f"{ORCH}/health")
        print(f"[health] HTTP {hr.status_code} body={hr.text[:120]!r}")
        h = hr.json()
        print(f"[health] orch={h.get('status')} ap={h.get('activepieces')} ver={h.get('version')}")
        if h.get("status") != "healthy":
            sys.exit("orchestrator not healthy")

        a = deploy("SAICO Trigger", trigger_payload(), client)
        b = deploy("SAICO Outcome", outcome_payload(), client)

        print("\n=== SUMMARY ===")
        print(json.dumps({
            "trigger_flow_id": a.get("flow_id"),
            "trigger_webhook_url": a.get("webhook_url"),
            "outcome_flow_id": b.get("flow_id"),
            "outcome_webhook_url": b.get("webhook_url"),
            "project_id": PROJECT_ID,
        }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
