# Empirical Findings — Live Production Probe

> Date: 2026-04-22 · Branch: `claude/setup-siyadah-os-briefing-kboY9`
> Method: read-only HTTP probes against the live Activepieces deployment at
> `activepieces-production-2499.up.railway.app` using operator-supplied
> credentials (in-session only, not persisted anywhere).
> **Secrets were never written to a file or a commit.**

---

## 1. Headline numbers (every one disagrees with the briefing)

| Metric | Briefing said | Reality (probed) | Evidence |
|---|---:|---:|---|
| Pieces available | 661 | **687** | `GET /api/v1/pieces/?includeHidden=false` |
| Templates available | 360 | **420** | `GET /api/v1/templates?limit=1000` |
| Unique pieces referenced across templates | — | **114** | Iterated `template.pieces[]` |
| Flows currently in production project | — | **147** | `GET /api/v1/flows/?projectId=ou4j…&limit=200` |
| Flow names used ≥2 times (duplicates) | — | **9 groups → 49 duplicate flows** | Grouped by `version.displayName` |
| Pieces actually used in the 147 live flows | — | **33 of 687 (4.8 %)** | Walked every flow tree |
| Active connections in the project | — | **3** (Gmail, Sheets, Drive) | `GET /api/v1/app-connections/?projectId=…` |
| ROUTER usages across 147 flows | — | **16** | `grep type=ROUTER` on flow tree |
| LOOP_ON_ITEMS usages across 147 flows | — | **10** | Same |
| CODE usages | — | **16** | Same |

The briefing's numbers are off by: **+26 pieces, +60 templates, 33 vs 114 potential**. Every forward claim in kb/docs that cited these numbers is stale.

---

## 2. Flow Awareness gap — empirically proven

The most impactful finding of this probe.

### 2.1 Duplicate flow groups

| Duplicates | Flow name |
|---:|---|
| **×20** | `حفظ + تنبيه` (Save + notify) |
| × 9 | `تنبيه إيميل فوري` (Instant email alert) |
| × 5 | `توجيه الليدات — سيادة` |
| × 4 | `متابعة ذكية — سيادة` |
| × 3 | `نظام ليدات كامل` |
| × 2 | `Strict Guard Test` |
| × 2 | `fuzzy test` |
| × 2 | `إرسال إيميلات جماعي — سيادة` |
| × 2 | `Slack تنبيه` |
| **Total** | **49 flows (33 %) are duplicates** |

### 2.2 Proof the duplicates are structurally identical

Picked two `حفظ + تنبيه` flows at random:

```
Flow 1 (CTyZWTxex23iaeiTzyjOR, updated 2026-04-08T09:40:39):
   PIECE_TRIGGER | webhook       |
   PIECE         | google-sheets | insert_row
   PIECE         | gmail         | send_email

Flow 2 (MCkdsTeV7RTrTTpM4xmAw, updated 2026-04-08T08:50:51):
   PIECE_TRIGGER | webhook       |
   PIECE         | google-sheets | insert_row
   PIECE         | gmail         | send_email

STRUCTURALLY IDENTICAL: True
```

Same pieces, same actions, same order. Built ~50 min apart on the same day.
**The orchestrator creates a brand-new flow every time a user asks for the
same outcome, instead of detecting the existing one and editing it.**

This is exactly what briefing Principle ③ (`No Conflicts + Edit Instead of
Create`) warned against. The gap is live, expensive (AP quota waste), and
user-visible (dashboard clutter).

### 2.3 Required fix (Wave 4 of `docs/PLAN.md`, promoted in priority)

- Before every `/v2/build-*` call, query existing flows for the same `(trigger_piece, action_chain)` skeleton within the tenant.
- If match found → call `UPDATE_FLOW` path; return `edited=true`.
- If conflict detected (e.g., two webhook flows with identical trigger) → return 409 with both IDs; let the caller decide.
- Index flows in Postgres so the lookup is O(log n) not O(flows) per build.

---

## 3. Dynamic values — partially working, partially stale

Flow 1 above, step_2 (gmail.send_email) inputs:

```
auth       = "{{connections['MKlKHKfL6OwZ7oqt0nt5h']}}"         ← DYNAMIC ✓
body       = "تم حفظ ليد:\n{{trigger.body.name}} — {{trigger.body.email}}"  ← DYNAMIC ✓
subject    = "ليد جديد محفوظ!"                                   ← STATIC (missed)
body_type  = "plain_text"                                        ← STATIC (correct)
```

### Finding

The **8 hard-coded templates** (`main.py:1431–1455`) resolve `{{trigger.body.*}}` correctly where the template author remembered. The gap is not in the engine — the engine's `generate_property_settings` correctly marks strings containing `{{…}}` as `CUSTOM_INPUT` (`main.py:636-638`). The gap is:

- No static analysis checks whether a field that **should** be dynamic (subject line, receiver, sheet row keys) accidentally ships as static text.
- The engine never rejects a build with suspicious-looking static strings that match trigger-body key names.
- There is **zero dependency** between the caller's declared input keys and what the flow's trigger actually produces; the orchestrator cannot tell whether `subject="New lead"` is a placeholder the caller forgot to parameterise, or a deliberate static string.

### Required fix (Wave 3 of `docs/PLAN.md` — **Parameter Binder**, code-level)

1. For every `/v2/build-*` call, after resolving the trigger, collect the set of keys the trigger's webhook payload is documented to expose (or infer from the spec's schema).
2. For each PIECE step downstream, compare its string inputs against the trigger key names.
3. If a string is literally equal to a trigger key (or looks like a templated placeholder such as `<email>` or `EMAIL`), **warn** and suggest `{{trigger.body.<key>}}`.
4. Optionally, in strict mode, **reject** the build with a 422 listing the suspect fields.

This is a 50-line code change in `main.py` between `clean_input_config` and `generate_property_settings`. It does **not** need any LLM — purely deterministic.

---

## 4. Piece under-utilisation

114 pieces referenced by templates, but only 33 used in live flows.

**Used heavily in live flows:**

| Piece | Live uses | Templates | Mismatch |
|---|---:|---:|---|
| webhook | 143 | — (trigger-only) | — |
| gmail | 131 | 153 | ~ parity |
| google-sheets | 67 | 192 | templates push more |
| slack | 21 | 111 | **5× templates** |
| whatsapp (+ variants) | 20 | ≈ 0 Meta-backed | different piece |
| openai | 14 | 30 | ~ parity |
| http | 14 | 51 | ~ parity |
| hubspot | 13 | 39 | **3× templates** |

**Heavily supported in templates, never used in live flows:**

| Piece | Template count | Live uses |
|---|---:|---:|
| ai (generic) | 155 | 0 |
| text-ai | 97 | 0 |
| date-helper | 80 | 0 |
| forms | 61 | 0 |
| store | 58 | 0 |
| utility-ai | 54 | 0 |
| data-mapper | 46 | 0 |
| notion | 31 | 0 |
| salesforce | 26 | 0 |
| pipedrive | 25 | 0 |
| perplexity-ai | 19 | 0 |
| firecrawl | 22 | 0 |

### Implication

The orchestrator's actual capability envelope is **Gmail + Sheets + (sometimes) Slack/HubSpot/WhatsApp + custom CODE**. Everything else in the 40-scenario catalogue — CRM diversity, data-mapper transforms, AI pre-processing, form ingestion, notion knowledge, Salesforce, Pipedrive — is **runtime-possible but orchestrator-blind**. The templates exist; the orchestrator never picks them.

---

## 5. Trigger diversity — none

All 147 live flows have `trigger.type = PIECE_TRIGGER`. Walking their structure:

- **webhook-triggered**: 143 flows
- **schedule-triggered** (`schedule` or `schedule_trigger`): 2 flows
- **piece-native triggers** (e.g., "Gmail: new email", "Sheets: row added", "Stripe: payment"): **0 flows**

The orchestrator's `main.py:842-846` defines only `every_day` and `cron_expression` helpers for schedule. There is no high-level support for `piece-native triggers` (the briefing's §6 patterns P1/P6 rely on them). Activepieces supports them; Siyadah does not.

---

## 6. Multi-tenancy gap — the blast radius in this deployment

The probe confirmed that the production project (`ou4jOTA4KMnDrzOVsKWvd`) is the **single** project in use. `GET /api/v1/projects/` returned empty (insufficient platform scope), but the `147 flows` all sit under that one project.

This means today the system has **zero tenants** — it is a single-tenant deployment marketed as multi-tenant. The impersonation risk (`reports/multi-tenancy-gap.md`, 14 leaking write sites) is **latent but not yet exploited**, because there are no other tenants to cross into.

### Consequence

- Every claim in the briefing about tenant isolation is *aspirational* until the second tenant arrives.
- The moment a second paying tenant is onboarded, the 14 write endpoints become a hot backdoor.
- Wave 1 (real multi-tenancy) must land **before** onboarding tenant #2, not after.

---

## 7. Local probe confirmation — security fixes work

From `/tmp/orch.log` running `uvicorn main:app` locally with the patched code:

```
ORCHESTRATOR_ALLOWED_ORIGINS not set — defaulting to ['https://app.siyadah.ai'].

/v2/templates  (no X-API-Key)    → HTTP 401  ✓ blocked
/v2/templates  (wrong X-API-Key) → HTTP 401  ✓ blocked
/v2/templates  (correct key)     → HTTP 200  ✓ allowed

Origin: https://evil.example.com   → no Access-Control-Allow-Origin echoed
Origin: https://app.siyadah.ai     → Access-Control-Allow-Origin: https://app.siyadah.ai
```

Fixes in `main.py:1765-1782` (CORS) and `main.py:1793-1794` (hmac.compare_digest) behave as designed.

### Secondary footgun

`api_key_check` middleware is a no-op when `ORCHESTRATOR_API_KEY` env var is **unset**. A staging deploy that forgets to set it is completely unauthenticated. This is documented behaviour but the middleware should log a **loud warning at boot** when key is empty AND path is public production. Add to Wave 1.

---

## 8. What we have NOT yet probed (requires user approval — touches state)

| Test | What it would prove | Blast radius |
|---|---|---|
| **T4** Impersonation | Passing an arbitrary `project_id` with valid `ORCHESTRATOR_API_KEY` succeeds in creating a flow in that project | Creates 1 flow + 1 project (deletable) |
| **T5** Dynamic-values silent failure | Deploy a flow with `receiver="hardcoded@test"` and fire webhook; observe the email goes to the static address regardless of webhook body | Creates 1 flow + 1 real Gmail send |
| **T6** Real bytes E2E | Deploy webhook→Sheet, fire webhook, verify row appears in Google Sheet | Creates 1 flow + 1 real Sheet row |

All three are small, reversible, and would close the last empirical blind spots. **Awaiting operator approval before touching production state.**

---

## 9. Security hygiene — what the operator must do now

The credentials pasted into the chat are in the session log. Assume they are compromised and **rotate all of the following** within 24h:

1. **`AP_PASSWORD`** — change the login credential for `a@siyadah-ai.com`.
2. **`ANTHROPIC_API_KEY`** — revoke + regenerate in the Anthropic console.
3. **`FIRECRAWL_API_KEY`** — revoke + regenerate.
4. **`AP_MCP_TOKEN`** — regenerate in AP.
5. **`AP_API_KEY`** — regenerate in AP (platform admin).
6. **`AP_JWT_SECRET`** — regenerate (invalidates all sessions; acceptable).
7. **`AP_ENCRYPTION_KEY`** — **DO NOT rotate casually**; it encrypts stored connections. Plan a migration first.
8. **Postgres + Redis creds** (Railway private domain — lower risk but rotate anyway).

After rotation, re-seed the Railway orchestrator service env with the new values. Do not commit the `.env` file.
