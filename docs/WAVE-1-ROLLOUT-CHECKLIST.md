# Wave 1 — Rollout Checklist

> **Status:** Orchestrator changes merged to `claude/check-node-env-0Lldj`.
> **Default mode:** `REQUIRE_TENANT_ENFORCE=false` (dry-run).
> **Goal:** flip enforcement to `true` after 24h of zero violations in `tenant_audit_log`.

This file is the step-by-step operator playbook for shipping Wave-1 to
production. It covers: orchestrator env setup, `tenant_api_keys`
seeding, BFF verification, dry-run observation, and the final
enforcement flip.

---

## Step 0 — Before you start

You need access to:

- Railway dashboard for the `siyadah-orchestrator` service (env vars + logs).
- Railway Postgres console for the orchestrator's DB (to run the seed SQL).
- The raw value of `ORCHESTRATOR_API_KEY` currently in prod.
- GitHub write access on `ALKHANFAR/siyadah-orchestrator` (to merge the PR).
- Read access to `ALKHANFAR/Siyadah-6.5` to verify the BFF side (no changes
  are required to the BFF per this repo's audit, but see step 3).

---

## Step 1 — Deploy the orchestrator (dry-run safe)

1.1 Open the PR from `claude/check-node-env-0Lldj` → `main`.

1.2 **Before merging**, set the following in Railway env for the
`siyadah-orchestrator` service:

```
REQUIRE_TENANT_ENFORCE=false        # dry-run. Required.
PG_CA_BUNDLE=<path>                 # optional — prefer this for TLS.
# OR
SIYADAH_SKIP_PG_SSL=1               # explicit opt-in to legacy behaviour.
```

If neither TLS var is set, the service will still start (the deprecated
fallback is preserved for the rollout window) but prints a loud warning
on every boot. **Plan to set `PG_CA_BUNDLE` within 30 days.**

1.3 Merge the PR. Railway auto-deploys. Expected boot logs:

```
... | INFO    | Postgres TLS: verified against /etc/ssl/certs/railway-ca.pem
... | INFO    | Database tables ensured          # creates tenant_api_keys + tenant_audit_log
... | INFO    | Authenticated — project=ou4jOTA4KMnDrzOVsKWvd
... | INFO    | Siyadah Orchestrator v7.1.0 starting
```

1.4 Smoke check: `curl https://siyadah-orchestrator-production.up.railway.app/health`
should still return `{"status":"healthy","activepieces":"connected",...}`.

1.5 Smoke check: send one chat message from `localhost:3000/chat`. The
chat should still work — no tenant header required in dry-run.

---

## Step 2 — Seed `tenant_api_keys`

Open Railway Postgres console for the orchestrator DB. Copy the raw
`ORCHESTRATOR_API_KEY` currently in Railway env (you'll paste it into
the SQL below). Run:

```sql
-- Seed the existing BFF key against the existing default project.
-- AFTER this row exists, the middleware stops falling back to the
-- legacy env-key compare.
INSERT INTO tenant_api_keys (id, project_id, key_hash, label, scopes)
VALUES (
  gen_random_uuid(),
  'ou4jOTA4KMnDrzOVsKWvd',           -- your AP_PROJECT_ID
  encode(sha256('<paste-raw-ORCHESTRATOR_API_KEY-here>'::bytea), 'hex'),
  'siyadah65-bff-prod',
  ARRAY['read', 'write']
);

-- Confirm
SELECT id, project_id, label, created_at, revoked_at
FROM tenant_api_keys
ORDER BY created_at DESC;
```

**Security note:** the raw key is never stored — only its sha256 hash.
Key rotation = insert a new row + set `revoked_at` on the old one.

---

## Step 3 — Verify the BFF already sends `X-Siyadah-Tenant`

**Good news:** the current `src/lib/orchestrator-server.ts` on
`Siyadah-6.5` already injects `X-Siyadah-Tenant` per the
`orchestratorFetch` helper. No code change is required if all server-
side calls go through that helper.

**Audit (run on the Mac):**

```bash
cd ~/Desktop/Siyadah-6.5-main

# A) Every outbound orchestrator URL must go through orchestratorFetch.
#    Any raw fetch() to env.ORCHESTRATOR_URL is a Wave-1 leak.
grep -rn --include='*.ts' --include='*.tsx' \
  "env\.ORCHESTRATOR_URL\|ORCHESTRATOR_URL" src/ \
  | grep -v 'orchestrator-server.ts' \
  | grep -v 'config/env.ts'

# B) Confirm tenantId is pulled from the authenticated session.
grep -rn "orchestratorFetch\s*(" src/ | head -20
```

Expected: (A) returns zero hits (outside the helper + env config).
(B) shows every call passing a `tenantId` derived from `auth()`
session, never hard-coded.

If (A) has hits, migrate those callers to `orchestratorFetch`
before flipping enforcement.

---

## Step 4 — Observe the dry-run (24h minimum)

Query the audit log periodically:

```sql
-- Any violations recorded?
SELECT violation, COUNT(*) AS n,
       MIN(occurred_at) AS first_seen,
       MAX(occurred_at) AS last_seen
FROM tenant_audit_log
WHERE violation IS NOT NULL
GROUP BY violation
ORDER BY n DESC;

-- Who is sending invalid keys?
SELECT remote_ip, user_agent, COUNT(*) AS n
FROM tenant_audit_log
WHERE violation = 'unknown_or_revoked_key'
GROUP BY remote_ip, user_agent
ORDER BY n DESC
LIMIT 20;

-- Which paths are missing the tenant header most?
SELECT endpoint, COUNT(*) AS n
FROM tenant_audit_log
WHERE violation = 'missing_tenant_header'
GROUP BY endpoint
ORDER BY n DESC;
```

**Go / No-go for enforcement:**

| Violation type | Target count (24h) | If exceeded, do this |
|---|---|---|
| `missing_tenant_header` | 0 | BFF caller is bypassing `orchestratorFetch`; migrate it. |
| `tenant_mismatch` | 0 | BFF session.projectId ≠ key's bound project; fix BFF session logic. |
| `unknown_or_revoked_key` | 0 (from BFF IP) | Legacy bootstrap path still being used; seed `tenant_api_keys` row. |
| `missing_api_key` | 0 (from BFF IP) | BFF isn't sending the header; should be impossible via helper. |

---

## Step 5 — Flip enforcement

After 24h+ clean dry-run:

1. In Railway env for `siyadah-orchestrator`: set `REQUIRE_TENANT_ENFORCE=true`.
2. Railway redeploys.
3. Immediately after deploy, send a deliberately bad request to verify:

   ```bash
   curl -i -X POST https://siyadah-orchestrator-production.up.railway.app/v2/templates \
     -H "X-API-Key: $ORCHESTRATOR_API_KEY"
   # Expected: HTTP 401 {"error":"missing_tenant_header","request_id":"..."}
   ```

4. Confirm the chat still works (BFF always sends the header).
5. Tail Railway logs for 1h looking for any `auth-block` entries. Any
   real block = investigate immediately.

---

## Step 6 — Harden Postgres TLS

If you deployed with `SIYADAH_SKIP_PG_SSL=1` in Step 1, replace it:

1. Download the Railway Postgres CA bundle (Railway dashboard → Postgres service → Connect → Certificate).
2. Upload to the orchestrator service (Volume) or mount as env file.
3. Set `PG_CA_BUNDLE=/path/to/railway-ca.pem`.
4. Remove `SIYADAH_SKIP_PG_SSL`.
5. Redeploy. Expected log: `Postgres TLS: verified against /path/to/railway-ca.pem`.

---

## Rollback

If anything goes wrong at any step:

```
Railway env: REQUIRE_TENANT_ENFORCE=false    # instant dry-run again
```

Or revert the PR entirely:

```bash
git revert dab4792 d16de7b
git push
```

Dry-run mode was designed so revert is rarely needed — the audit log
absorbs violations without breaking requests.

---

## What's next (Phase 2)

After Wave-1 is enforced cleanly for 7 days:

- **Phase 2 (Resilience):** `tenacity` retries on `SiyadahEngine._r` +
  `slowapi` rate limiting per tenant (10/min build, 60/min reads).
- See `/root/.claude/plans/rippling-dreaming-knuth.md` for the full
  7-phase plan.
