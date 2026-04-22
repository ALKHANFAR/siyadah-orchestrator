# Test Coverage Map — what's actually verified vs what's assumed

> Phase A of the empirical plan.
> Source: static reading of `test_v71_qa.py`, `test_mega_challenge.py`, `deploy_sondos_saas.py`.
> No network required for this map — everything derived from code.

---

## 1. TL;DR

The test suite looks impressive. It is **not**.

- **8 QA tests** + **1 mega-flow static builder** + **1 E2E deploy script** — ≈ 79 KB of test code.
- **Zero tests** verify a deployed flow **actually fires** and produces real bytes (Sheet row, Slack message, email).
- **Zero tests** verify multi-tenant isolation against **impersonation** — the existing "isolation" test only proves the DB rows differ, not that tenant A can't read tenant B through a crafted `project_id`.
- **Zero tests** exercise BRANCH logic under real input (both branches actually take their path).
- **Zero tests** exercise LOOP with real items.
- **Zero tests** verify dynamic values (`{{trigger.body.*}}`) get resolved at run-time.
- **Zero tests** for Arabic / RTL content end-to-end.
- **Zero tests** for cost runaway, rate limit, concurrent writes.

**Consequence**: the existing green checks in CI tell you the orchestrator is *alive*. They do not tell you it *works* for a real tenant.

---

## 2. Exact inventory

### `test_v71_qa.py` — 8 tests (33.5 KB)

| # | Test | `file:line` | What it actually verifies | What it does NOT verify |
|---|---|---|---|---|
| 1 | `test_isolation` | `test_v71_qa.py:71` | DB rows of project A ≠ DB rows of project B | Can caller with API key write to arbitrary project? Can they read arbitrary project via MCP? **No.** |
| 2 | `test_sse_stress` | `test_v71_qa.py:162` | SSE accepts 20 messages + 10 burst reads without crashing | Does SSE leak session between tenants? Is session bound to `project_id`? **No.** |
| 3 | `test_onboarding_flow` | `test_v71_qa.py:276` | apple.com → preview → register → auto_settings populated | Non-English / non-obvious site. Arabic merchant. Malformed HTML. Cloudflare challenge. **No.** |
| 4 | `test_suggest_engine` | `test_v71_qa.py:364` | Ghost project returns `sector=default` with onboarding hint | Does the engine respect sector-specific playbooks for real registered projects? **No coverage beyond default.** |
| 5 | `test_compression` | `test_v71_qa.py:425` | `compress_response` reduces size ≥ 50 %, strips nulls | Not a behavioural test — internal utility check only. |
| 6 | `test_live_ingestion` | `test_v71_qa.py:505` | Ingest apple.com → read back `business_description` via direct SQL | Only happy path. No quota handling, no Firecrawl outage, no rate limit. |
| 7 | `test_sse_trace` | `test_v71_qa.py:573` | Captures 3 ping events on SSE | Pings are server-driven; tests transport, not content. |
| 8 | `test_error_simulation` | `test_v71_qa.py:640` | Wrong API key → 401 | Missing key? Expired key? Key for other tenant? **No.** |

### `test_mega_challenge.py` — 1 challenge (30.8 KB)

| # | Challenge | `file:line` | What it actually verifies | What it does NOT verify |
|---|---|---|---|---|
| 1 | `run_mega_challenge` | `test_mega_challenge.py:629` | A 15-step flow JSON can be **built in-memory locally** + cross-step refs (`{{step_N}}`) present in strings | Does the flow actually DEPLOY to AP? Does it FIRE? Does each step RUN? **No.** |

Key quote from the file itself (`test_mega_challenge.py:73-80`):

> "Build the full 15+ step Activepieces flow JSON locally using builder primitives."

**Locally**. In memory. Never deployed. This is a JSON schema linter pretending to be an integration test.

### `deploy_sondos_saas.py` — 1 E2E script (14.4 KB)

| # | Stage | `file:line` | What it verifies | Limitation |
|---|---|---|---|---|
| 1 | `/health` probe | `deploy_sondos_saas.py:27-34` | Orchestrator alive + AP reachable | Binary |
| 2 | MCP `check_system_health` | `deploy_sondos_saas.py:41-48` | `projects_found` > 0 | Shape check |
| 3 | `/v2/build-complex` 5-step flow | `deploy_sondos_saas.py:51-201` | Build returns `flow_id` + `status=ENABLED` | **No webhook actually fired — no real bytes tested.** |
| 4 | `diagnose_flow` | `deploy_sondos_saas.py:228-243` | Deployed flow has the expected step count | Still static — doesn't run the flow |
| 5 | `test_webhook` | `deploy_sondos_saas.py:246-264` | MCP call returns a status | Calls orchestrator's own `test_webhook` tool; does NOT confirm the outer Activepieces engine actually executed the flow downstream (Sheet write, Gmail send). |

All of this runs against **one hard-coded project** (`ou4jOTA4KMnDrzOVsKWvd`, `deploy_sondos_saas.py:211`). So it proves a flow deploys — it proves nothing about multi-tenancy.

---

## 3. Coverage against the 10 sovereign capabilities

| Capability | Covered? | Evidence |
|---|---|---|
| C1 Universal Absorption 360° | **Partial** | Only website via Firecrawl (`test_live_ingestion`). No GMaps, IG, competitors. |
| C2 Dynamic Employee Spawner | **Zero** | Concept doesn't exist in code. |
| C3 PBWG Flow Engine | **No** | Flows built from hand-written specs or prompt output; no pattern import, no Parameter Binder. `build_mega_flow_json()` shows a *reference* for what a pattern looks like, but it's not reused. |
| C4 Flow Awareness + Conflict Detection | **Zero** | No endpoint searches existing flows before building. |
| C5 Zero-Leak Silent Execution | **N/A at orchestrator level** | Lives in BFF. |
| C6 Continuous Re-absorption | **No** | Only one-shot ingestion. |
| C7 Real Multi-tenancy + Compliance Router + Data Residency | **No** | DB FK isolation only; no impersonation guard, no jurisdiction routing, no region-locked replicas. |
| C8 UsageMeter + Wallet Metering | **Zero** | No concept of LLM/flow/MCP cost tracking. |
| C9 Proactive Intelligence + Dosing | **Partial** | `/v2/logic/proactive-suggestions` exists (`main.py:3545`); no dosing state machine, no week-1/month-1 tone shift. |
| C10 SafetyLayer + Crisis Routing | **Zero** | No classifier, no escalation, no country-aware hotline map. |

---

## 4. Most dangerous blind spots (prioritised)

1. **Deployed flows never fire in tests.** The entire "it works" narrative rests on happy-path build-status codes. There is no published test that catches the "stuck in DRAFT" class of bugs beyond `_BOOLEAN_FIELD_NAMES` guard. **Fix: add `test_real_e2e_flow_fires` that fires a webhook and polls Google Sheet for the expected row within 30 s.**
2. **The isolation test is a false positive.** Two DB rows differing proves nothing about the auth contract. **Fix: add `test_impersonation_blocked` that tries to `POST /v2/build-complex` with tenant A's API key but `project_id=tenant_B` — must return 403 once Wave 1 lands; today it returns 200.**
3. **BRANCH under real input is un-tested.** `deploy_sondos_saas.py` deploys a router but never fires webhook payloads with `tier=HOT` vs `tier=WARM` vs missing, then verifies each branch actually ran. **Fix: 3 curl shots with 3 payloads + Sheet row check per branch.**
4. **No Arabic/RTL E2E.** apple.com is the only site in `test_live_ingestion`. A Saudi merchant Arabic site may produce completely different FAQs / tone / sector labels. **Fix: add `test_arabic_site_absorption` against a known Arabic URL.**
5. **No cost bound.** A single tenant can drive up the Anthropic bill in ingestion + suggest engine with no limit in code. **Fix: cap per-tenant daily LLM cost; test by running ingestion 50× and asserting the 50th is blocked.**
6. **Dynamic values bug is un-detected.** If `{{trigger.body.name}}` is left as literal text in a built flow, tests today don't notice — the flow still deploys ENABLED. **Fix: `test_dynamic_values_resolved` that diffs the deployed step inputs and fails if any `{{` remains un-replaced at run-time.**
7. **Concurrent tenant writes.** No concurrent-load test. Race conditions in `_auto_configure_settings` are possible (two ingestions for the same tenant → last-wins silently). **Fix: simulate 10 parallel ingestions against the same `project_id`; all but one should 409.**

---

## 5. What this means for the master plan

These blind spots reshape the acceptance gates (§8 of `docs/PLAN.md`):

- **G3 Real Bytes** is even more critical than previously stated — it is the **first** test the current suite has ever tried. Every wave from 3 onwards must add ≥ 1 real-bytes test case for each new pattern it unlocks.
- **G5 Tenant Isolation** needs a stricter definition: **"a holder of Tenant A's credentials cannot read or write Tenant B's state, period."** The DB-row-diff test is kept but re-labelled as G5.a (schema integrity), and G5.b (auth integrity) is added.
- **New gate proposal — G8 Dynamic Values Integrity**: after any flow build, parse all step inputs; any string containing `{{...}}` must reference a known upstream step name that actually exists. Zero un-resolvable placeholders allowed.

---

## 6. Reproducibility

```bash
# Inventory the existing tests
grep -nE "^async def test_|^def test_" test_v71_qa.py
grep -nE "^async def (run_mega_challenge|main)" test_mega_challenge.py deploy_sondos_saas.py

# Confirm the mega challenge builds in memory only (no deploy call)
grep -nE "(build-complex|build-and-deploy|build-dynamic|build-router|build-loop)" test_mega_challenge.py
# Empty — mega test never deploys

# Confirm the suggest engine coverage is only "default" / ghost projects
grep -n "sector == \"default\"" test_v71_qa.py

# Confirm deploy script is single-project
grep -n "ou4jOTA4KMnDrzOVsKWvd" deploy_sondos_saas.py
```
