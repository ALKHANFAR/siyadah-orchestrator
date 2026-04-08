# سيادة Orchestrator v5.1.0

Async Multi-Tenant Automation Engine — **ROUTER · LOOP · CODE · PIECE · PRESETS · Smart Schema · MCP Dispatcher · Re-import**

## الميزات

| القدرة | الوصف |
|---|---|
| **Async Engine** | بناء على `httpx.AsyncClient` لأداء عالٍ |
| **Multi-Tenancy** | دعم مشاريع و اتصالات متعددة عبر `project_id` و `connection_ids` |
| **Golden Protocol v4** | `IMPORT_FLOW → GET-verify → LOCK_AND_PUBLISH → ENABLE` |
| **8 قوالب جاهزة** | من تنبيه إيميل بسيط إلى تقرير يومي مجدول |
| **4 سيناريوهات متقدمة** | ROUTER, LOOP, Smart Followup, Router+Loop Combo |
| **Smart Schema** | جلب `propertySettings` تلقائياً من مواصفات الـ Piece |
| **MCP Dispatcher** | واجهة أدوات لـ Claude / AI agents عبر `/v2/mcp/execute` |
| **Re-import** | تحديث بنية فلو موجود بدون إعادة إنشائه |
| **Diagnose** | تشخيص بنية أي فلو بجميع الخطوات والأنواع |

## البنية

```
Client / Claude AI
       │
       ▼
┌──────────────────────────────────────┐
│     Siyadah Orchestrator v5.1.0      │
│  ┌────────────────────────────────┐  │
│  │  /v2/build-and-deploy         │  │
│  │  /v2/build-dynamic            │  │
│  │  /v2/build-router             │  │
│  │  /v2/build-loop               │  │
│  │  /v2/build-complex            │  │
│  │  /v2/build-preset             │  │
│  │  /v2/build-smart              │  │
│  │  /v2/mcp/execute              │  │
│  │  /v2/flows/{id}/reimport      │  │
│  │  /v2/flows/{id}/diagnose      │  │
│  └────────────────────────────────┘  │
└──────────────┬───────────────────────┘
               │
               ▼
        Activepieces API
   (Golden Protocol v4 Pipeline)
```

## V2 Endpoints

| Endpoint | Method | الوظيفة |
|---|---|---|
| `/` | GET | الحالة + الإصدار |
| `/health` | GET | فحص الاتصال بـ Activepieces |
| `/templates` | GET | عرض الفلوات الحالية |
| `/connections` | GET | عرض الاتصالات |
| `/pieces/{name}` | GET | مواصفات أداة |
| `/operators` | GET | قائمة شروط الـ ROUTER |
| `/v2/templates` | GET | القوالب المتاحة |
| `/v2/presets` | GET | السيناريوهات المتقدمة |
| `/v2/build-and-deploy` | POST | بناء من قالب |
| `/v2/build-dynamic` | POST | بناء مخصص بأي أدوات |
| `/v2/build-router` | POST | بناء فلو مع تفرّع |
| `/v2/build-loop` | POST | بناء فلو مع تكرار |
| `/v2/build-complex` | POST | بناء أي مزيج |
| `/v2/build-preset` | POST | بناء من سيناريو جاهز |
| `/v2/build-smart` | POST | بناء ذكي مع Schema Validation |
| `/v2/validate-flow` | POST | التحقق قبل البناء (Dry Run) |
| `/v2/flows/{id}` | PATCH | تفعيل / تعطيل / حذف فلو |
| `/v2/flows/{id}/reimport` | POST | تحديث فلو موجود |
| `/v2/flows/{id}/diagnose` | GET | تشخيص بنية الفلو |
| `/v2/test-webhook/{id}` | POST | اختبار عبر webhook |
| `/v2/client-status` | GET | لوحة حالة العميل |
| `/v2/available-pieces` | GET | كتالوج الأدوات المتاحة |
| `/v2/pieces/{name}/schema` | GET | مواصفات تفصيلية لأداة |
| `/v2/mcp/tools` | GET | قائمة أدوات MCP |
| `/v2/mcp/execute` | POST | تنفيذ أداة MCP |
| `/v2/create-project` | POST | إنشاء مشروع جديد |

## القوالب (Templates)

| الاسم | الوصف |
|---|---|
| `webhook_to_email` | تنبيه إيميل فوري |
| `webhook_to_sheet` | حفظ بيانات في جدول |
| `webhook_to_sheet_and_email` | حفظ + تنبيه |
| `support_auto_reply` | رد تلقائي + تذكرة دعم |
| `marketing_welcome` | ترحيب مشترك جديد |
| `ops_log_report` | تسجيل عملية + تقرير |
| `lead_notify_and_confirm` | نظام ليدات كامل |
| `scheduled_report` | تقرير يومي |

## السيناريوهات المتقدمة (Presets)

| الاسم | النوع | الوصف |
|---|---|---|
| `lead_routing` | ROUTER | توجيه ليدات حسب وجود الإيميل |
| `bulk_email` | LOOP | إيميلات جماعية — تكرار + إرسال |
| `smart_followup` | ROUTER + LOOP | تصنيف بالسكور + مهام متابعة |
| `router_loop_combo` | ROUTER + 2×LOOP | توجيه + تكرار داخل كل فرع |

## الإعداد

```bash
# 1. انسخ المتغيرات
cp .env.example .env
# 2. عدّل القيم في .env

# 3. شغّل
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

### متغيرات البيئة (.env)

| المتغير | الوصف |
|---|---|
| `AP_BASE_URL` | رابط Activepieces |
| `AP_EMAIL` | إيميل تسجيل الدخول |
| `AP_PASSWORD` | كلمة المرور |
| `AP_PROJECT_ID` | معرّف المشروع الافتراضي |
| `GMAIL_CONNECTION_ID` | معرّف اتصال Gmail |
| `SHEETS_CONNECTION_ID` | معرّف اتصال Google Sheets |
| `ORCHESTRATOR_API_KEY` | مفتاح حماية endpoints الـ v2 (اختياري) |

## النشر

```bash
git add . && git commit -m "v5.1.0" && git push
# Railway auto-deploys from main branch
```

## القواعد المثبتة

- `propertySettings: {}` في كل step settings (فارغ أو من الـ schema)
- `IMPORT_FLOW` الأسلوب الوحيد الموثوق لإنشاء الفلوات
- `GET` بعد كل عملية (لا تثق بـ 200 وحده)
- `auth: {{connections['externalId']}}` لربط الاتصالات
- `LOCK_AND_PUBLISH` ثم `CHANGE_STATUS` إذا لم يُفعّل تلقائياً
# force rebuild
