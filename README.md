<div align="center">

# سيادة Orchestrator | Siyadah Orchestrator

### v6.6.0 — Diamond Edition

**محرك أتمتة غير متزامن متعدد المستأجرين | Async Multi-Tenant Automation Engine**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Deploy on Railway](https://img.shields.io/badge/Deploy-Railway-0B0D0E?logo=railway)](https://railway.app)

---

**سيادة** هو محرك تنسيق يربط بين وكلاء الذكاء الاصطناعي ومنصة [Activepieces](https://www.activepieces.com) لبناء، نشر، وإدارة سلاسل الأتمتة — من إيميل بسيط إلى تفرّعات معقدة بلوبات متداخلة — عبر API واحد.

**Siyadah** is an orchestration engine that bridges AI agents with [Activepieces](https://www.activepieces.com) to build, deploy, and manage automation flows — from a simple email alert to complex router+loop combos — through a single API.

</div>

---

## المحتويات | Table of Contents

<table>
<tr>
<td width="50%">

**العربية**
- [المعمارية](#المعمارية)
- [القدرات الأساسية](#القدرات-الأساسية)
- [البروتوكول الذهبي](#البروتوكول-الذهبي-golden-protocol-v5)
- [الذكاء التلقائي](#الذكاء-التلقائي-smart-auto-fill)
- [النبضة الشاملة](#النبضة-الشاملة-universal-pulse)
- [نقاط الوصول](#نقاط-الوصول-api-endpoints)
- [القوالب](#القوالب-templates)
- [السيناريوهات المتقدمة](#السيناريوهات-المتقدمة-presets)
- [تكامل MCP](#تكامل-mcp)
- [الإعداد والتشغيل](#الإعداد-والتشغيل)
- [النشر](#النشر)

</td>
<td width="50%">

**English**
- [Architecture](#المعمارية)
- [Core Capabilities](#القدرات-الأساسية)
- [Golden Protocol](#البروتوكول-الذهبي-golden-protocol-v5)
- [Smart Auto-Fill](#الذكاء-التلقائي-smart-auto-fill)
- [Universal Pulse](#النبضة-الشاملة-universal-pulse)
- [API Endpoints](#نقاط-الوصول-api-endpoints)
- [Templates](#القوالب-templates)
- [Presets](#السيناريوهات-المتقدمة-presets)
- [MCP Integration](#تكامل-mcp)
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
                        │   External Client     │
                        └──────────┬───────────┘
                                   │  REST / MCP
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│                  Siyadah Orchestrator v6.6.0                     │
│                                                                  │
│  ┌────────────┐  ┌──────────────┐  ┌──────────────────────────┐ │
│  │  Templates  │  │   Presets    │  │    Smart Build Engine    │ │
│  │  (8 ready)  │  │ (4 complex) │  │  Schema + Auto-Fill +    │ │
│  │             │  │             │  │  Dropdown Intelligence   │ │
│  └──────┬─────┘  └──────┬──────┘  └────────────┬─────────────┘ │
│         │               │                      │                │
│         └───────────────┼──────────────────────┘                │
│                         ▼                                       │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Golden Protocol v5 Pipeline                  │   │
│  │  IMPORT_FLOW → GET-verify → LOCK_AND_PUBLISH → ENABLE   │   │
│  │                    + Universal Pulse                      │   │
│  └──────────────────────────┬───────────────────────────────┘   │
│                             │                                   │
│  ┌──────────────────────────┼───────────────────────────────┐   │
│  │          Connection & Multi-Tenancy Layer                 │   │
│  │    guard_connections · resolve_conns · API Key Auth       │   │
│  └──────────────────────────┬───────────────────────────────┘   │
│                             │                                   │
│  ┌──────────────────────────┼───────────────────────────────┐   │
│  │             MCP Dispatcher (16 tools)                     │   │
│  │    Claude-native tool interface + AP MCP Proxy            │   │
│  └──────────────────────────┬───────────────────────────────┘   │
└─────────────────────────────┼───────────────────────────────────┘
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
<td>مبني على <code>httpx.AsyncClient</code> مع timeout قراءة 120 ثانية لسلاسل IMPORT/PUBLISH الطويلة</td>
<td>Built on <code>httpx.AsyncClient</code> with 120s read timeout for long IMPORT/PUBLISH chains</td>
</tr>
<tr>
<td><strong>Multi-Tenancy</strong></td>
<td>دعم مشاريع واتصالات متعددة عبر <code>project_id</code> و <code>connection_ids</code></td>
<td>Multiple projects and connections via <code>project_id</code> and <code>connection_ids</code></td>
</tr>
<tr>
<td><strong>Golden Protocol v5</strong></td>
<td>خط أنابيب صارم: IMPORT → VERIFY → LOCK → ENABLE مع تحقق نهائي من الحالة</td>
<td>Strict pipeline: IMPORT → VERIFY → LOCK → ENABLE with final state confirmation</td>
</tr>
<tr>
<td><strong>Smart Auto-Fill</strong></td>
<td>ملء تلقائي ذكي للحقول المفقودة حسب النوع (BOOLEAN, NUMBER, ARRAY, DROPDOWN)</td>
<td>Type-aware auto-fill for missing fields (BOOLEAN, NUMBER, ARRAY, DROPDOWN)</td>
</tr>
<tr>
<td><strong>Universal Pulse</strong></td>
<td>نبضة تفعيل شاملة تضمن بيانات جاهزة لأي فلو (إيميل، جداول، إلخ) من النبضة الأولى</td>
<td>Universal activation pulse ensuring ready data for any flow type from the first pulse</td>
</tr>
<tr>
<td><strong>8 Templates</strong></td>
<td>من تنبيه إيميل بسيط إلى تقرير يومي مجدول</td>
<td>From simple email alert to scheduled daily reports</td>
</tr>
<tr>
<td><strong>4 Complex Presets</strong></td>
<td>ROUTER، LOOP، ومزيج منهما مع تفرّعات متداخلة</td>
<td>ROUTER, LOOP, and nested combos with branch logic</td>
</tr>
<tr>
<td><strong>Smart Schema</strong></td>
<td>جلب <code>propertySettings</code> تلقائياً من مواصفات الـ Piece مع تحقق من الأنواع</td>
<td>Auto-fetch <code>propertySettings</code> from piece specs with type validation</td>
</tr>
<tr>
<td><strong>MCP Dispatcher</strong></td>
<td>16 أداة عبر واجهة أدوات Claude / AI Agents عبر <code>/v2/mcp/execute</code></td>
<td>16 tools via Claude/AI agent tool interface at <code>/v2/mcp/execute</code></td>
</tr>
<tr>
<td><strong>Connection Management</strong></td>
<td>إنشاء، فحص صحة، اختبار، وحذف الاتصالات مع حماية من الحذف العشوائي</td>
<td>Create, health-check, test, and delete connections with safe-delete guard</td>
</tr>
<tr>
<td><strong>Flow Lifecycle</strong></td>
<td>بناء، نشر، تفعيل، تعطيل، حذف، إعادة استيراد، وتشخيص</td>
<td>Build, deploy, enable, disable, delete, re-import, and diagnose</td>
</tr>
<tr>
<td><strong>Draft Guard</strong></td>
<td>حقن حقول BOOLEAN المفقودة تلقائياً لمنع حالة DRAFT</td>
<td>Auto-inject missing BOOLEAN fields to prevent DRAFT state</td>
</tr>
<tr>
<td><strong>600+ Pieces</strong></td>
<td>كتالوج كامل من أدوات الأتمتة المتاحة (Gmail, Slack, HubSpot, Sheets…)</td>
<td>Full catalog of available automation pieces (Gmail, Slack, HubSpot, Sheets…)</td>
</tr>
</table>

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

بالإضافة إلى **Draft Guard** — طبقة حماية تحقن حقول BOOLEAN المعروفة (`draft`, `public`, `active`…) تلقائياً لمنع الفلو من البقاء في حالة DRAFT.

Additionally, **Draft Guard** auto-injects known BOOLEAN fields (`draft`, `public`, `active`…) to prevent flows from staying in DRAFT state.

---

## النبضة الشاملة | Universal Pulse

نبضة التفعيل الذكية تُرسل تلقائياً بعد كل نشر ناجح وتتضمن حقولاً ثابتة تضمن عمل أي نوع من الفلوات:

The smart activation pulse is sent automatically after every successful deploy, containing fixed fields that ensure any flow type works:

```json
{
  "event": "Siyadah_Activation",
  "status": "Success",
  "timestamp": "2026-04-11T...",
  "receiver": ["test@siyadah.ai"],
  "subject": "سيادة — تجربة تفعيل ناجحة",
  "body": "تهانينا! نظام الأتمتة الخاص بك يعمل الآن بكفاءة.",
  "values": {"A": "بيانات تجريبية", "B": "نجاح التفعيل"},

  "email": { ... },
  "row": { ... },
  "customer": { ... },
  "order": { ... }
}
```

الحقول الإضافية (`email`, `row`, `customer`, `order`) تُضاف ديناميكياً حسب القطع المستخدمة في الفلو (Gmail, Sheets, Slack, HubSpot).

Additional fields (`email`, `row`, `customer`, `order`) are dynamically added based on pieces used in the flow.

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

### الاستعلام | Query

| Endpoint | Method | الوظيفة | Description |
|---|---|---|---|
| `/templates` | `GET` | عرض الفلوات الحالية | List current flows |
| `/connections` | `GET` | عرض الاتصالات | List connections |
| `/pieces/{name}` | `GET` | مواصفات أداة | Piece specification |
| `/operators` | `GET` | قائمة شروط الـ ROUTER | Router condition operators |
| `/v2/templates` | `GET` | القوالب المتاحة | Available templates |
| `/v2/presets` | `GET` | السيناريوهات المتقدمة | Complex presets |
| `/v2/client-status` | `GET` | لوحة حالة شاملة | Full client dashboard |
| `/v2/available-pieces` | `GET` | كتالوج 600+ أداة | 600+ pieces catalog |
| `/v2/pieces/{name}/schema` | `GET` | مواصفات تفصيلية لأداة | Detailed piece schema |

### إدارة الاتصالات | Connection Management

| Endpoint | Method | الوظيفة | Description |
|---|---|---|---|
| `/v2/connect` | `POST` | إنشاء اتصال جديد | Create new connection |
| `/v2/connections/health` | `GET` | فحص صحة جميع الاتصالات | Health check all connections |
| `/v2/connections/{id}/test` | `POST` | اختبار اتصال محدد | Test specific connection |
| `/v2/connections/{id}` | `DELETE` | حذف اتصال (مع حماية) | Delete connection (with guard) |

### MCP و المشاريع | MCP & Projects

| Endpoint | Method | الوظيفة | Description |
|---|---|---|---|
| `/v2/mcp/tools` | `GET` | قائمة أدوات MCP (16 أداة) | MCP tools list (16 tools) |
| `/v2/mcp/execute` | `POST` | تنفيذ أداة MCP | Execute MCP tool |
| `/v2/mcp/proxy` | `POST` | بروكسي لـ AP MCP Server | Proxy to AP MCP Server |
| `/v2/create-project` | `POST` | إنشاء مشروع جديد | Create new project |

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
| `smart_followup` | Smart Follow-up | ROUTER + LOOP | ROUTER + LOOP | تصنيف بالسكور + مهام متابعة | Score classification + follow-up tasks |
| `router_loop_combo` | Router+Loop Combo | ROUTER + 2×LOOP | ROUTER + 2×LOOP | توجيه + تكرار داخل كل فرع | Route + loop inside each branch |

---

## تكامل MCP

واجهة أدوات متوافقة مع Claude وأي AI Agent يدعم بروتوكول MCP:

MCP-compatible tool interface for Claude and any AI Agent supporting the MCP protocol:

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
<tr><td>15</td><td><code>list_operators</code></td><td>شروط الـ ROUTER | Router operators</td></tr>
<tr><td>16</td><td><code>mcp_proxy</code></td><td>بروكسي لـ AP MCP | AP MCP proxy</td></tr>
</table>

---

## الإعداد والتشغيل

### المتطلبات | Prerequisites

- Python 3.10+
- نسخة Activepieces (Self-hosted أو Cloud)
- اتصالات مفعّلة (Gmail, Google Sheets, إلخ)

### التثبيت | Installation

```bash
# استنسخ المشروع | Clone the repository
git clone https://github.com/ALKHANFAR/siyadah-orchestrator.git
cd siyadah-orchestrator

# انسخ متغيرات البيئة | Copy environment variables
cp .env.example .env

# عدّل القيم في .env | Edit values in .env

# ثبّت المتطلبات | Install dependencies
pip install -r requirements.txt

# شغّل المحرك | Run the engine
uvicorn main:app --host 0.0.0.0 --port 8000
```

### متغيرات البيئة | Environment Variables

| المتغير | Variable | مطلوب | الوصف | Description |
|---|---|---|---|---|
| `AP_BASE_URL` | `AP_BASE_URL` | **نعم** | رابط Activepieces | Activepieces instance URL |
| `AP_EMAIL` | `AP_EMAIL` | **نعم** | إيميل تسجيل الدخول | Login email |
| `AP_PASSWORD` | `AP_PASSWORD` | **نعم** | كلمة المرور | Password |
| `AP_PROJECT_ID` | `AP_PROJECT_ID` | **نعم** | معرّف المشروع الافتراضي | Default project ID |
| `GMAIL_CONNECTION_ID` | `GMAIL_CONNECTION_ID` | **نعم** | معرّف اتصال Gmail | Gmail connection ID |
| `SHEETS_CONNECTION_ID` | `SHEETS_CONNECTION_ID` | **نعم** | معرّف اتصال Google Sheets | Sheets connection ID |
| `ORCHESTRATOR_API_KEY` | `ORCHESTRATOR_API_KEY` | اختياري | مفتاح حماية endpoints الـ v2 | V2 endpoints API key |
| `AP_MCP_SERVER_URL` | `AP_MCP_SERVER_URL` | اختياري | عنوان بروكسي MCP | MCP proxy URL |
| `AP_MCP_TOKEN` | `AP_MCP_TOKEN` | اختياري | توكن MCP | MCP token |
| `ORCHESTRATOR_HTTPX_TIMEOUT` | `ORCHESTRATOR_HTTPX_TIMEOUT` | اختياري | مهلة HTTP بالثواني (افتراضي: 120) | HTTP timeout in seconds (default: 120) |

### الحماية | Security

عند تعيين `ORCHESTRATOR_API_KEY`، جميع endpoints تحت `/v2/` تتطلب هيدر:

When `ORCHESTRATOR_API_KEY` is set, all `/v2/` endpoints require the header:

```
X-API-Key: your-api-key
```

---

## النشر | Deployment

### Railway (الموصى به | Recommended)

المشروع يتضمن `Procfile` جاهز:

The project includes a ready `Procfile`:

```
web: uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
```

```bash
# ادفع التغييرات وسيُعاد النشر تلقائياً | Push changes for auto-deploy
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

## القواعد المعمارية | Architectural Rules

| القاعدة | Rule | الوصف | Description |
|---|---|---|---|
| `propertySettings: {}` | Mandatory | في كل step settings (فارغ أو من الـ schema) | In every step settings (empty or from schema) |
| `IMPORT_FLOW` | Trusted Method | الأسلوب الموثوق لإنشاء/تحديث بنية الفلو | Trusted method for creating/updating flow structure |
| `GET` after critical ops | Verification | لا تثق بـ 200 وحده | Never trust 200 alone |
| `auth` format | Connection binding | `{{connections['externalId']}}` لربط الاتصالات | For binding connections |
| `LOCK_AND_PUBLISH` | State transition | ثم `CHANGE_STATUS` إذا لم يُفعّل تلقائياً | Then `CHANGE_STATUS` if not auto-enabled |

---

## المساهمات | Tech Stack

| التقنية | Technology | الدور | Role |
|---|---|---|---|
| **FastAPI** | Web Framework | إطار العمل الأساسي | Core framework |
| **httpx** | HTTP Client | العميل غير المتزامن | Async HTTP client |
| **Pydantic v2** | Validation | التحقق من البيانات | Data validation |
| **uvicorn** | ASGI Server | خادم ASGI | ASGI server |
| **python-dotenv** | Config | إدارة متغيرات البيئة | Environment management |

---

<div align="center">

**سيادة — محرك الأتمتة الذكي**

**Siyadah — The Smart Automation Engine**

صُنع بعناية لتمكين الأعمال العربية من الأتمتة الذكية

Crafted with care to empower Arab businesses with smart automation

---

v6.6.0 — Diamond Edition

</div>
