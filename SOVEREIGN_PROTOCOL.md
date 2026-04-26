# 🛡️ SOVEREIGN PROTOCOL v1.0

> **The one document that explains what this orchestrator is, why it exists,
> how it stays safe, and how to operate it without asking anyone.**
>
> Designed so the newest engineer on the team can read this once and ship
> code on day two without breaking anything load-bearing.

**Version:** 1.0 — sealed 2026-04-26
**Repo:** `github.com/ALKHANFAR/siyadah-orchestrator`
**Production host:** `siyadah-orchestrator-production.up.railway.app`
**Last sealed commit:** `bc49206` (Gap 4 — Sovereign OAuth + Phase 12a — Dahae)
**Companion specs:** `AGENTS.md` (this repo), `frontend/AGENTS.md`, `frontend/docs/gstack/`

---

## 1. الفلسفة السيادية — Why this thing exists

Siyadah is not "an automation tool", "a CRM", or "a chatbot". It is the
**operating system of an autonomous company**:

1. It absorbs a business identity from a single URL (Firecrawl + Claude Sonnet).
2. It stands up the company's digital workforce (sales agent, support agent,
   ops watcher, accountant, …) without human intervention.
3. It runs operations 24/7 with **0% required human touch**.
4. It keeps growing the business **even if the founder is on a one-year sabbatical**.

This repo — `siyadah-orchestrator` — is **the engine**. It is the FastAPI
service that:

- Translates intent → an Activepieces flow (the Golden Protocol v5 pipeline).
- Owns institutional memory per tenant (sector, FAQs, brand keywords).
- Runs the OAuth hand-off for every connected SaaS integration with bank-grade
  envelope encryption.
- Speaks MCP + SSE so the chat brain (in `Siyadah-6.5`) can call it safely.

**It is the engine. The brain lives in Siyadah-6.5.** Don't put LLM calls,
prompts, or intent classification here. The orchestrator is the safe,
predictable execution layer the brain talks to.

> Slogan: _"خذ إجازة لمدة سنة.. وشركتك ستنمو وتتماسك بفضل سيادة."_

---

## 2. The three-system architecture

```
User (Arabic chat)
   │
   ▼
┌──────────────────────────────────────────────────────────────┐
│  Siyadah-6.5  (Next.js 16 BFF + Brain)                       │
│  • intent-classifier.ts  (Arabic NLU + Claude fallback)      │
│  • consultant.ts + skill-loader.ts  (system prompt)          │
│  • /api/chat + /api/chat/stream  (ReAct loop, 3 turns)       │
│  • /api/orchestrator/[...path]  (BFF, ~30 path allowlist)    │
└────────────────────┬─────────────────────────────────────────┘
                     │ X-API-Key + X-Siyadah-Tenant
                     ▼
┌──────────────────────────────────────────────────────────────┐
│  siyadah-orchestrator  (FastAPI / Railway / THIS REPO)       │
│  • Golden Protocol v5  (CREATE → IMPORT → GET → LOCK → ...)  │
│  • Sniper Validator  (piece_validator.py)                    │
│  • OAuth Saga + Envelope Encryption                          │
│  • Piece Registry (688 pieces) + Dahae ranker                │
│  • MCP/SSE transport + Proactive Intelligence                │
└────────────────────┬─────────────────────────────────────────┘
                     │ Golden Protocol v5
                     ▼
┌──────────────────────────────────────────────────────────────┐
│  Activepieces  (self-hosted, Railway)                        │
│  • 688 pieces, 15+ categories                                │
│  • Project ou4jOTA4KMnDrzOVsKWvd                             │
└──────────────────────────────────────────────────────────────┘
```

**Hard rule:** Siyadah-6.5 **never** calls Activepieces directly. Every write
goes through this orchestrator. The orchestrator **never** surfaces UI.
Activepieces **never** sees a tenant identity (it sees only the Siyadah
project ID).

---

## 3. The 688 pieces — what they are, how we rank them

Activepieces ships ~688 "pieces" — each one is a connector to a SaaS
(Gmail, Slack, HubSpot, Stripe, …) plus its actions and triggers.

We mirror that catalogue into our own Postgres table `piece_registry`
(`models.py:213`) so the chat brain can:

1. Validate flow JSON before sending it to Activepieces.
2. Rank pieces by usefulness for a given sector.

### 3.1 Sniper Validator — `piece_validator.py`

The validator walks every step of a proposed flow and refuses it before AP
even sees it if any of these are true:

- The piece name is not in `piece_registry`.
- The action/trigger name doesn't exist in that piece's schema.
- A required prop is missing or contains a banned placeholder
  (`TODO_REPLACE_WITH_…`).
- The auth type doesn't match what the connection provides.

This is what stopped the infamous `ai.ask` vs `ai.askAi` bug from ever
reaching production.

### 3.2 Dahae Score — Phase 12a (`scripts/compute_dahae_scores.py`)

For every piece, we compute a single number **`effective_dahae` ∈ [0, 100]**
that captures "how much real automation power does this piece carry".

```
effective_dahae = dahae_score × (1 − laziness_score / 100)
```

Where `dahae_score` is a 5-component composite (breadth, richness,
compression, symmetry, adoption) and `laziness_score` penalises pieces
whose action names are repetitive or whose props are mostly empty.

**Empirical distribution against 688 production pieces:**

```
min=4   median=20   p75=28   p95=39   max=53
```

**Top 5:** mailchimp(53), hubspot(50), bexio(49), dimo(49), google-sheets(49).
**Bottom:** single-action API wrappers (`*-lookup`, `*-verifier`, …) at
effective=5.

The chat brain in `skill-loader.ts` should consult this score when injecting
its tool catalogue into Claude's prompt — top-Dahae pieces first.

### 3.3 The bridge — `/v2/logic/suggest`

```python
POST /v2/logic/suggest
{ "project_id": "<tenant>" }

→
{
  "sector": "E-commerce",
  "suggestions": [...],          # 3 hardcoded sector recipes
  "recommended_pieces": [...],   # top 8 pieces ranked by effective_dahae
  "intelligent": true,
  "_hint": "..."
}
```

The endpoint joins `ProjectIdentity.sector` against
`SECTOR_CATEGORY_MAP` (`main.py:3669`) and queries `piece_registry` ordered
by `effective_dahae DESC` filtered by category overlap.

---

## 4. Zero-Trust security — five layers

### 4.1 Envelope encryption for OAuth tokens — `siyadah_crypto.py`

Every encrypted token row carries its own 32-byte DEK (Data Encryption Key).
The DEK is wrapped under a single Master Key from `SIYADAH_OAUTH_MK` env.

```
plaintext  → AES-GCM-256 with random DEK + IV  → ciphertext
DEK         → AES-GCM-256 with MK + IV         → wrapped_dek
```

**Why:** if one DEK leaks, exactly one token is exposed. If MK leaks, you
re-wrap every DEK without re-encrypting any token. **Crypto agility** is
captured by `encryption_version` on every row — so we can roll forward
to AES-SIV or post-quantum primitives without a flag day.

The plaintext token is **never** held in a Python attribute beyond the
single function frame that uses it. After `engine.refresh()` finishes,
the variable goes out of scope and Python GC reclaims it.

### 4.2 OAuth saga — `oauth_routes.py`

Every OAuth flow is a state machine:

```
INITIATED  →  TOKEN_OBTAINED  →  AP_CONNECTION_CREATED  →  COMPLETED
                    │
                    └────────────→  COMPENSATED   (rollback path)
```

If any post-token step fails (e.g. Activepieces rejects the connection
creation), the orchestrator **deletes the encrypted_tokens row** and marks
the saga `COMPENSATED`. There are **never** orphaned tokens. The contract
is called **"Sovereign No-Ghost"**: a saga is either green end-to-end or
fully rolled back.

### 4.3 HMAC + PKCE state — `siyadah_oauth_state.py`

The OAuth `state=` URL parameter is an HMAC-SHA256 token signed with
`SIYADAH_OAUTH_STATE_KEY`. Plus we layer:

- **PKCE S256** (RFC 7636) so a stolen authorization code can't be replayed
  without the verifier.
- **Redis nonce single-use store** — every state token is one-shot. Re-use
  → 403.

### 4.4 Cross-replica refresh — `oauth_routes.py:_claim_due_tokens`

The token-refresh worker uses Postgres `SELECT … FOR UPDATE SKIP LOCKED`
plus a `processing_until` lease column. If a worker crashes mid-refresh,
the lease expires and another replica picks up the row — no double
charges, no dropped tokens. Verified with the gauntlet: 50 tokens × 2
concurrent cycles = exactly 50 provider calls, zero duplicates.

### 4.5 Webhook verification — `oauth_webhooks.py`

- **Slack:** v0= HMAC signature of `(ts, body)` with `SLACK_SIGNING_SECRET`,
  300-second timestamp window, replay deduplication via Redis.
- **Google RISC:** JWT verified with `PyJWKClient` + JWKS caching.
  `alg=none` defended explicitly.

---

## 5. The five things that will trip you up

### 5.1 "Why is my flow stuck in DRAFT?"

`propertySettings: {}` is missing on at least one step. Run:

```
GET /v2/flows/{id}/diagnose
```

The diagnose endpoint walks the flow tree and reports any step with empty
or missing `propertySettings`. The Golden Protocol v5 `_build_step_from_spec`
in `main.py` injects this for every step we build, but if you ever write
flow JSON by hand you must include it.

### 5.2 "AP returned HTTP 200 but the trigger is `EMPTY`"

This is real. AP silently keeps the flow in DRAFT if the import didn't
materialise the trigger. That is why **Step ③ GET-verify is non-negotiable**.
`main.py:226 verify_flow` raises 500 if it sees `trigger.type == "EMPTY"`.
**Never trust HTTP 200 alone on a flow write.**

### 5.3 "I see DEFAULT_PID, is multi-tenancy fake?"

No. `DEFAULT_PID` is the **last-resort fallback** in `resolve_pid`
(`main.py:369`). Production calls flow through `request.state.project_id`
set by the auth middleware (`auth.py:require_tenant`). The fallback only
ever fires on local dev where there's a single tenant. Don't remove the
constant — it's the dev escape hatch — but don't trust it in prod.

### 5.4 "Why isn't my new tool showing up after a redeploy?"

Activepieces only loads pieces that are present at startup time. We do
not auto-fetch the 688 pieces on Railway boot — that would block the
health check and trigger rollback loops. Run:

```bash
python -m scripts.sync_pieces
```

It's idempotent. Diff is shown at the end.

### 5.5 "The forensic test says my data leaked"

Run `tests/forensic_production_evidence.py`. It scans `encrypted_tokens`,
`oauth_sagas`, and `tenant_audit_log` for any byte sequence matching
`xoxb-`, `xoxe-`, `ya29.`, `1//0`, or any literal copy of an env-bound
secret. Last forensic run: **0 leaks across all three tables.**

---

## 6. Files you will touch and what each does

| File | Lines | Role |
|---|---:|---|
| `main.py` | 4823 | FastAPI app, 50+ /v2/ routes, `golden_build`, presets, MCP dispatcher |
| `auth.py` | ~250 | `require_tenant` middleware, `hmac.compare_digest`, audit log |
| `database.py` | ~234 | asyncpg engine + Base + 3-tier Postgres TLS + idempotent migrations |
| `models.py` | ~565 | SQLAlchemy models (10 tables, including `piece_registry`, `encrypted_tokens`, `oauth_sagas`) |
| `piece_validator.py` | ~250 | Sniper Validator |
| `siyadah_crypto.py` | ~196 | Envelope encryption + crypto agility |
| `siyadah_oauth_state.py` | ~282 | HMAC state + PKCE + Redis NonceStore |
| `oauth_routes.py` | ~1200 | OAuth flow + refresh worker (FOR UPDATE SKIP LOCKED) |
| `oauth_webhooks.py` | ~530 | Slack HMAC + Google RISC |
| `oauth_providers.py` | ~117 | Provider config registry |
| `mcp_sse.py` | ~310 | MCP SSE sessions (Redis-backed) |
| `ingestion.py` | ~250 | Firecrawl + Claude website absorption |
| `logging_config.py` | ~190 | structlog JSON + secret scrubber + Sentry init |
| `limits_config.py` | ~60 | slowapi rate limiter |

### Scripts you should know

| Script | Purpose |
|---|---|
| `scripts/sync_pieces.py` | Refresh `piece_registry` from Activepieces |
| `scripts/compute_dahae_scores.py` | Compute Dahae + laziness + effective_dahae |
| `scripts/apply_phase9_migrations.py` | Forensic snapshot of schema diff before/after `init_db()` |
| `scripts/seed_tenant_key.py` | Mint a tenant API key for local dev |

### Tests you must not break

| Suite | What it proves |
|---|---|
| `tests/forensic_production_evidence.py` | Zero plaintext leaks in `encrypted_tokens`, `oauth_sagas`, `audit_log` |
| `tests/forensic_registry_audit.py` | 3-source reconciliation (DB cache vs AP live vs activepieces.com) |
| `tests/big_drop_10_master_flows.py` | 10 master flows push end-to-end |
| `tests/gauntlet_360.py` | Cross-replica safety (50 tokens × 2 cycles, no doubles) |
| `tests/run_loop1_hardening_demo.py` | Q1/Q2/Q4/Q9 hardening (lease, AAD, AP recovery, structural concurrency) |
| `tests/run_loop2_google_risc_demo.py` | 8/8 RISC scenarios green |
| `tests/run_loop3_e2e_demo.py` | End-to-end Slack uninstall flow |
| `tests/test_phase_9_crypto.py` | Envelope encryption properties |

---

## 7. Operational runbook

### 7.1 Local boot

```bash
python3 -m venv /tmp/siyadah_venv
/tmp/siyadah_venv/bin/pip install -r requirements.txt

export DATABASE_URL='postgresql://...'
export REDIS_URL='redis://...'
export SIYADAH_OAUTH_MK='...'
export SIYADAH_OAUTH_STATE_KEY='...'
export AP_BASE_URL='https://activepieces-production-2499.up.railway.app'
export AP_PROJECT_ID='ou4jOTA4KMnDrzOVsKWvd'
export ORCHESTRATOR_API_KEY='dev-only-key'

uvicorn main:app --host 0.0.0.0 --port 8000
```

### 7.2 Production deploy

Railway auto-deploys on push to `main`. Required envs are listed in
`AGENTS.md §Environment variables`. **Never** set `SIYADAH_SKIP_PG_SSL=1`
in prod after the rollout window — provision `PG_CA_BUNDLE` instead.

### 7.3 Smoke tests

```bash
# Liveness
curl -s $URL/health | jq

# Auth + tenant scoping
curl -s $URL/v2/client-status -H "X-API-Key: $KEY" | jq

# Brain bridge
curl -s -X POST $URL/v2/logic/suggest \
  -H "X-API-Key: $KEY" -H "X-Siyadah-Tenant: $PID" \
  -d '{"project_id":"'$PID'"}' | jq
```

### 7.4 When something is broken

Read `AGENTS.md §When the agent is unsure`. Six diagnostic endpoints will
tell you almost anything:

```
GET /health
GET /v2/client-status
GET /v2/flows/{id}/diagnose
GET /connections
GET /v2/connections/health
GET /v2/pieces/{name}/schema
```

---

## 8. Compliance posture (sealed at v1.0)

Audited against `AGENTS.md` (orchestrator + frontend) and the 8-paper
gstack retrospective on 2026-04-26. Result: **B+ on the orchestrator,
13 ✅ COMPLIANT / 6 ⚠️ PARTIAL / 1 ⚠️ GAP.**

The remaining items are documented as Phase-13 backlog and do not block
multi-tenant launch:

- B5 — auth scoped to `/v2/*` only; legacy `/connections` and `/templates`
  routes bypass enforcement. Mitigation: legacy routes don't accept writes.
- B7 — SSE session UUID without IP binding; no re-verify on POST
  `/v2/mcp/messages/{sid}`. Mitigation: tenant scoping is checked on
  connect, single-worker deploy mitigates the risk.
- B9 — 91 `except Exception` blocks. Mostly fire-and-forget logging
  fallbacks; narrow as we touch them.
- B12 — no circuit breaker on Firecrawl in `ingestion.py`. Mitigation:
  ingestion is offline; failures degrade to "no DNA absorbed" not "service
  down".
- B13 — in-memory SSE queues. Mitigation: Railway runs a single worker.
  If we ever scale horizontally, switch to Redis-backed asyncio.Queue.
- B14 — Sentry + structlog yes; Prometheus metrics no. Phase-13 work.

The full audit lives in this conversation transcript and will be archived
to `~/.gstack/analytics/` per gstack discipline.

---

## 9. The contract — what each agent promises

### 9.1 The orchestrator promises

1. Every `/v2/*` write is scoped to the tenant in `request.state.project_id`.
2. Every flow write goes through Golden Protocol v5 (no shortcut).
3. Every OAuth handshake is either fully green or fully compensated.
4. Every encrypted_tokens row carries its own DEK; no shared keys.
5. Every audit-log entry is append-only and tenant-scoped.

### 9.2 The orchestrator does not promise

1. To call any LLM (delegate to Siyadah-6.5).
2. To handle browser sessions (delegate to Siyadah-6.5).
3. To validate end-user-facing copy (delegate to Siyadah-6.5).
4. To run >1 worker without the SSE refactor.

### 9.3 The brain (Siyadah-6.5) promises

1. To never expose `ORCHESTRATOR_API_KEY` to the browser.
2. To never construct AP flow JSON inline — always go through
   `/v2/build-*` endpoints.
3. To always carry `X-Siyadah-Tenant` derived from session, never from
   request body.
4. To rate-limit `/api/chat` and `/api/chat/stream` symmetrically.

### 9.4 The brain does not promise

1. To enforce destructive-tool confirmation server-side (yet — open gap
   per gstack red-team D3).
2. To pass `req.signal` to Anthropic for SSE cancellation (yet — open
   gap per red-team D4).

These two open gaps are tracked as P0 for the next phase.

---

## 10. Where to read next

| Document | What you'll learn |
|---|---|
| `AGENTS.md` (this repo) | The 3-system contract, BFF→Orchestrator endpoints, Golden Protocol v5 |
| `frontend/AGENTS.md` | The 4-department architecture, intent classification, the 5-layer experiential philosophy |
| `frontend/docs/gstack/retrospective/00-master.md` | The 7-specialist audit verdict (B-/C+) and must-fix list |
| `frontend/docs/gstack/retrospective/02-cso-security.md` | OWASP Top-10 mapping with file:line evidence |
| `frontend/docs/gstack/retrospective/07-red-team.md` | The five "two-things-interact-wrong-at-once" scenarios |
| `frontend/SIYADAH-MASTER-PLAN.md` | The 11-phase roadmap (1057 lines) |
| `models.py` (this repo) | The 10 tables that hold every byte of state |

---

## 11. The promise this protocol enforces

> **سيادة = الكيان الرقمي البديل** — يمتص الشركة من رابط، يبني موظفيها
> الرقميين، يدير عملياتها كاملةً، ويستمر في العمل في غياب المؤسس.
>
> هذا ليس CRM. هذا ليس Chatbot. هذا ليس Marketing tool.
> **هذا نظام تشغيل شركة.**

Any technical decision that contradicts this promise is rejected.
Any feature that doesn't serve one of the four departments
(Workforce / Vault / War Room / Warehouse) is deferred.
Any complexity that leaks to the non-technical user is hidden.

**Golden rule:** chat 90%, dashboard 10%, non-technical user 100%.

---

**The Sovereign Seal — placed 2026-04-26.**
**Commit `bc49206` is the snapshot this protocol describes.**
