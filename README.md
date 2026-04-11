<div align="center">

# سيادة Orchestrator | Siyadah Orchestrator

### v7.1.0 — Proactive Intelligence Edition

**نظام تشغيل SaaS مستقل بذكاء استباقي | Autonomous SaaS OS with Proactive Intelligence**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Async-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Redis](https://img.shields.io/badge/Redis-Sessions-DC382D?logo=redis&logoColor=white)](https://redis.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Deploy on Railway](https://img.shields.io/badge/Deploy-Railway-0B0D0E?logo=railway)](https://railway.app)

---

**سيادة** نظام تنسيق ذكي يربط وكلاء الذكاء الاصطناعي بمنصة [Activepieces](https://www.activepieces.com) لبناء ونشر وإدارة سلاسل الأتمتة — مع ذاكرة مؤسسية، امتصاص مواقع بالذكاء الاصطناعي، محرك اقتراحات قطاعي، **ومحرك ذكاء استباقي** يكتشف الفرص الضائعة وتنبيهات الصحة قبل أن يسأل العميل.

**Siyadah** is an intelligent orchestration engine bridging AI agents with [Activepieces](https://www.activepieces.com) to build, deploy, and manage automation flows — with institutional memory, AI-powered website absorption, sector-aware suggestions, **and a proactive intelligence engine** that discovers missed opportunities and health alerts before the client even asks.

</div>

---

## المحتويات | Table of Contents

<table>
<tr>
<td width="50%">

**العربية**
- [المعمارية](#المعمارية)
- [القدرات الأساسية](#القدرات-الأساسية)
- [الذكاء الاستباقي](#الذكاء-الاستباقي--proactive-intelligence)
- [البروتوكول الذهبي](#البروتوكول-الذهبي-golden-protocol-v5)
- [الذكاء التلقائي](#الذكاء-التلقائي-smart-auto-fill)
- [النبضة الشاملة](#النبضة-الشاملة-universal-pulse)
- [نقاط الوصول](#نقاط-الوصول-api-endpoints)
- [القوالب](#القوالب-templates)
- [السيناريوهات المتقدمة](#السيناريوهات-المتقدمة-presets)
- [تكامل MCP و SSE](#تكامل-mcp-و-sse)
- [الإعداد والتشغيل](#الإعداد-والتشغيل)
- [النشر](#النشر)

</td>
<td width="50%">

**English**
- [Architecture](#المعمارية)
- [Core Capabilities](#القدرات-الأساسية)
- [Proactive Intelligence](#الذكاء-الاستباقي--proactive-intelligence)
- [Golden Protocol](#البروتوكول-الذهبي-golden-protocol-v5)
- [Smart Auto-Fill](#الذكاء-التلقائي-smart-auto-fill)
- [Universal Pulse](#النبضة-الشاملة-universal-pulse)
- [API Endpoints](#نقاط-الوصول-api-endpoints)
- [Templates](#القوالب-templates)
- [Presets](#السيناريوهات-المتقدمة-presets)
- [MCP & SSE Integration](#تكامل-mcp-و-sse)
- [Setup](#الإعداد-والتشغيل)
- [Deployment](#النشر)

</td>
</tr>
</table>

---

## المعمارية

```
                        ┌──────────────────────┐
                        │  Claude / AI Agent /  │
                        │   SaaS Dashboard      │
                        └──────────┬───────────┘
                                   │  REST / MCP / SSE
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  Siyadah Orchestrator v7.1.0                         │
│                                                                      │
│  ┌────────────┐  ┌──────────────┐  ┌───────────────────────────────┐│
│  │  Templates  │  │   Presets    │  │     Smart Build Engine        ││
│  │  (8 ready)  │  │ (4 complex) │  │  Schema + Auto-Fill +         ││
│  │             │  │             │  │  Dropdown Intelligence        ││
│  └──────┬─────┘  └──────┬──────┘  └────────────┬──────────────────┘│
│         │               │                      │                    │
│         └───────────────┼──────────────────────┘                    │
│                         ▼                                           │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │              Golden Protocol v5 Pipeline                       │  │
│  │  IMPORT_FLOW → GET-verify → LOCK_AND_PUBLISH → ENABLE        │  │
│  │                    + Universal Pulse                           │  │
│  └───────────────────────────┬───────────────────────────────────┘  │
│                              │                                      │
│  ┌───────────────────────────┼───────────────────────────────────┐  │
│  │       Institutional Memory (Postgres)                         │  │
│  │  ProjectIdentity · KnowledgeAsset · AutonomousSetting         │  │
│  └───────────────────────────┼───────────────────────────────────┘  │
│                              │                                      │
│  ┌───────────────────────────┼───────────────────────────────────┐  │
│  │       Proactive Intelligence Engine                           │  │
│  │  Success Patterns × Identity → Opportunities + Health Scan    │  │
│  └───────────────────────────┼───────────────────────────────────┘  │
│                              │                                      │
│  ┌───────────────────────────┼───────────────────────────────────┐  │
│  │  MCP Dispatcher (16 tools) + SSE Real-Time + Redis Sessions   │  │
│  └───────────────────────────┼───────────────────────────────────┘  │
└──────────────────────────────┼──────────────────────────────────────┘
                               │  httpx.AsyncClient
                               ▼
                    ┌─────────────────────┐
                    │   Activepieces API   │
                    │  (Self-hosted / Cloud)│
                    └─────────────────────┘
```

---

## القدرات الأساسية

<table>
<tr>
<th>القدرة | Capability</th>
<th>الوصف بالعربية</th>
<th>English Description</th>
</tr>
<tr>
<td><strong>Async Engine</strong></td>
<td>مبني على <code>httpx.AsyncClient</code> مع timeout 120 ثانية</td>
<td>Built on <code>httpx.AsyncClient</code> with 120s read timeout</td>
</tr>
<tr>
<td><strong>Multi-Tenancy</strong></td>
<td>عزل كامل للبيانات عبر <code>project_id</code> — كل عميل في كون مستقل</td>
<td>Full data isolation via <code>project_id</code> — each client in its own universe</td>
</tr>
<tr>
<td><strong>Institutional Memory</strong></td>
<td>ذاكرة مؤسسية في Postgres: هوية المشروع، أصول المعرفة، إعدادات ذاتية</td>
<td>Postgres-backed memory: ProjectIdentity, KnowledgeAsset, AutonomousSetting</td>
</tr>
<tr>
<td><strong>AI Website Absorption</strong></td>
<td>امتصاص المواقع بالذكاء الاصطناعي: كشط → تحليل → استخلاص هوية + FAQs + نبرة</td>
<td>AI-powered scrape → analyze → extract identity + FAQs + tone of voice</td>
</tr>
<tr>
<td><strong>Proactive Intelligence</strong></td>
<td>مقارنة الهوية بأنماط النجاح + فحص صحة الفلوهات = اقتراحات استباقية</td>
<td>Identity × success patterns + flow health scan = proactive suggestions</td>
</tr>
<tr>
<td><strong>Sector-Aware Suggestions</strong></td>
<td>اقتراحات فلوهات مخصصة حسب القطاع (تجارة، صحة، تعليم، عقارات)</td>
<td>Sector-specific flow recommendations (E-commerce, Healthcare, Education, Real Estate)</td>
</tr>
<tr>
<td><strong>Smart Onboarding</strong></td>
<td>معاينة → تسجيل → تكوين ذاتي للإعدادات في خطوة واحدة</td>
<td>Preview → Register → Auto-configure settings in one step</td>
</tr>
<tr>
<td><strong>Golden Protocol v5</strong></td>
<td>خط أنابيب صارم: IMPORT → VERIFY → LOCK → ENABLE مع تحقق نهائي</td>
<td>Strict pipeline: IMPORT → VERIFY → LOCK → ENABLE with final confirmation</td>
</tr>
<tr>
<td><strong>Smart Auto-Fill</strong></td>
<td>ملء تلقائي للحقول المفقودة حسب النوع + Draft Guard</td>
<td>Type-aware auto-fill for missing fields + Draft Guard</td>
</tr>
<tr>
<td><strong>Universal Pulse</strong></td>
<td>نبضة تفعيل شاملة تضمن بيانات جاهزة من النبضة الأولى</td>
<td>Activation pulse ensuring ready data from the first pulse</td>
</tr>
<tr>
<td><strong>MCP + SSE</strong></td>
<td>16 أداة MCP + نقل SSE في الوقت الحقيقي مع جلسات Redis</td>
<td>16 MCP tools + real-time SSE transport with Redis-backed sessions</td>
</tr>
<tr>
<td><strong>Context-Aware Hints</strong></td>
<td>نظام <code>_hint</code> ذكي يقترح الخطوة التالية حسب حالة الذاكرة</td>
<td>Smart <code>_hint</code> system suggesting next action based on memory state</td>
</tr>
<tr>
<td><strong>8 Templates + 4 Presets</strong></td>
<td>من تنبيه إيميل إلى تفرّعات ROUTER+LOOP متداخلة</td>
<td>From email alert to nested ROUTER+LOOP combos</td>
</tr>
<tr>
<td><strong>600+ Pieces</strong></td>
<td>كتالوج كامل: Gmail, Slack, HubSpot, Sheets, Drive…</td>
<td>Full catalog: Gmail, Slack, HubSpot, Sheets, Drive…</td>
</tr>
</table>

---

## الذكاء الاستباقي | Proactive Intelligence

المحرك الاستباقي يجعل العميل يقول "كيف عرف النظام هذا؟" — يقارن حالة المشروع بأنماط النجاح المخزنة ويفحص صحة التشغيلات تلقائياً.

The proactive engine makes clients say "how did the system know?" — it compares project state against stored success patterns and auto-scans flow health.

### `GET /v2/logic/proactive-suggestions`

```
┌─────────────────────────────┐
│   ProjectIdentity (Mem)     │──┐
│   • sector                  │  │    ┌───────────────────────────┐
│   • language                │  ├──→ │  Pattern Matcher          │
│   • business_description    │  │    │  identity × patterns →    │
│   • absorbed_at             │  │    │  missed opportunities     │
├─────────────────────────────┤  │    └─────────────┬─────────────┘
│   KnowledgeAsset (Mem)      │──┘                  │
│   • faqs                    │                     ▼
│   • tone_of_voice           │       ┌──────────────────────────┐
│   • brand_keywords          │       │  OPPORTUNITY hints       │
├─────────────────────────────┤       │  WARNING hints           │
│   Success Patterns          │──────→│  SUCCESS_STORY hints     │
│   (built-in + Mem custom)   │       │  INFO hints              │
├─────────────────────────────┤       └──────────────────────────┘
│   Flow Runs (AP API)        │──┐
│   last 10 runs per flow     │  │    ┌───────────────────────────┐
│                             │  └──→ │  Health Check Loop        │
│                             │       │  failure > 50% → WARNING  │
└─────────────────────────────┘       └───────────────────────────┘
```

### مخطط الإشارة الذكية | Smart Hint Schema

كل `_hint` يرجع ككائن منظم:

Every `_hint` returns as a structured object:

```json
{
  "type": "OPPORTUNITY | WARNING | SUCCESS_STORY | INFO",
  "message": "Human-readable insight",
  "meta": {
    "pattern_id": "faq_intelligence_gap",
    "related_template": "gmail_autoresponder",
    "flow_id": "...",
    "flows_in_alarm": 2
  }
}
```

| النوع | Type | اللون المقترح | الوصف | Description |
|---|---|---|---|---|
| `OPPORTUNITY` | Opportunity | Amber | فرصة ضائعة — نمط نجاح لم يُتبنَّ بعد | Missed opportunity — unadopted success pattern |
| `WARNING` | Warning | Red | نسبة فشل الفلو > 50% في آخر التشغيلات | Flow failure rate > 50% in recent runs |
| `SUCCESS_STORY` | Success Story | Green | قصة نجاح من أقران القطاع للتحفيز | Peer success story for motivation |
| `INFO` | Info | Blue | لا إنذارات — النظام سليم | No alerts — system healthy |

### مثال استجابة | Example Response

```json
{
  "project_id": "client-xyz",
  "language": "ar",
  "missed_opportunities": [
    {
      "pattern_id": "faq_intelligence_gap",
      "title": "طبقة الأسئلة الشائعة مفقودة",
      "description": "الشركات المشابهة خزّنت أسئلة شائعة في الذاكرة...",
      "related_template": "gmail_autoresponder",
      "hint_type": "OPPORTUNITY",
      "_hint": { "type": "OPPORTUNITY", "message": "..." }
    }
  ],
  "flow_health_alerts": [
    {
      "flow_id": "abc123",
      "flow_name": "Lead Capture",
      "failure_rate": 0.7,
      "hint_type": "WARNING",
      "_hint": { "type": "WARNING", "message": "Flow «Lead Capture» failed 7/10..." }
    }
  ],
  "success_stories": [
    {
      "pattern_id": "faq_intelligence_gap",
      "hint_type": "SUCCESS_STORY",
      "message": "Teams that ship FAQ-backed automations typically cut first-response latency sharply."
    }
  ],
  "_hint": { "type": "WARNING", "message": "..." }
}
```

---

## البروتوكول الذهبي | Golden Protocol v5

كل فلو يُبنى ويُنشر عبر خط أنابيب صارم يضمن نجاح النشر بنسبة 100%:

Every flow is built and deployed through a strict pipeline ensuring 100% deployment success:

```
 ① CREATE_FLOW          إنشاء الفلو الفارغ | Create empty flow
        │
 ② IMPORT_FLOW          استيراد البنية الكاملة (trigger + actions) | Import full structure
        │
 ③ GET-verify           التحقق من البنية عبر GET | Verify structure via GET
        │
 ④ LOCK_AND_PUBLISH     قفل ونشر | Lock and publish
        │
 ⑤ ENABLE               تفعيل + تحقق نهائي (status=ENABLED) | Enable + final confirmation
        │
 ⑥ UNIVERSAL PULSE      نبضة اختبار ذكية | Smart test pulse
```

> **القاعدة الذهبية**: لا تثق بـ HTTP 200 وحده — دائماً تحقق بـ GET بعد العمليات الحرجة.
>
> **Golden Rule**: Never trust HTTP 200 alone — always verify with GET after critical operations.

---

## الذكاء التلقائي | Smart Auto-Fill

عند بناء أي فلو، المحرك يملأ الحقول المطلوبة المفقودة تلقائياً حسب نوعها:

When building any flow, the engine auto-fills missing required fields based on their type:

| النوع | Type | القيمة الافتراضية | Default Value |
|---|---|---|---|
| `BOOLEAN` | Boolean | `False` | `False` |
| `NUMBER` | Number | `0` | `0` |
| `ARRAY` | Array | `[]` | `[]` |
| `STATIC_DROPDOWN` | Static Dropdown | `options[0].value` | `options[0].value` |
| `DROPDOWN` | Dynamic Dropdown | `options[0].value` | `options[0].value` |
| أخرى | Other | `"Siyadah Auto-Fill"` | `"Siyadah Auto-Fill"` |

**Draft Guard** — طبقة حماية تحقن حقول BOOLEAN المعروفة تلقائياً لمنع حالة DRAFT.

**Draft Guard** — auto-injects known BOOLEAN fields to prevent flows from staying in DRAFT state.

---

## النبضة الشاملة | Universal Pulse

```json
{
  "event": "Siyadah_Activation",
  "status": "Success",
  "timestamp": "2026-04-11T...",
  "receiver": ["test@siyadah.ai"],
  "subject": "سيادة — تجربة تفعيل ناجحة",
  "body": "تهانينا! نظام الأتمتة الخاص بك يعمل الآن بكفاءة.",
  "values": {"A": "بيانات تجريبية", "B": "نجاح التفعيل"},
  "email": { "..." },
  "row": { "..." },
  "customer": { "..." },
  "order": { "..." }
}
```

---

## نقاط الوصول | API Endpoints

### النظام | System

| Endpoint | Method | الوظيفة | Description |
|---|---|---|---|
| `/` | `GET` | الحالة والإصدار | Status & version |
| `/health` | `GET` | فحص الاتصال بـ Activepieces | Activepieces connectivity check |

### البناء والنشر | Build & Deploy

| Endpoint | Method | الوظيفة | Description |
|---|---|---|---|
| `/v2/build-and-deploy` | `POST` | بناء من قالب | Build from template |
| `/v2/build-dynamic` | `POST` | بناء مخصص بأي أدوات | Custom build with any pieces |
| `/v2/build-router` | `POST` | بناء فلو مع تفرّع | Build flow with branching |
| `/v2/build-loop` | `POST` | بناء فلو مع تكرار | Build flow with looping |
| `/v2/build-complex` | `POST` | بناء أي مزيج (ROUTER+LOOP+CODE+PIECE) | Build any combo |
| `/v2/build-preset` | `POST` | بناء من سيناريو جاهز | Build from preset |
| `/v2/build-smart` | `POST` | بناء ذكي مع Schema Validation | Smart build with schema validation |
| `/v2/validate-flow` | `POST` | التحقق قبل البناء (Dry Run) | Pre-build validation (dry run) |

### إدارة الفلوات | Flow Management

| Endpoint | Method | الوظيفة | Description |
|---|---|---|---|
| `/v2/flows/{id}` | `PATCH` | تفعيل / تعطيل / حذف | Enable / disable / delete |
| `/v2/flows/{id}/reimport` | `POST` | تحديث فلو موجود | Update existing flow |
| `/v2/flows/{id}/diagnose` | `GET` | تشخيص بنية الفلو | Diagnose flow structure |
| `/v2/test-webhook/{id}` | `POST` | اختبار عبر webhook | Test via webhook |
| `/v2/client-status` | `GET` | لوحة حالة شاملة (فلوهات + تشغيلات + اتصالات) | Full dashboard |

### الذاكرة المؤسسية والهوية | Institutional Memory & Identity

| Endpoint | Method | الوظيفة | Description |
|---|---|---|---|
| `/v2/project/register` | `POST` | تسجيل مشروع في الذاكرة | Register project in memory |
| `/v2/project/{id}/memory` | `GET` | استخراج كامل الذاكرة | Full memory dump |
| `/v2/project/{id}/hint` | `GET` | إشارة ذكية حسب اكتمال الهوية | Smart hint based on identity completeness |
| `/v2/identity/ingest` | `POST` | امتصاص موقع (AI scrape → persist) | Absorb website (AI scrape → persist) |
| `/v2/saas/register` | `POST` | تسجيل SaaS ذكي (preview → register → auto-config) | Smart SaaS onboarding |

### الذكاء الاستباقي والاقتراحات | Proactive Intelligence & Suggestions

| Endpoint | Method | الوظيفة | Description |
|---|---|---|---|
| `/v2/logic/suggest` | `POST` | اقتراحات فلوهات حسب القطاع | Sector-aware flow suggestions |
| `/v2/logic/proactive-suggestions` | `GET` | **الفرص الضائعة + تنبيهات الصحة + قصص النجاح** | **Missed opportunities + health alerts + success stories** |

### الاستعلام | Query

| Endpoint | Method | الوظيفة | Description |
|---|---|---|---|
| `/templates` | `GET` | عرض الفلوات الحالية | List current flows |
| `/connections` | `GET` | عرض الاتصالات | List connections |
| `/pieces/{name}` | `GET` | مواصفات أداة | Piece specification |
| `/operators` | `GET` | قائمة شروط الـ ROUTER | Router condition operators |
| `/v2/templates` | `GET` | القوالب المتاحة | Available templates |
| `/v2/presets` | `GET` | السيناريوهات المتقدمة | Complex presets |
| `/v2/available-pieces` | `GET` | كتالوج 600+ أداة | 600+ pieces catalog |
| `/v2/pieces/{name}/schema` | `GET` | مواصفات تفصيلية لأداة | Detailed piece schema |

### إدارة الاتصالات | Connection Management

| Endpoint | Method | الوظيفة | Description |
|---|---|---|---|
| `/v2/connect` | `POST` | إنشاء اتصال جديد | Create new connection |
| `/v2/connections/health` | `GET` | فحص صحة جميع الاتصالات | Health check all connections |
| `/v2/connections/{id}/test` | `POST` | اختبار اتصال محدد | Test specific connection |
| `/v2/connections/{id}` | `DELETE` | حذف اتصال (مع حماية) | Delete connection (with guard) |

### MCP و SSE | MCP & SSE

| Endpoint | Method | الوظيفة | Description |
|---|---|---|---|
| `/v2/mcp/tools` | `GET` | قائمة أدوات MCP (16 أداة) | MCP tools list (16 tools) |
| `/v2/mcp/execute` | `POST` | تنفيذ أداة MCP | Execute MCP tool |
| `/v2/mcp/proxy` | `POST` | بروكسي لـ AP MCP Server | Proxy to AP MCP Server |
| `/v2/mcp/sse` | `GET` | فتح قناة SSE (جلسة Redis) | Open SSE channel (Redis session) |
| `/v2/mcp/messages/{session}` | `POST` | إرسال أوامر عبر SSE | Send commands via SSE |
| `/v2/create-project` | `POST` | إنشاء مشروع Activepieces | Create Activepieces project |

---

## القوالب | Templates

8 قوالب جاهزة للاستخدام الفوري | 8 ready-to-use templates:

| # | الاسم | Name | الوصف | Description |
|---|---|---|---|---|
| 1 | `webhook_to_email` | Email Alert | تنبيه إيميل فوري | Instant email notification |
| 2 | `webhook_to_sheet` | Sheet Logger | حفظ بيانات في جدول | Save data to spreadsheet |
| 3 | `webhook_to_sheet_and_email` | Sheet + Email | حفظ + تنبيه | Save + notify |
| 4 | `support_auto_reply` | Support Ticket | رد تلقائي + تذكرة دعم | Auto-reply + support ticket |
| 5 | `marketing_welcome` | Welcome Flow | ترحيب مشترك جديد | New subscriber welcome |
| 6 | `ops_log_report` | Ops Logger | تسجيل عملية + تقرير | Operation log + report |
| 7 | `lead_notify_and_confirm` | Full Lead System | نظام ليدات كامل | Complete lead system |
| 8 | `scheduled_report` | Daily Report | تقرير يومي مجدول | Scheduled daily report |

---

## السيناريوهات المتقدمة | Presets

4 سيناريوهات معقدة تجمع بين ROUTER و LOOP | 4 complex presets combining ROUTER and LOOP:

| الاسم | Name | النوع | Type | الوصف | Description |
|---|---|---|---|---|---|
| `lead_routing` | Lead Router | ROUTER | ROUTER | توجيه ليدات حسب وجود الإيميل | Route leads by email presence |
| `bulk_email` | Bulk Email | LOOP | LOOP | إيميلات جماعية — تكرار + إرسال | Mass email — loop + send |
| `smart_followup` | Smart Follow-up | ROUTER + LOOP | ROUTER + LOOP | تصنيف بالسكور + مهام متابعة | Score classification + follow-up |
| `router_loop_combo` | Router+Loop Combo | ROUTER + 2×LOOP | ROUTER + 2×LOOP | توجيه + تكرار داخل كل فرع | Route + loop inside each branch |

---

## تكامل MCP و SSE

### أدوات MCP | MCP Tools

واجهة متوافقة مع Claude وأي AI Agent يدعم بروتوكول MCP | MCP-compatible tool interface:

<table>
<tr><th>#</th><th>الأداة | Tool</th><th>الوصف | Description</th></tr>
<tr><td>1</td><td><code>check_system_health</code></td><td>فحص حالة النظام | System health check</td></tr>
<tr><td>2</td><td><code>get_client_status</code></td><td>لوحة حالة العميل | Client dashboard</td></tr>
<tr><td>3</td><td><code>list_templates</code></td><td>عرض القوالب الـ 8 | List 8 templates</td></tr>
<tr><td>4</td><td><code>list_presets</code></td><td>عرض السيناريوهات المعقدة | List complex presets</td></tr>
<tr><td>5</td><td><code>list_available_pieces</code></td><td>كتالوج 600+ أداة | 600+ pieces catalog</td></tr>
<tr><td>6</td><td><code>get_piece_schema</code></td><td>مواصفات أداة تفصيلية | Detailed piece schema</td></tr>
<tr><td>7</td><td><code>build_from_template</code></td><td>بناء من قالب | Build from template</td></tr>
<tr><td>8</td><td><code>build_dynamic_flow</code></td><td>بناء مخصص | Custom build</td></tr>
<tr><td>9</td><td><code>build_from_preset</code></td><td>بناء من سيناريو | Build from preset</td></tr>
<tr><td>10</td><td><code>validate_flow</code></td><td>التحقق بدون نشر | Validate without deploying</td></tr>
<tr><td>11</td><td><code>test_webhook</code></td><td>إرسال بيانات تجريبية | Send test data</td></tr>
<tr><td>12</td><td><code>manage_flow</code></td><td>تفعيل / تعطيل / حذف | Enable / disable / delete</td></tr>
<tr><td>13</td><td><code>diagnose_flow</code></td><td>تشخيص بنية الفلو | Diagnose flow structure</td></tr>
<tr><td>14</td><td><code>update_flow</code></td><td>تحديث فلو موجود | Update existing flow</td></tr>
<tr><td>15</td><td><code>ingest_website</code></td><td>امتصاص موقع بالذكاء الاصطناعي | AI website absorption</td></tr>
<tr><td>16</td><td><code>get_institutional_memory</code></td><td>قراءة الذاكرة المؤسسية | Read institutional memory</td></tr>
</table>

### SSE (Server-Sent Events)

نقل في الوقت الحقيقي مع جلسات Redis (مع fallback للذاكرة):

Real-time transport with Redis-backed sessions (in-memory fallback):

```
Client                          Orchestrator              Redis
  │                                 │                       │
  │── GET /v2/mcp/sse ────────────→│                       │
  │←── SSE: session_id ───────────│── save session ──────→│
  │                                 │                       │
  │── POST /messages/{session} ──→│                       │
  │←── SSE: tool result ──────────│                       │
```

---

## الإعداد والتشغيل

### المتطلبات | Prerequisites

- Python 3.10+
- نسخة Activepieces (Self-hosted أو Cloud)
- PostgreSQL (للذاكرة المؤسسية)
- Redis (اختياري — لجلسات SSE)
- مفاتيح Anthropic + Firecrawl (اختياري — لامتصاص المواقع)

### التثبيت | Installation

```bash
git clone https://github.com/ALKHANFAR/siyadah-orchestrator.git
cd siyadah-orchestrator

cp .env.example .env
# عدّل القيم في .env | Edit values in .env

pip install -r requirements.txt

uvicorn main:app --host 0.0.0.0 --port 8000
```

### متغيرات البيئة | Environment Variables

| المتغير | مطلوب | الوصف | Description |
|---|---|---|---|
| `AP_BASE_URL` | **نعم** | رابط Activepieces | Activepieces instance URL |
| `AP_EMAIL` | **نعم** | إيميل تسجيل الدخول | Login email |
| `AP_PASSWORD` | **نعم** | كلمة المرور | Password |
| `AP_PROJECT_ID` | **نعم** | معرّف المشروع الافتراضي | Default project ID |
| `GMAIL_CONNECTION_ID` | **نعم** | معرّف اتصال Gmail | Gmail connection ID |
| `SHEETS_CONNECTION_ID` | **نعم** | معرّف اتصال Sheets | Sheets connection ID |
| `DATABASE_URL` | **نعم** | رابط Postgres | Postgres connection URL |
| `REDIS_URL` | اختياري | رابط Redis (SSE sessions) | Redis URL for SSE |
| `ORCHESTRATOR_API_KEY` | اختياري | مفتاح حماية `/v2/` | V2 API key |
| `ORCHESTRATOR_URL` | اختياري | عنوان المحرك العام | Public orchestrator URL |
| `ANTHROPIC_API_KEY` | اختياري | مفتاح Claude (لامتصاص المواقع) | Claude key for absorption |
| `FIRECRAWL_API_KEY` | اختياري | مفتاح Firecrawl (كشط المواقع) | Firecrawl scraping key |
| `AP_MCP_SERVER_URL` | اختياري | عنوان بروكسي MCP | MCP proxy URL |
| `AP_MCP_TOKEN` | اختياري | توكن MCP | MCP token |
| `ORCHESTRATOR_HTTPX_TIMEOUT` | اختياري | مهلة HTTP بالثواني (افتراضي: 120) | HTTP timeout (default: 120) |

### الحماية | Security

عند تعيين `ORCHESTRATOR_API_KEY`، جميع endpoints تحت `/v2/` تتطلب هيدر:

When `ORCHESTRATOR_API_KEY` is set, all `/v2/` endpoints require the header:

```
X-API-Key: your-api-key
```

---

## النشر | Deployment

### Railway (الموصى به | Recommended)

```
web: uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
```

```bash
git push origin main
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## البنية الداخلية | Project Structure

```
siyadah-orchestrator/
├── main.py              # المحرك الرئيسي (4100+ سطر) | Main engine
├── models.py            # نماذج Postgres (SQLAlchemy) | Postgres models
├── database.py          # اتصال قاعدة البيانات | Database connection
├── ingestion.py         # محرك امتصاص المواقع | Website absorption engine
├── mcp_sse.py           # نقل SSE + جلسات Redis | SSE transport + Redis sessions
├── requirements.txt     # المتطلبات | Dependencies
├── Procfile             # تهيئة Railway | Railway config
├── railway.json         # إعدادات Railway | Railway settings
├── .env.example         # نموذج متغيرات البيئة | Env template
└── README.md            # هذه الوثيقة | This document
```

---

## القواعد المعمارية | Architectural Rules

| القاعدة | Rule | الوصف | Description |
|---|---|---|---|
| `propertySettings: {}` | Mandatory | في كل step settings | In every step settings |
| `IMPORT_FLOW` | Trusted Method | لإنشاء/تحديث بنية الفلو | For creating/updating flow structure |
| `GET` after critical ops | Verification | لا تثق بـ 200 وحده | Never trust 200 alone |
| `auth` format | Connection binding | `{{connections['externalId']}}` | For binding connections |
| `LOCK_AND_PUBLISH` | State transition | ثم `CHANGE_STATUS` إذا لزم | Then `CHANGE_STATUS` if needed |
| Multi-tenant isolation | Data safety | كل بيانات تحت `project_id` | All data scoped to `project_id` |
| Proactive `_hint` schema | UI contract | نوع + رسالة + meta | type + message + meta |

---

## التقنيات | Tech Stack

| التقنية | Technology | الدور | Role |
|---|---|---|---|
| **FastAPI** | Web Framework | الإطار الأساسي | Core framework |
| **httpx** | HTTP Client | العميل غير المتزامن | Async HTTP client |
| **SQLAlchemy 2.0** | ORM | نماذج الذاكرة المؤسسية | Institutional memory models |
| **asyncpg** | PostgreSQL Driver | اتصال Postgres غير متزامن | Async Postgres driver |
| **Redis (aioredis)** | Session Store | جلسات SSE | SSE sessions |
| **Pydantic v2** | Validation | التحقق من البيانات | Data validation |
| **uvicorn** | ASGI Server | خادم ASGI | ASGI server |
| **Anthropic SDK** | AI Analysis | تحليل المواقع | Website analysis |
| **Firecrawl** | Web Scraping | كشط المواقع | Website scraping |
| **sse-starlette** | SSE Transport | نقل أحداث الوقت الحقيقي | Real-time event transport |

---

<div align="center">

**سيادة — نظام التشغيل الذكي للأتمتة**

**Siyadah — The Intelligent Automation OS**

صُنع بعناية لتمكين الأعمال العربية من الأتمتة الاستباقية

Crafted with care to empower Arab businesses with proactive automation

---

v7.1.0 — Proactive Intelligence Edition

</div>
