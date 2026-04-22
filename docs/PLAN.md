# Siyadah OS — Master Execution Plan

> Crystallized after 10 refinement iterations.
> Written: 2026-04-22 · Branch: `claude/setup-siyadah-os-briefing-kboY9`
> Author: Claude (verified every number against `main.py`, `AGENTS.md`, WebFetch)

---

## 1. Context

The user owns three systems that must together become an **Autonomous Company OS**:

1. **Siyadah-6.5** (`github.com/ALKHANFAR/Siyadah-6.5`) — Next.js 16 BFF + Chat UI.
2. **siyadah-orchestrator** (this repo) — FastAPI build engine.
3. **Activepieces** (self-hosted) — 687-piece automation runtime.

Two planning briefings (§12 §13 of the original master briefing + the 40-scenario
catalogue + `CLAUDE.md v3`) described the target state. Direct inspection of
`main.py` revealed the briefings drift from reality — some numbers wrong, some
security claims exaggerated, some multi-tenancy claims untrue. This plan is
corrected against what the code actually does.

**Goal**: reach the 10 sovereign capabilities (C1–C10) below in 7 waves, without
fabrication, without skipping lower-layer integrity, without selling features
the runtime cannot actually deliver.

---

## 2. Verified Ground Truth (what I re-checked)

| Claim in briefing | Reality | Source |
|---|---|---|
| Orchestrator 24 endpoints | **38 endpoints** | `grep -cE "^@app\." main.py` |
| `except Exception` ×59 | **36** | `grep -c "except Exception" main.py` |
| AP has 661 pieces | **687** | `AGENTS.md:51` |
| Templates available | **420** (not 360) | live probe `/api/v1/templates?limit=1000` |
| Multi-tenancy fully enforced | **half-built**: schema has `project_id` FK, but auth does not verify caller owns it | `models.py:21-73` + `main.py:1870,…,4038` |
| CORS secure | `allow_origins=["*"], allow_credentials=True` | `main.py:1761` |
| API-key comparison secure | `if key != ORCH_API_KEY` (timing-leak) | `main.py:1772` |
| `agents.md` is a "no-hallucination contract" | **false** — it is just a README convention for AI agents | WebFetch |
| Universal 360° absorption exists | **only website** via Firecrawl; no GMaps, no IG, no competitors | `ingestion.py:1-80` |
| SSE tenant-safe | Session in Redis with TTL 3600, **no tenant binding** | `mcp_sse.py:27-60` |

---

## 3. Corrections applied to the briefing

1. **No fixed E1–E7 employee catalogue.** Replaced by **Dynamic Employee Spawner** — generates N employees per tenant with auto-chosen names/roles/prompts from DNA + sector + KB.
2. **Pricing ≠ four tiers.** One entry package ($25/mo) + Wallet-metered usage. `UsageMeter` becomes a first-class service.
3. **50-tool locked list ≠ real.** Toolkit is dynamic per tenant (Smart Pruning by sector + connected pieces).
4. **40 scenarios ≠ 40 implementations.** Extract 14 Pattern Primitives + 5 variables-that-change-everything + 10 failure-modes from them. Scenarios become E2E fixtures, not hard-coded features.
5. **"24 endpoints" → 38.** All counts in kb/docs updated.
6. **"661 pieces" → 687.** **"360 templates" → 420.**
7. **"Multi-tenancy is real" → half-built.** Treat tenant isolation as an architectural project, not a bug-fix.
8. **Waterfall Ceiling is strict.** A gate sits between every wave — no UI polish while lower layers leak.
9. **`agents.md` source-citation contract is fabricated.** The *principle* of writing a README for agents is adopted (see `AGENTS.md`), but no pretend "cite-source" methodology is cited.
10. **Wave 0 is blocking.** No code written anywhere until exported BRANCH + LOOP flows + AP staging token are provided; otherwise PBWG will hallucinate JSON again.

---

## 4. The 10 Sovereign Capabilities (what must exist)

| # | Capability | Summary |
|---|---|---|
| C1 | Universal Absorption 360° | URL → DNA across website + GMaps + IG + competitors + language + tone + currency + compliance |
| C2 | Dynamic Employee Spawner | Generates employees (count, names, roles, dedicated prompts) per tenant |
| C3 | PBWG Flow Engine | Builds flows that *actually run* — via import + Parameter Binder + dynamic values |
| C4 | Flow Awareness + Conflict Detection | Edits existing flows; detects overlapping triggers |
| C5 | Zero-Leak Silent Execution | No tool names, no JSON to user — results only |
| C6 | Continuous Re-absorption | Every message / website change → TenantBrain update |
| C7 | Real Multi-tenancy + Compliance Router + Data Residency | Verified tenant isolation; PDPL / GDPR / HIPAA / LGPD; region-aware DB |
| C8 | UsageMeter + Wallet Metering | Every LLM token, flow exec, MCP call, scrape counted live |
| C9 | Proactive Intelligence + Dosing | day1 → day3 → week1 → … tone and depth; proactive alerts |
| C10 | SafetyLayer + Crisis Routing | Crisis detection → immediate human + AI pause; per-country hotlines |

---

## 5. Execution matrix — 7 Waves × 3 Lanes

> **Gate rule**: Wave N+1 does not begin until Wave N's acceptance test passes with **real bytes** (Sheet row, Slack message, webhook fire). No mocks.

### Wave 0 — Ground Truth (READ-ONLY · blocking)

| Lane | Deliverable |
|---|---|
| A (AP) | `scripts/fetch-templates.py`, `scripts/fetch-pieces-schemas.py` → `data/ap-templates.json`, `data/pieces-schemas.json` |
| A | `data/exported-flows/` — 3 JSON exports (BRANCH, LOOP, proven `vEWK9tqzYluFJNgZdyK5i`) |
| B (orch) | `reports/multi-tenancy-gap.md` ✅ · `reports/security-audit.md` ✅ · `reports/test-coverage-map.md` ✅ · `reports/empirical-findings.md` ✅ |
| C (FE) | `reports/voice-leaks.md` (to be produced in Siyadah-6.5 repo, not here) |

**Accepted when:** 687 pieces cached locally, 420 templates cached, exported flows saved.

### Wave 1 — Foundational data + real multi-tenancy (Lane B heavy)

| Lane | Work |
|---|---|
| B | Introduce `X-Siyadah-Tenant` header requirement on every `/v2/*` write endpoint; add `require_tenant()` middleware verifying the caller's API key is authorised for that `project_id` |
| B | Remove `DEFAULT_PID` fallback on all 14 write sites (see `reports/multi-tenancy-gap.md`); keep it only for `GET /`, `/health` |
| B | Split `main.py` into modules (section 7 below) |
| B | Replace 36 bare `except Exception` with typed handlers + structured logging |
| C | Add `TenantBrain` class; hydrate from orchestrator's `/v2/client-status` |
| C | Revive 6 dead brain variables (maturityLevel, strategicFocus, opportunityScore, competitorContext, founderIntent, v10_sessions.context) |

### Wave 2 — Prompt Composer + Voice Purge (Lane C heavy)

8-layer Prompt Composer; enable Anthropic ephemeral prompt caching; purge
voice leaks from `skill-loader.ts:463-464` and sibling sites; Consulting
Engine produces next-best-question from Company-Profile gaps.

### Wave 3 — PBWG + Dynamic-values fix + BRANCH fix (cross-lane, THE critical wave)

Golden 20 patterns from exported flows; Parameter Binder (code-level, not
prompt-level) fills `{{trigger.body.*}}`; Branch Builder imports
`data/exported-flows/branch-reference.json` instead of generating JSON.

**Acceptance — the real E2E:**

1. `curl POST /api/chat/stream` with Arabic instruction to save leads + notify Slack.
2. System imports pattern → deploys flow.
3. `curl` webhook with `{"name":"أحمد","email":"test@test.com"}`.
4. **Open Google Sheet with human eyes** — see row.
5. **Open Slack** — see notification.
6. Repeat for a BRANCH flow; both branches fire correctly on different field values.

If this fails, Wave 4 does not start.

### Wave 4 — Flow awareness + compliance router + data residency (Lane B)

`flow-awareness` proposes edits over duplicates (urgent — 49 duplicate flows
detected in production, see `reports/empirical-findings.md`); `conflict-detector`
catches overlapping triggers; `compliance-router` dispatches by tenant
jurisdiction; `data-residency` enforces region-locked Postgres replicas;
expand `ALLOWED_TOOLS` with `edit_existing_flow`, `dry_run_flow`, etc.

### Wave 5 — Chat-as-Kernel UI + Dynamic Employee Spawner + UsageMeter (Lane C + B)

`/chat` fullscreen, multi-thread, journey panel, command palette. Spawner
replaces any static employee catalogue. `UsageMeter` records `(tenant_id,
event, quantity, cost)` to Redis counter + Postgres audit; wallet widget
shows live consumption; alert at 80 %, block at 0 %, optional auto-recharge.

### Wave 6 — Reactive brain + dosing + SafetyLayer + continuous re-absorption

Decision engine re-ranks on any TenantBrain change. Dosing state machine
switches tone (day1 / day3 / week1 / week3 / month1 / veteran). SafetyLayer
classifier pauses AI and alerts humans on crisis signals (self-harm, fraud,
severe complaint). Weekly cron re-scrapes tenant website and diffs.

### Wave 7 — Brutal QA + launch readiness

Finish main.py modularisation; mandatory Langfuse on staging; Redis-backed
rate limit replaces in-memory; 15 Playwright scenarios (5 drawn from the 40
catalogue); gstack `/cso` + `/qa` 4-axis brutal QA audit; 0 critical CVE.

---

## 6. Critical files (by lane and wave)

### Lane B (this repo)

- `main.py` — all 14 `DEFAULT_PID` writes (lines listed in `reports/multi-tenancy-gap.md`)
- `main.py:1761` — CORS hardening (Wave 1 / fixed in commit 2 of this PR)
- `main.py:1769-1772` — API-key comparison (Wave 1 / fixed in commit 2 of this PR)
- `main.py:1203+` — `_build_smart_pulse` — needs Parameter Binder (Wave 3)
- `models.py:21-73` — schema already keyed on `project_id`; add `tenants` table with API-key → project_id mapping (Wave 1)
- `ingestion.py:33-69` — replace single-prompt Claude analysis with 360° pipeline (Wave 1 → C1)
- `mcp_sse.py:27-60` — bind SSE session to tenant (Wave 1)

### Lane C (Siyadah-6.5 repo — *not writable from here*)

- `src/lib/skill-loader.ts:463-464` — voice leaks
- `src/stores/chat-store.ts:24` — hardcoded WELCOME
- `src/app/api/chat/*` — error messages with "عذراً / يرجى" (Wave 2)
- New files `src/lib/v10/{tenant-brain,facts-store,pattern-library,decision-engine}.ts` (Wave 1)

### Lane A (AP runtime)

- `data/ap-templates.json` — generated Wave 0 (420 templates)
- `data/pieces-schemas.json` — generated Wave 0
- `data/exported-flows/*.json` — uploaded by user Wave 0

---

## 7. Modularisation (Wave 7) — adjusted from user's proposal

```
siyadah-orchestrator/
├── app.py                       # ≤ 100 lines — FastAPI instance + router mounts
├── core/
│   ├── config.py                # env vars, version, constants
│   ├── security.py              # CORS, API-key (hmac), require_tenant middleware
│   └── logging.py               # structured log + Langfuse hook
├── db/
│   ├── session.py               # SQLAlchemy async session
│   └── models.py                # moved from root models.py
├── engine/
│   ├── client.py                # SiyadahEngine class (main.py:76)
│   ├── golden_protocol.py       # IMPORT → LOCK → PUBLISH → VERIFY
│   ├── parameter_binder.py      # Dynamic values fix (Wave 3)
│   ├── conflict_detector.py     # Wave 4
│   ├── flow_awareness.py        # Wave 4
│   └── branch_builder.py        # Wave 3
├── services/
│   ├── tenant_manager.py        # verify_tenant_owns_project(api_key, project_id)
│   ├── compliance_router.py     # Wave 4
│   ├── data_residency.py        # Wave 4
│   ├── usage_meter.py           # Wave 5
│   ├── ingestion_360.py         # Wave 1 — C1
│   └── proactive.py             # Wave 6
├── routes/
│   ├── health.py                # / , /health
│   ├── build.py                 # /v2/build-* endpoints
│   ├── flow.py                  # /v2/flows/* endpoints
│   ├── project.py               # /v2/project/* , /v2/identity/*
│   ├── mcp.py                   # /v2/mcp/* + mcp_sse router
│   ├── usage.py                 # /v2/usage/* (Wave 5)
│   └── admin.py                 # /v2/client-status, /v2/saas/*
├── schemas/                     # Pydantic models extracted from main.py
├── helpers/                     # build_trigger, build_action, fuzzy_match, etc.
└── tests/
    ├── test_multi_tenancy.py    # verifies tenant isolation
    ├── test_security.py         # CORS + API-key timing
    └── test_golden_protocol.py  # IMPORT → LOCK → PUBLISH E2E
```

Difference vs. user's proposal: I keep `db/`, `schemas/`, and `helpers/`
because they were implied but missing; without them `engine/` and `routes/`
end up with circular imports.

---

## 8. Acceptance gates

| Gate | When | Criterion |
|---|---|---|
| G1 Waterfall Ceiling | Between every wave | Prior wave acceptance passed |
| G2 No-Hallucination | Every commit | Every claim in commit message has `file:line` |
| G3 Real Bytes | Wave 3+ | Bytes reach real Sheet / Slack / webhook |
| G4 Voice Silence | Wave 2+ | `grep` for forbidden Arabic phrases = 0 in output paths |
| G5a Tenant Isolation (schema) | Wave 1+ | Query with tenant A never returns tenant B's rows |
| G5b Tenant Isolation (auth) | Wave 1+ | Caller with tenant A credentials cannot write to tenant B's `project_id` (empirically confirmed not enforced today) |
| G6 Cost parity | Wave 5+ | UsageMeter within ±5 % of actual Anthropic + Stripe bill |
| G7 Compliance per jurisdiction | Wave 4+ | KSA tenant's PHI never leaves KSA region; audit log proves it |
| G8 Dynamic Values Integrity | Wave 3+ | After any flow build, all `{{…}}` references an existing upstream step; zero un-resolvable placeholders |

---

## 9. Wave 0 inputs required from user (BLOCKING)

1. AP staging token + `project_id`.
2. Exported BRANCH flow JSON from AP UI.
3. Exported LOOP flow JSON.
4. Flow `vEWK9tqzYluFJNgZdyK5i` exported.
5. Google Sheet staging ID + Slack staging channel + a webhook URL.
6. MCP repo-access scope for `ALKHANFAR/Siyadah-6.5` (currently restricted here to `alkhanfar/siyadah-orchestrator`). Without it, Lane C work has to be handed to the user manually.
7. Anthropic API key confirmed to support ephemeral prompt caching.
8. Stripe or Moyasar staging account for `UsageMeter` wallet testing.

---

## 10. Verification plan

### After this PR

```bash
# 1. Confirm docs added
ls docs reports

# 2. Confirm CORS now env-driven and defaults safe
grep -n "ORCHESTRATOR_ALLOWED_ORIGINS" main.py

# 3. Confirm API-key compared via hmac.compare_digest
grep -n "compare_digest" main.py

# 4. Syntax check
python3 -m py_compile main.py
```

### After Wave 3 (the real E2E)

```bash
# Submit instruction via chat
curl -X POST "$SIYADAH_BFF_URL/api/chat/stream" \
     -H "Authorization: Bearer $TEST_TOKEN" \
     -d '{"message":"احفظ كل ليد في Sheet وارسل Slack"}'

# Fire webhook against the deployed flow
curl -X POST "$AP_WEBHOOK_URL" \
     -H "Content-Type: application/json" \
     -d '{"name":"أحمد","email":"a@test.com"}'

# Manual verification
open "$GOOGLE_SHEET_URL"   # expect a new row with أحمد
open "$SLACK_CHANNEL_URL"  # expect a notification
```

### After Wave 5 (UsageMeter)

```bash
# Compare meter with Anthropic billing
curl -s "$ORCH_URL/v2/usage/$TENANT_ID" -H "X-API-Key: $KEY" | jq .llm_tokens
# Cross-check against Anthropic usage dashboard
```

---

## 11. What this PR delivers (now)

- `docs/PLAN.md` — this file.
- `reports/multi-tenancy-gap.md` — 14 `DEFAULT_PID` write sites with `file:line`.
- `reports/security-audit.md` — 11 findings (F1–F11), each with `file:line` + proposed patch.
- `reports/test-coverage-map.md` — what existing tests verify vs assume.
- `reports/empirical-findings.md` — live production probe (687 pieces, 420 templates, 49 duplicate flows).
- Two code fixes (separate commit):
  - `main.py:1761` — CORS origins driven by new `ORCHESTRATOR_ALLOWED_ORIGINS` env var.
  - `main.py:1769-1772` — API key compared with `hmac.compare_digest`.

What this PR **does not** do: touch `tenant_id`, split `main.py`, or change
BFF contract. Those require Wave 0 inputs + cross-repo coordination.

---

*End of plan.*
