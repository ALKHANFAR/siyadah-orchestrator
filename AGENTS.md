# AGENTS.md

> **Coordinator spec for the Siyadah automation platform.**
> Read this file first. It is the contract between three systems. Any AI agent
> working on any of them must understand the boundaries below before changing code.

---

## The three systems

| # | System | Repo / Host | Role | Language |
|---|---|---|---|---|
| 1 | **Siyadah-6.5** | `github.com/ALKHANFAR/Siyadah-6.5` | User-facing BFF: chat UI, dashboard, onboarding, tenant auth, rate limits, secured proxy | TypeScript / Next.js 16 |
| 2 | **siyadah-orchestrator** *(this repo)* | `github.com/ALKHANFAR/siyadah-orchestrator` | Build engine: translates intent → Activepieces flow, institutional memory, website absorption, MCP/SSE, proactive intelligence | Python / FastAPI |
| 3 | **Activepieces** | `activepieces-production-2499.up.railway.app` | Self-hosted automation runtime (687 pieces) | — (external) |

### Request flow (end-to-end)

```
User → Siyadah-6.5 (Next.js)
     → /api/orchestrator/[...path]  (BFF proxy, 29-entry path allowlist, tenant-scoped)
     → siyadah-orchestrator (FastAPI, Railway)
     → Activepieces REST API  (Golden Protocol v5)
     → deployed flow running in Activepieces
```

**Hard rule:** Siyadah-6.5 **never** calls Activepieces directly. Every automation write goes through the orchestrator.

---

## What each system owns (and what it must never do)

### Siyadah-6.5 owns
- User authentication, session, UI
- Per-tenant rate limiting (`src/lib/security/rate-limit.ts`)
- The 29-entry path allowlist for the orchestrator proxy
- Chat streaming against Claude for conversation only
- Storing tenant-scoped metadata in its own Postgres

### Siyadah-6.5 must never
- Construct Activepieces flow JSON directly
- Call Activepieces REST API bypassing the orchestrator
- Store orchestrator-owned state (flow IDs, institutional memory, project identity)

### siyadah-orchestrator owns
- Building, validating, deploying Activepieces flows (Golden Protocol v5)
- Institutional memory: `ProjectIdentity`, `KnowledgeAsset`, `AutonomousSetting` (Postgres)
- AI website absorption (Firecrawl → Claude → persist)
- MCP tool surface (16 tools) + SSE sessions (Redis)
- Proactive intelligence: success patterns × identity → suggestions

### siyadah-orchestrator must never
- Surface user-facing UI
- Handle user authentication (it trusts the BFF's `X-API-Key`)
- Hardcode tenant IDs — every write is scoped to `project_id`

---

## Contract: BFF → Orchestrator

### Auth
- When `ORCHESTRATOR_API_KEY` is set, **all `/v2/` endpoints require** `X-API-Key: <key>`
- BFF injects the header in its proxy handler (`src/lib/orchestrator-server.ts`)

### Endpoints the BFF is expected to call

| Endpoint | Method | Used by | Purpose |
|---|---|---|---|
| `/health` | GET | health probe | Liveness + AP connectivity |
| `/v2/client-status` | GET | dashboard | Full status (flows + runs + connections) |
| `/v2/templates`, `/v2/presets` | GET | UI picker | Catalog |
| `/v2/available-pieces` | GET | UI picker | 687 pieces |
| `/v2/build-and-deploy` | POST | one-click template | Build from template |
| `/v2/build-preset` | POST | advanced flows | Build from preset (lead_routing, bulk_email, smart_followup, router_loop_combo) |
| `/v2/build-complex` | POST | agent-composed flows | Arbitrary ROUTER+LOOP+CODE+PIECE chains |
| `/v2/validate-flow` | POST | dry-run | Pre-build validation (no deploy) |
| `/v2/flows/{id}` | PATCH | dashboard | Enable / disable / delete |
| `/v2/flows/{id}/diagnose` | GET | debug UI | Flow structure diagnostic |
| `/v2/identity/ingest` | POST | onboarding | Absorb website (AI scrape) |
| `/v2/saas/register` | POST | onboarding | preview → register → auto-config |
| `/v2/logic/proactive-suggestions` | GET | dashboard | Missed opportunities + health alerts |
| `/v2/mcp/execute` | POST | agent loop | Execute any of the 16 MCP tools |
| `/v2/mcp/sse` | GET | agent loop | Open SSE session (Redis-backed) |

The full list lives in `main.py` — grep `^@app\.`. If the BFF needs a path not in the 29-entry allowlist, **update the allowlist first**, then call.

### Request shapes

Request/response shapes for each endpoint are Pydantic models in `main.py`
(search `class .*Body` — `BuildBody`, `DynamicBuildBody`, `ComplexBuildBody`,
`PresetBuildBody`, etc). Read the model, don't guess.

---

## Contract: Orchestrator → Activepieces

### Golden Protocol v5 — MANDATORY

Never trust HTTP 200 alone. Every flow write goes through this pipeline
(`main.py:1270 golden_build`):

```
① CREATE_FLOW          POST /api/v1/flows           → get flow_id
② IMPORT_FLOW          POST /api/v1/flows/{id}/operations  op=IMPORT_FLOW
③ GET-verify           GET  /api/v1/flows/{id}      → confirm trigger exists
④ LOCK_AND_PUBLISH     POST /api/v1/flows/{id}/operations  op=LOCK_AND_PUBLISH
⑤ ENABLE (if draft)    POST /api/v1/flows/{id}/operations  op=CHANGE_STATUS
⑥ UNIVERSAL PULSE      POST to the flow's webhook URL with a canonical payload
```

Step ③ is non-negotiable. `main.py:166` comment: *"SILENT FAILURE: trigger still EMPTY after IMPORT_FLOW. Check propertySettings: {} in every step."*

### Architectural rules (enforced by orchestrator)

| Rule | Where | Why |
|---|---|---|
| `propertySettings: {}` in **every** step settings | `_build_step_from_spec` in `main.py` | Without it, AP silently keeps flow in DRAFT |
| `auth` format: `{{connections['externalId']}}` | `C()` helper | Binds flow to tenant's stored connection |
| Final `GET` must show `status=ENABLED` and `publishedVersionId` match | `golden_build` | Catches publish-vs-enable races |
| All writes scoped to `project_id` | `DEFAULT_PID` + `resolve_conns()` | Multi-tenant isolation |
| `Draft Guard` injects known BOOLEAN fields | `_BOOLEAN_FIELD_NAMES` | Prevents flows stuck in DRAFT |

---

## Environment variables

### Required on the orchestrator (Railway service)

```
AP_BASE_URL           # Activepieces instance URL
AP_EMAIL              # Login email for AP
AP_PASSWORD           # Login password
AP_PROJECT_ID         # Default AP project (multi-tenant scope root)
GMAIL_CONNECTION_ID   # AP connection externalId for Gmail
SHEETS_CONNECTION_ID  # AP connection externalId for Sheets
DATABASE_URL          # Postgres (institutional memory)
```

### Optional but recommended

```
REDIS_URL              # SSE sessions (falls back to in-memory if unset)
ORCHESTRATOR_API_KEY   # Turns on /v2/ auth — set in production
ANTHROPIC_API_KEY      # Website absorption (AI analysis)
FIRECRAWL_API_KEY      # Website scraping
AP_MCP_SERVER_URL      # AP MCP proxy endpoint
AP_MCP_TOKEN           # AP MCP bearer token
ORCHESTRATOR_HTTPX_TIMEOUT  # Default 120s
```

### On the BFF (Siyadah-6.5)

The BFF needs `ORCHESTRATOR_URL` + `ORCHESTRATOR_API_KEY` to reach this service.
It does **not** need the `AP_*` variables — those belong exclusively to the orchestrator.

---

## Commands

### Run the orchestrator locally
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in the values above
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Deploy
Railway auto-deploys on push to `main`. This branch
(`claude/enhanced-client-journey-8Yu8A`) is for feature work — merge to
`main` to ship.

### Verify a deploy end-to-end
```bash
python deploy_sondos_saas.py    # Multi-tenant proof — creates project + 5-step flow
python test_v71_qa.py           # QA suite
python test_mega_challenge.py   # Complex ROUTER+LOOP scenarios
```

### Quick checks
```bash
curl -s $ORCHESTRATOR_URL/health | jq
curl -s $ORCHESTRATOR_URL/v2/client-status -H "X-API-Key: $ORCHESTRATOR_API_KEY" | jq
```

---

## Known-good building blocks

### Templates (`/v2/templates`)
`webhook_to_email`, `webhook_to_sheet`, `webhook_to_sheet_and_email`,
`support_auto_reply`, `marketing_welcome`, `ops_log_report`,
`lead_notify_and_confirm`, `scheduled_report`

### Presets (`/v2/presets`)
`lead_routing` (ROUTER), `bulk_email` (LOOP), `smart_followup` (ROUTER+LOOP),
`router_loop_combo` (ROUTER + 2×LOOP)

### Active connections — AP project `ou4jOTA4KMnDrzOVsKWvd`
Run `GET /connections` to refresh. As of last audit:

| Name | Piece | externalId |
|---|---|---|
| Gmail | `@activepieces/piece-gmail` | `MKlKHKfL6OwZ7oqt0nt5h` |
| Google Sheets | `@activepieces/piece-google-sheets` | `TtUKW8AMWsMBlY7ayqocf` |
| Google Drive | `@activepieces/piece-google-drive` | `J0iUwaxY1Hc6vSo3LY6o6` |

**Do not reference pieces without a live connection** — the build will fail at `guard_connections` (`main.py`).

---

## Institutional memory (Postgres)

Three tables, all keyed on `project_id`:

| Model | Fields | Written by |
|---|---|---|
| `Project` | `project_id`, `name`, `created_at` | `/v2/project/register`, `/v2/saas/register` |
| `ProjectIdentity` | `sector`, `language`, `business_description`, `website_url`, `absorbed_at` | `/v2/identity/ingest` |
| `KnowledgeAsset` | `faqs`, `tone_of_voice`, `brand_keywords` | `/v2/identity/ingest` |

Schema lives in `models.py`. The proactive engine (`main.py:3545`) reads these
tables to produce `OPPORTUNITY / WARNING / SUCCESS_STORY / INFO` hints.

---

## Common pitfalls (all three systems)

- **Never trust HTTP 200 on flow writes** — always `GET` to confirm. Golden Protocol enforces this; don't short-circuit it.
- **Never call Activepieces from the BFF** — always via orchestrator.
- **Never reference a piece without an active connection** — `guard_connections` rejects the build.
- **Never hardcode a tenant** — every request carries `project_id` (falls back to `DEFAULT_PID` only in single-tenant dev).
- **Never change the system prompt / tool list mid-agent-session** — breaks prompt caching (`shared/prompt-caching.md`); inject updates as a user message instead.
- **When you see `DRAFT` after publish** — missing `propertySettings: {}` or a missing BOOLEAN field. Run `/v2/flows/{id}/diagnose`.
- **Hesitant-drip / no-show / win-back flows have time-based waits** — use `@activepieces/piece-delay` (`delayFor` / `delay_until`). Max duration per step is `pausedFlowTimeoutDays`.

---

## When the agent is unsure

1. `GET /health` — is the orchestrator alive and connected to AP?
2. `GET /v2/client-status` — full dashboard snapshot.
3. `GET /v2/flows/{id}/diagnose` — flow structure + trigger + steps.
4. `GET /connections` + `GET /v2/connections/health` — connection state.
5. `GET /v2/pieces/{name}/schema` — exact fields for a piece before you build.
6. Read the Pydantic model in `main.py` — don't guess request shapes.

---

## Related reading (in this repo)

- `README.md` — full Arabic + English product documentation
- `main.py` — the engine (4100+ lines; grep for the feature name)
- `models.py` — Postgres schema
- `ingestion.py` — website absorption pipeline
- `mcp_sse.py` — MCP + SSE transport
- `deploy_sondos_saas.py` — reference end-to-end deploy script
