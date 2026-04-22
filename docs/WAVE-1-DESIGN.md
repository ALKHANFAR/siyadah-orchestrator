# Wave 1 — Design Doc

> Status: **DRAFT — awaiting operator approval**
> Target branches: `siyadah-orchestrator` → `wave1-real-multitenancy`
>                 `Siyadah-6.5` → `wave1-bff-tenant-injection`
> PR pair is **atomic**: neither merges without the other.

---

## 1. Context

Wave-0 evidence (`reports/multi-tenancy-gap.md`, `reports/empirical-findings.md`,
`reports/security-audit.md`) proved the orchestrator's multi-tenancy is
half-built: schema isolates on `project_id`, auth does not verify caller
ownership. 14 write endpoints accept arbitrary `project_id` from the
request body; fallback to a global `DEFAULT_PID` when missing.

Wave 1 closes this gap end-to-end:

- New table `tenant_api_keys` maps `(api_key_hash → project_id + scopes)`.
- New middleware `require_tenant()` reads `X-API-Key` + `X-Siyadah-Tenant`
  and attaches a verified `project_id` to `request.state`.
- 14 write sites read `project_id` from `request.state`, never from body.
- Postgres TLS verification re-enabled (F3).
- SSE sessions bound to `project_id` (F5).
- 36 bare `except Exception` → typed handlers with structured logging (F6).

Coordinated BFF changes (Siyadah-6.5) are required — see §5.

---

## 2. Table of contents

| § | Section |
|---|---|
| 3 | Migration v9 — Postgres schema |
| 4 | Middleware pseudocode |
| 5 | BFF (Siyadah-6.5) breaking changes |
| 6 | 14 endpoint modifications |
| 7 | Postgres SSL hardening (F3) |
| 8 | SSE session binding (F5) |
| 9 | Exception handling (F6) |
| 10 | Rollback plan |
| 11 | Test matrix (before → after) |
| 12 | Deployment order + kill-switches |

---

## 3. Migration v9 — Postgres schema

New file: `db/migrations/v9_multitenancy.sql`. Idempotent (guarded by
`siyadah.schema_version` check).

```sql
BEGIN;

-- 3.1 tenant_api_keys: one row per issued key, bound to exactly one project.
CREATE TABLE IF NOT EXISTS tenant_api_keys (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id  VARCHAR(64) NOT NULL
              REFERENCES projects(project_id) ON DELETE CASCADE,
  key_hash    CHAR(64) NOT NULL,                -- sha256 of raw key; never stored raw
  label       VARCHAR(128) NOT NULL,            -- e.g. "siyadah65-bff-prod"
  scopes      TEXT[] NOT NULL DEFAULT ARRAY['read','write'],
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  revoked_at  TIMESTAMPTZ,
  last_used_at TIMESTAMPTZ,
  CONSTRAINT uq_tak_key_hash UNIQUE (key_hash)
);
CREATE INDEX IF NOT EXISTS ix_tak_project_id ON tenant_api_keys(project_id);
CREATE INDEX IF NOT EXISTS ix_tak_active
  ON tenant_api_keys(key_hash) WHERE revoked_at IS NULL;

-- 3.2 tenant_audit_log: who did what, when, from where.
CREATE TABLE IF NOT EXISTS tenant_audit_log (
  id              BIGSERIAL PRIMARY KEY,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  project_id      VARCHAR(64),                   -- null if unauthenticated
  api_key_hash    CHAR(64),                      -- which key did the write
  endpoint        VARCHAR(255) NOT NULL,         -- e.g. "POST /v2/build-complex"
  http_status     SMALLINT NOT NULL,
  payload_digest  CHAR(64),                      -- sha256(json.dumps(body))
  request_id      UUID,                          -- correlation id
  remote_ip       INET,
  user_agent      TEXT,
  violation       VARCHAR(64)                    -- null for normal writes; set in dry-run
);
CREATE INDEX IF NOT EXISTS ix_tal_project_occurred
  ON tenant_audit_log(project_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_tal_violation
  ON tenant_audit_log(violation) WHERE violation IS NOT NULL;

-- 3.3 Record this migration
INSERT INTO schema_version (version, applied_at)
VALUES (9, now()) ON CONFLICT DO NOTHING;

COMMIT;
```

**Seed row** (one-time, manual — NOT in the migration):

```sql
INSERT INTO tenant_api_keys (project_id, key_hash, label, scopes)
VALUES (
  'ou4jOTA4KMnDrzOVsKWvd',
  encode(sha256('<paste-raw-ORCHESTRATOR_API_KEY-here>'::bytea), 'hex'),
  'siyadah65-bff-prod',
  ARRAY['read','write']
);
```

`ORCHESTRATOR_API_KEY` env var is retained only as a **bootstrap seed**
for `tenant_api_keys`. After seeding, the orchestrator never compares
against it directly; it compares the caller's key hash to rows in
`tenant_api_keys`.

انتهيت من §1 + §2 + §3 (Migration v9). هل أكمل بـ §4 (Middleware pseudocode)؟

---

## 4. Middleware pseudocode

New file: `core/security.py`. Replaces the existing inline `api_key_check`
at `main.py:1768-1775`.

```python
# core/security.py
import hmac, hashlib, os, json, logging, uuid
from fastapi import Request
from fastapi.responses import JSONResponse
from db.session import async_session
from db.models import TenantApiKey
from sqlalchemy import select

log = logging.getLogger("siyadah.auth")

# Kill-switch. When True, violations are logged to tenant_audit_log with
# `violation=<reason>` but requests are allowed through. When False,
# violations return 401/403. Wave 1 deploys with ENFORCE=False for 1 week
# to collect BFF violations, then flips to True.
ENFORCE = os.getenv("REQUIRE_TENANT_ENFORCE", "false").lower() == "true"

PUBLIC_PATHS = {"/", "/health", "/openapi.json", "/docs", "/redoc"}

def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

async def require_tenant(request: Request, call_next):
    path = request.url.path
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id

    if path in PUBLIC_PATHS or not path.startswith(("/v2/", "/templates", "/connections")):
        return await call_next(request)

    raw_key = request.headers.get("X-API-Key", "")
    claimed_pid = request.headers.get("X-Siyadah-Tenant", "")

    violation: str | None = None
    resolved = None

    if not raw_key:
        violation = "missing_api_key"
    elif not claimed_pid:
        violation = "missing_tenant_header"
    else:
        async with async_session() as s:
            row = (await s.execute(
                select(TenantApiKey).where(
                    TenantApiKey.key_hash == _hash_key(raw_key),
                    TenantApiKey.revoked_at.is_(None),
                )
            )).scalar_one_or_none()
        if not row:
            violation = "unknown_or_revoked_key"
        elif not hmac.compare_digest(row.project_id.encode(), claimed_pid.encode()):
            violation = "tenant_mismatch"
        else:
            resolved = row

    if resolved:
        request.state.project_id = resolved.project_id
        request.state.scopes = resolved.scopes
        request.state.api_key_hash = resolved.key_hash
    else:
        request.state.project_id = None
        request.state.scopes = []
        request.state.api_key_hash = _hash_key(raw_key) if raw_key else None

    if violation and ENFORCE:
        await _audit(request, 401 if violation.startswith("missing") else 403, violation)
        log.warning("auth-block req=%s path=%s violation=%s", request_id, path, violation)
        status = 401 if violation.startswith("missing") else 403
        return JSONResponse(status_code=status, content={
            "error": violation, "request_id": request_id,
        })

    if violation:  # dry-run: log but allow
        log.warning("DRY-RUN auth-violation req=%s path=%s violation=%s",
                    request_id, path, violation)
        await _audit(request, 0, violation)  # http_status=0 marks dry-run log only

    response = await call_next(request)

    if not violation:
        await _audit(request, response.status_code, None)
    return response

async def _audit(request, http_status: int, violation: str | None):
    """Best-effort insert into tenant_audit_log. Never raises."""
    try:
        async with async_session() as s:
            await s.execute(
                "INSERT INTO tenant_audit_log "
                "(project_id, api_key_hash, endpoint, http_status, "
                " request_id, remote_ip, user_agent, violation) "
                "VALUES (:p,:h,:e,:s,:r,:ip,:ua,:v)",
                {
                    "p": getattr(request.state, "project_id", None),
                    "h": getattr(request.state, "api_key_hash", None),
                    "e": f"{request.method} {request.url.path}",
                    "s": http_status,
                    "r": request.state.request_id,
                    "ip": request.client.host if request.client else None,
                    "ua": request.headers.get("user-agent", "")[:500],
                    "v": violation,
                },
            )
            await s.commit()
    except Exception as e:
        log.error("audit-log failed: %s", e)  # never fail the request
```

Wiring in `main.py`:

```python
# Replace the existing api_key_check decorator (main.py:1768-1775) with:
from core.security import require_tenant
app.middleware("http")(require_tenant)
```

### Header contract

| Header | Required? | Value | Notes |
|---|---|---|---|
| `X-API-Key` | yes | raw key issued to the BFF | server hashes with sha256; never stored raw |
| `X-Siyadah-Tenant` | yes | the target `project_id` | must match the key's bound project |
| `X-Request-Id` | optional | client-supplied UUID | echoed back for correlation |

### Env vars (new)

| Var | Default | Effect |
|---|---|---|
| `REQUIRE_TENANT_ENFORCE` | `false` | `true` → block on violation; `false` → dry-run |
| `REQUIRE_TENANT_DRY_RUN_UNTIL` | unset | ISO-8601 date; middleware reads it and warns daily that dry-run window is open |

### Performance budget

- 1 Postgres SELECT per request (indexed on `key_hash`) ≈ 0.5 ms on Railway.
- 1 INSERT into `tenant_audit_log` per request ≈ 0.7 ms. `await` is
  fire-and-forget via `asyncio.create_task()` if the insert becomes a
  hotspot; keep it in the request path for Wave 1 to aid debugging.

انتهيت من §4 (Middleware). هل أكمل بـ §5 (BFF breaking changes)؟
