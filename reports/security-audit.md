# Security Audit — siyadah-orchestrator

> Generated: 2026-04-22 · Branch: `claude/setup-siyadah-os-briefing-kboY9`
> Scope: read-only audit. Every finding verified against `file:line`.
> Methodology: OWASP Top 10 + STRIDE (per gstack `/cso`).

---

## 1. Scorecard

| # | Finding | File:Line | Severity | Fixed in this PR? |
|---|---|---|---|---|
| F1 | CORS allows any origin with credentials | `main.py:1761` | **Critical** | ✅ Yes (commit 2) |
| F2 | API-key compared with `!=` (timing-side-channel) | `main.py:1772` | **Critical** | ✅ Yes (commit 2) |
| F3 | Postgres SSL verification disabled | `database.py:34-35` | **Critical** | ❌ Needs env plumbing |
| F4 | Multi-tenant writes fall back to `DEFAULT_PID` | `main.py:1870,…` (14 sites — see `multi-tenancy-gap.md`) | **Critical** | ❌ Wave 1 |
| F5 | SSE session not bound to tenant | `mcp_sse.py:27-60` | High | ❌ Wave 1 |
| F6 | 36 bare `except Exception` blocks swallow errors | 36 occurrences in `main.py` | High | ❌ Wave 1 |
| F7 | No rate limit at orchestrator layer | none (grep returns 0 hits) | High | ❌ Wave 7 |
| F8 | No structured audit log of who did what | n/a | Medium | ❌ Wave 1 |
| F9 | No observability (Langfuse / OTel not wired) | n/a | Medium | ❌ Wave 7 |
| F10 | `ANTHROPIC_KEY`, `FIRECRAWL_KEY` read via `os.getenv` with no redaction in error paths | `ingestion.py:25-28`, `main.py` top | Low | ❌ Wave 7 |
| F11 | `main.py` is 4,110 lines; makes review prohibitive (availability of human reviewers is a security control) | `main.py` | Medium | ❌ Wave 7 |

---

## 2. F1 — CORS open to the world

### Evidence (`main.py:1759-1762`)

```python
app = FastAPI(title="Siyadah Orchestrator", version=VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
```

### Why it's critical

FastAPI logs a warning when `allow_origins=["*"]` is combined with
`allow_credentials=True`, and silently ignores the wildcard in that case.
But browsers still echo the `Origin` header back — meaning **any website
the user visits can issue authenticated requests to the orchestrator**
if the `X-API-Key` ever leaks into the browser (e.g. via a BFF bug).

### Fix (applied in commit 2)

```python
# core allowlist from env; fallback disables CORS to "never allow credentials cross-site"
_raw_origins = os.getenv("ORCHESTRATOR_ALLOWED_ORIGINS", "").strip()
_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["https://app.siyadah.ai"],  # safe default
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Siyadah-Tenant"],
)
```

Behaviour:

- In production: set `ORCHESTRATOR_ALLOWED_ORIGINS=https://app.siyadah.ai` (or CSV).
- Default (unset): hard-coded safe default `https://app.siyadah.ai` — not `*`.

---

## 3. F2 — API-key comparison is timing-leaky

### Evidence (`main.py:1769-1775`)

```python
async def api_key_check(request: Request, call_next):
    if ORCH_API_KEY and request.url.path.startswith("/v2/"):
        key = request.headers.get("X-API-Key", "")
        if key != ORCH_API_KEY:
            return JSONResponse(status_code=401,
                                content={"error": "Invalid or missing API key"})
    return await call_next(request)
```

### Why it's critical

Python's `!=` on `str` short-circuits on the first differing byte. Under
a remote timing attack (well-documented, e.g. CWE-208), an attacker can
recover the key byte-by-byte by measuring response latency.

### Fix (applied in commit 2)

```python
import hmac
...
async def api_key_check(request: Request, call_next):
    if ORCH_API_KEY and request.url.path.startswith("/v2/"):
        key = request.headers.get("X-API-Key", "")
        # constant-time comparison — resistant to remote timing attacks
        if not hmac.compare_digest(key.encode("utf-8"),
                                   ORCH_API_KEY.encode("utf-8")):
            return JSONResponse(status_code=401,
                                content={"error": "Invalid or missing API key"})
    return await call_next(request)
```

`hmac.compare_digest` is stdlib — no new dependency.

---

## 4. F3 — Postgres SSL verification disabled

### Evidence (`database.py:31-36`)

```python
if _raw_db_url and not _skip_ssl:
    import ssl as _ssl
    _ctx = _ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = _ssl.CERT_NONE
    _connect_args = {"ssl": _ctx}
```

### Why it's critical

Disabling hostname check AND cert verification defeats the purpose of SSL:
a man-in-the-middle between Railway's runtime and the Postgres pod could
silently read or rewrite every query. For a multi-tenant system that will
eventually hold PHI / PII under PDPL / GDPR, this is non-negotiable.

### Recommended fix (Wave 1, not in this PR)

```python
if _raw_db_url and not _skip_ssl:
    import ssl as _ssl
    _ctx = _ssl.create_default_context(
        cafile=os.getenv("DATABASE_CA_BUNDLE") or None
    )
    # Keep hostname + cert verification ON. If Railway provides a self-signed
    # chain, ship the CA via DATABASE_CA_BUNDLE env var, don't disable verify.
    _connect_args = {"ssl": _ctx}
```

Why not now: requires confirming the Railway CA bundle path. Out of scope
for this audit-only PR.

---

## 5. F5 — SSE sessions not tenant-bound

### Evidence (`mcp_sse.py:27-60`)

- `session_id` generated with `uuid.uuid4()`.
- Stored in Redis or in-memory with `SESSION_TTL = 3600`.
- `POST /v2/mcp/messages/{session_id}` accepts *any* caller who guesses or
  intercepts a session_id.
- No `project_id` written into the session blob; no check on message receipt.

### Why it's High (not Critical)

Mitigated by `ORCH_API_KEY`, but that key grants *all* tenants — so two
tenants sharing the same key could observe each other's SSE stream by
session-id guessing (low entropy: one uuid4 = 128 bits, acceptable alone —
but with zero tenant binding, a compromised session_id leaks the whole
MCP conversation).

### Recommended fix (Wave 1)

Bind `project_id` to session on open; verify on every message:

```python
session = {"project_id": request.state.project_id, "opened_at": now, …}
await _redis.setex(f"sse:{sid}", SESSION_TTL, json.dumps(session))
...
# on message POST
stored = await _redis.get(f"sse:{sid}")
if not stored or json.loads(stored)["project_id"] != request.state.project_id:
    raise HTTPException(403)
```

---

## 6. F6 — 36 bare `except Exception` blocks

Grep: `grep -c "except Exception" main.py` → **36**.

### Why it's High

Each one either:
- Silently swallows the error and returns 500 with no context, or
- Returns a fake-success path (e.g. empty list) that the caller treats as valid.

The briefing claimed 59; the actual count is 36. Still too many.

### Recommended fix (Wave 1)

- Replace with typed handlers (`httpx.HTTPError`, `asyncio.TimeoutError`, `sqlalchemy.exc.DBAPIError`, …).
- All unexpected exceptions bubble to a top-level FastAPI exception handler that:
  - Logs with correlation id + tenant id.
  - Reports to Sentry / Langfuse.
  - Returns 500 with a safe message.

---

## 7. F7 — No rate limiting

Grep: `grep -nE "(rate.?limit|throttle|bucket)" main.py` → zero matches.

### Why it's High

- A single tenant can exhaust the orchestrator's connection pool, its
  Anthropic quota, or its AP project quota.
- No back-pressure means cost runaway is a real fiscal risk once UsageMeter
  (Wave 5) is live.

### Recommended fix (Wave 7)

- Redis-backed token bucket keyed on `tenant_id + endpoint`.
- Tiered limits: `/v2/build-*` stricter than `/health`.
- Configurable per tenant in the `tenants` table (Wave 1).

---

## 8. Positive findings (what's already done right)

- ✅ FastAPI + Pydantic give automatic request validation — no raw body parsing.
- ✅ `_BOOLEAN_FIELD_NAMES` guard (`main.py:48-52`) prevents a whole class of AP "stuck-in-draft" bugs.
- ✅ Golden Protocol v5 (`AGENTS.md:202-213`) enforces `GET`-verify after writes — defence-in-depth against AP silently regressing.
- ✅ Redis fallback on `mcp_sse.py:48-49` gracefully degrades — no crash if Redis is down.
- ✅ `database.py:20-24` correctly normalises Railway's `postgres://` URL for asyncpg.
- ✅ `project_id` is a proper FK with `ON DELETE CASCADE` in `models.py:40,55,68` — no orphan rows on tenant deletion.

---

## 9. What this PR fixes today (commit 2)

| Finding | File | Change |
|---|---|---|
| F1 CORS | `main.py:1761` | Origins from `ORCHESTRATOR_ALLOWED_ORIGINS` env; safe default |
| F2 API key | `main.py:1772` | `hmac.compare_digest` |

All other findings are flagged here for their assigned waves.

---

## 10. Reproducibility

```bash
# Confirm every finding
grep -n "allow_origins" main.py
grep -n "!=" main.py | grep -i "api_key\|ORCH_API_KEY"
grep -n "CERT_NONE\|check_hostname" database.py
grep -c "except Exception" main.py
grep -nE "(rate.?limit|throttle|bucket)" main.py
grep -nE "body\.project_id \|\| DEFAULT_PID" main.py
grep -c "tenant_id" main.py            # returns 0 (confirms F4 root cause)
```
