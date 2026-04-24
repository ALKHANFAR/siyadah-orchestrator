# CLAUDE.md — siyadah-orchestrator

> **For AI agents working on this repo.** Read this before touching code.
> The companion spec at `AGENTS.md` describes the 3-system platform; this
> file is the working map for the orchestrator only.

---

## What this repo is

FastAPI service on Railway that translates intent → Activepieces flow JSON,
owns per-tenant institutional memory, and exposes 17 MCP tools + SSE sessions
for the chat agent. Serves only the BFF (`Siyadah-6.5`), never a browser
directly.

- Production host: `siyadah-orchestrator-production.up.railway.app`
- Default branch: `main` · active dev branch: `claude/check-node-env-0Lldj`
- Python 3.11 · FastAPI · httpx · SQLAlchemy-async · asyncpg · Redis
- Activepieces Golden Protocol **v5** (code reality — do not "upgrade" to v6
  in docs unless you also update `main.py:1270 golden_build`)

---

## Current state (Wave-1 complete)

7-phase remediation landed on branch `claude/check-node-env-0Lldj`:

| Phase | What it closed | Module |
|---|---|---|
| 1 | Silent multi-tenant leak in 17 /v2/ sites; SSE unbound; bare excepts; Postgres TLS | `auth.py`, `main.py`, `mcp_sse.py`, `database.py` |
| 2 | Zero retries on AP 5xx; zero rate limiting; unbounded httpx pool | `limits_config.py`, `main.py` |
| 3 | Plain-text logs without tenant/request correlation; no Sentry | `logging_config.py` |
| 4 | "Orphan flow" problem (BFF lost track of built flows) | `models.FlowRegistry`, `/v2/flows/{id}/register-employee`, `/v2/flows?orphan=true` |
| 5 | No CI, no integration tests | `tests/`, `.github/workflows/ci.yml` |
| 6 | BFF couldn't ask "is this already built?" | `/v2/flows/{id}/graph`, `/v2/flows/check-duplicate` |
| 7 | Docs drift | this file |

**57/57 harsh pytest cases green** against real Postgres 16 + Redis 7 in
~12s locally. Rollout is gated by `REQUIRE_TENANT_ENFORCE=false` (dry-run
default) — see `docs/WAVE-1-ROLLOUT-CHECKLIST.md`.

---

## Module map

| File | Lines | Responsibility |
|---|---|---|
| `main.py` | ~4700 | FastAPI app, 50+ /v2 routes, `SiyadahEngine` AP client, templates, presets, MCP tool dispatcher. Monolithic — split is Phase 8+. |
| `auth.py` | ~250 | `require_tenant` middleware. Resolves `X-API-Key` against `tenant_api_keys`; verifies `X-Siyadah-Tenant`; writes `tenant_audit_log`. |
| `limits_config.py` | ~60 | slowapi `Limiter` keyed on `request.state.project_id`. Redis storage. |
| `logging_config.py` | ~190 | structlog JSON renderer + secret scrubber + Sentry init + contextvars helpers. |
| `models.py` | ~180 | SQLAlchemy models: `Project`, `ProjectIdentity`, `KnowledgeAsset`, `AutonomousSetting`, `TenantApiKey`, `TenantAuditLog`, `FlowRegistry`. |
| `database.py` | ~140 | asyncpg engine + Base. Three-tier Postgres TLS (PG_CA_BUNDLE / SIYADAH_SKIP_PG_SSL / legacy). |
| `mcp_sse.py` | ~310 | SSE transport. Sessions bound to `request.state.project_id`; cross-tenant 403. |
| `tests/conftest.py` | ~220 | pytest fixtures: schema reset, Redis flush, 3 tenants A/B/C seeded. |
| `tests/test_phase_*.py` | ~900 total | 57 harsh tests covering Phases 1-4 + 6. |

---

## Auth model (Wave-1)

Every `/v2/*` request goes through `auth.require_tenant` middleware:

1. **`X-API-Key`** (required) — raw key, sha256'd server-side, looked up in
   `tenant_api_keys.key_hash` (unique, indexed). Absence or miss → **401**
   always, even in dry-run.
2. **`X-Siyadah-Tenant`** (required when enforced) — must equal the
   project_id the key is bound to. Mismatch → **403**. In dry-run mode
   (`REQUIRE_TENANT_ENFORCE=false`, default) violations are written to
   `tenant_audit_log` but the request passes through.
3. On success: `request.state.project_id` is set. All route handlers read
   it via `resolve_pid(request, body.project_id)` — never trust the body
   field directly.

### Bootstrap fallback

If `tenant_api_keys` is empty AND the legacy `ORCHESTRATOR_API_KEY` env
matches, the request is accepted with `project_id=None`. This path exists
only so prod survives the window between migration and seeding; remove
once seeded.

---

## Env matrix

See `.env.example` for the canonical list. Must-configure for prod:

| Var | Purpose | Prod value |
|---|---|---|
| `DATABASE_URL` | Postgres | Railway-provided |
| `REDIS_URL` | Rate-limit storage + SSE sessions | Railway-provided |
| `AP_BASE_URL` | Activepieces REST root | `https://activepieces-production-2499.up.railway.app` |
| `AP_EMAIL`, `AP_PASSWORD` | Engine login | operator secret |
| `AP_PROJECT_ID` | Default project fallback | single-tenant dev value |
| `ORCHESTRATOR_API_KEY` | Legacy bootstrap key (and seed for `tenant_api_keys`) | operator secret |
| `ORCHESTRATOR_ALLOWED_ORIGINS` | CSV of BFF origins | `https://app.siyadah.ai` |
| `REQUIRE_TENANT_ENFORCE` | `false` (dry-run) → `true` (block on violation) | flip to `true` after 24h clean audit |
| `PG_CA_BUNDLE` | Path to Railway CA bundle | prod: set it |
| `SIYADAH_SKIP_PG_SSL` | Explicit opt-in to CERT_NONE | leave unset in prod |
| `SENTRY_DSN` | optional — error reporting | set if using Sentry |

---

## Running the test suite

```bash
# one-time: local Postgres + Redis
sudo -u postgres psql -c "CREATE USER sy WITH PASSWORD 'sy' SUPERUSER;"
sudo -u postgres psql -c "CREATE DATABASE siyadah_test OWNER sy;"
redis-server --daemonize yes --port 6380

# venv + deps
python3 -m venv .venv_test
.venv_test/bin/pip install -r requirements.txt pytest pytest-asyncio

# run harsh suite (57 tests, ~12s)
.venv_test/bin/python -m pytest tests/ --ignore=tests/integration_phase_1_4.py
```

CI runs the same suite against service-container Postgres 16 + Redis 7 on
every push — see `.github/workflows/ci.yml`.

---

## Common tasks — where to start

| Task | Touch |
|---|---|
| Add a new `/v2/` endpoint | `main.py`. Add `request: Request` to signature. Call `resolve_pid(request, body.project_id)` — never `body.project_id or DEFAULT_PID`. Add `@limiter.limit("N/minute")` if it's a write. |
| Add a new MCP tool | `v2_mcp_tools` (line ~4199) + `_mcp_dispatch` handler. Update `docs/WAVE-1-DESIGN.md` tool-count claim. |
| Add a new model | `models.py`. Add import to `database.py:init_db` so the table is created on startup. |
| Change auth behaviour | `auth.py`. Add a test in `tests/test_phase_1_tenant.py`. Never weaken API-key enforcement — only tenant enforcement is dry-runnable. |
| Change rate limits | `main.py` `@limiter.limit(...)` decorators. Keep SSE handshake unlimited. |
| Add a secret pattern to scrub | `logging_config._SECRET_PATTERNS`. Add a test in `tests/test_phase_3_logging.py`. |

---

## Safety rails

1. **Golden Protocol v5** is code reality. Don't rename it to v6 in
   docs unless you also change `golden_build` itself.
2. **`DEFAULT_PID` is a last-resort** — every production call must arrive
   via `resolve_pid(request, ...)` which prefers `request.state.project_id`.
3. **Never log raw `X-API-Key`** — only the sha256 hash. The scrubber in
   `logging_config.py` catches accidental leaks but defence-in-depth is
   cheap; use `request.state.api_key_hash` when you need to identify the
   caller in a log.
4. **Cross-tenant reads must 404, not 403** — hiding existence is the
   contract (`v2_register_employee`, `v2_flow_graph`, `v2_check_duplicate`).
5. **SSE handshake is not rate-limited on purpose.** Long-lived streams
   would exhaust the budget on connect and starve subsequent tool calls.

---

## Hard NO

- Don't weaken `require_tenant` — if the legacy bootstrap path causes
  friction, seed `tenant_api_keys` instead of bypassing.
- Don't replace `tenacity` with a hand-rolled retry — it misses 4xx
  exclusion and jitter.
- Don't write to the frontend's `digital_employees` table from the
  orchestrator. The orphan bridge gives the BFF enough info to write
  its own row (`/v2/flows/{id}/register-employee` response).
- Don't skip the harsh suite. 57 tests run in 12s — there is no excuse.

---

## Plan file

Full 7-phase remediation plan lives in
`/root/.claude/plans/rippling-dreaming-knuth.md`. Consult it before
starting a large refactor.
