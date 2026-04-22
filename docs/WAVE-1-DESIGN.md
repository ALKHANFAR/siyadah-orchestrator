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
