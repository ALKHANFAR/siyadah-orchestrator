# Multi-Tenancy Gap Report — siyadah-orchestrator

> Generated: 2026-04-22 · Branch: `claude/setup-siyadah-os-briefing-kboY9`
> Scope: read-only audit of `main.py` (4,110 lines) + `models.py`.
> Every claim below is verified against a specific `file:line` — no extrapolation.

---

## 1. TL;DR

The briefing claims multi-tenancy is fully enforced. The code shows otherwise.

**Reality:**

- ✅ **Schema level**: `projects`, `project_identities`, `knowledge_assets`, `autonomous_settings` are all keyed on `project_id` with a FK cascade (`models.py:21-73`).
- ❌ **Auth level**: no middleware verifies the caller's API key is authorised for the `project_id` they send.
- ❌ **Header level**: no `X-Siyadah-Tenant` header is read. `project_id` is taken from the request JSON body on **14 write endpoints**, then falls back to a global `DEFAULT_PID` env var if missing.
- ❌ **Audit level**: no log entry records which tenant performed which write; a compromised API key allows arbitrary tenant impersonation with no trace.

**Attack surface (concrete):**

Given a valid `ORCHESTRATOR_API_KEY`, a caller can:

- Build a flow inside *any* tenant's project by setting `project_id` in the body.
- Read any tenant's institutional memory via `/v2/project/{project_id}/memory` (no ownership check).
- Import / reconfigure any tenant's existing flow.
- Drain another tenant's AP connections quota.

The BFF (`AGENTS.md:98`) is the only thing preventing this today. If the
orchestrator is ever exposed outside the BFF — or if the BFF is compromised —
multi-tenancy collapses.

---

## 2. Evidence — the 14 leaking write sites

All 14 sites follow the same pattern:

```python
pid = body.project_id or DEFAULT_PID
```

| # | `main.py` line | Endpoint | Severity |
|---|---:|---|---|
| 1 | 1870 | `POST /v2/build-and-deploy` | **Critical** — writes flow into arbitrary project |
| 2 | 1897 | `POST /v2/build-dynamic` | **Critical** |
| 3 | 1967 | `POST /v2/build-router` | **Critical** |
| 4 | 2008 | `POST /v2/build-loop` | **Critical** |
| 5 | 2046 | `POST /v2/build-complex` | **Critical** |
| 6 | 2122 | `POST /v2/build-preset` | **Critical** |
| 7 | 2141 | `POST /v2/build-smart` | **Critical** |
| 8 | 2385 | `POST /v2/flows/{flow_id}/reimport` | **Critical** — overwrites *any* tenant's flow |
| 9 | 2793 | `POST /v2/project/register` | High |
| 10 | 2922 | `POST /v2/identity/ingest` | High — poisons any tenant's DNA |
| 11 | 2960 | `POST /v2/saas/register` | High |
| 12 | 3126 | `POST /v2/logic/suggest` | Medium |
| 13 | 3727 | `POST /v2/mcp/execute` | **Critical** — runs MCP tool in arbitrary project |
| 14 | 4038 | `POST /v2/connect` | **Critical** — attaches connection to arbitrary project |

(`grep -nE "body\.project_id \|\| DEFAULT_PID" main.py` reproduces this list.)

### Read sites with `DEFAULT_PID` fallback (information disclosure)

| `main.py` line | Endpoint | Risk |
|---:|---|---|
| 1814 | `GET /templates` | Lists flows of `DEFAULT_PID` to any authenticated caller |
| 1826 | `GET /connections` | Same |
| 2325-2327 | `GET /v2/client-status` | Lists flows + runs + connections of `DEFAULT_PID` |
| 2707 | `GET /v2/project/{project_id}/hint` | Reads arbitrary tenant's memory |
| 2837 | `GET /v2/project/{project_id}/memory` | Reads arbitrary tenant's memory |
| 3553 | `GET /v2/logic/proactive-suggestions` | Reveals arbitrary tenant's sector-specific intel |
| 4057 | `GET /v2/connections/health` | Lists arbitrary tenant's connections |
| 4074 | `POST /v2/connections/{connection_id}/test` | Tests arbitrary tenant's connection |
| 4095 | `DELETE /v2/connections/{connection_id}` | **Critical** — deletes arbitrary tenant's connection |

---

## 3. Root cause

Three design choices compound:

1. **Trust model is "the BFF is honest."** There is no server-side notion
   of "this API key may only write to project X." One shared API key
   (`ORCHESTRATOR_API_KEY`, `main.py:41`) grants write access to every project.
2. **`DEFAULT_PID` silently masks missing `project_id`.** A malformed
   request never fails with 400; it just writes into the global fallback
   tenant. This makes test data, real data, and impersonation
   indistinguishable.
3. **No `X-Siyadah-Tenant` header contract.** `AGENTS.md:104` implies the
   BFF injects tenant identity, but the orchestrator never reads a header —
   only body fields — so the contract is unenforceable.

---

## 4. Recommended fix (Wave 1 of the master plan)

### 4.1 New table: `tenant_api_keys`

```sql
CREATE TABLE tenant_api_keys (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id     VARCHAR(64) NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  key_hash       CHAR(64) NOT NULL,      -- sha256 of the API key; never store raw
  label          VARCHAR(128),           -- e.g. "siyadah-65-bff-prod"
  scopes         TEXT[] NOT NULL DEFAULT ARRAY['read','write'],
  created_at     TIMESTAMPTZ DEFAULT now(),
  revoked_at     TIMESTAMPTZ
);
CREATE INDEX ix_tak_key_hash ON tenant_api_keys(key_hash);
```

### 4.2 Middleware `require_tenant()`

```python
async def require_tenant(request: Request, call_next):
    if request.url.path.startswith(("/v2/", "/templates", "/connections")):
        key = request.headers.get("X-API-Key", "")
        tenant_hdr = request.headers.get("X-Siyadah-Tenant", "")
        if not key or not tenant_hdr:
            return JSONResponse({"error":"auth_required"}, status_code=401)
        # Look up by hash — constant-time compare inside the query
        row = await tenant_service.resolve(sha256(key), tenant_hdr)
        if not row:
            return JSONResponse({"error":"forbidden"}, status_code=403)
        request.state.project_id = row.project_id
        request.state.scopes = row.scopes
    return await call_next(request)
```

### 4.3 Replace the 14 `pid = body.project_id or DEFAULT_PID` sites

Each becomes:

```python
pid = request.state.project_id        # Never from body.
```

Body-supplied `project_id` is ignored (or rejected with 400 when scopes forbid cross-project reference).

### 4.4 Keep `DEFAULT_PID` only for

- `GET /health`
- `GET /` (landing)
- Local dev script `deploy_sondos_saas.py`

### 4.5 Audit log

Every write goes through a single helper `log_write(tenant_id, endpoint, payload_digest)`
that writes one row to `tenant_audit_log` (Postgres) + one structured log line.

---

## 5. Migration strategy (zero-downtime)

1. Deploy `require_tenant` middleware in **dry-run** mode (logs violation, does not block) for one week.
2. Collect violations → update BFF to send `X-Siyadah-Tenant` on all paths flagged.
3. Flip middleware to enforcing mode.
4. Remove `DEFAULT_PID` fallback from write sites.
5. Deprecate body-supplied `project_id`; reject if `X-Siyadah-Tenant` mismatches.

---

## 6. What this report does NOT fix today

- No code is changed in this commit — this is a read-only audit.
- The corresponding BFF contract update lives in the Siyadah-6.5 repo and is out of scope here.
- `mcp_sse.py` session binding to tenant is a separate ticket (Wave 1 sub-task).

---

## 7. Reproducibility

```bash
# Every line in this report can be re-derived from:
grep -nE "body\.project_id \|\| DEFAULT_PID" main.py      # the 14 write sites
grep -nE "DEFAULT_PID" main.py                             # all 27 references
grep -c "tenant_id" main.py                                # returns 0 — confirms gap
grep -n "X-Siyadah-Tenant" main.py                         # returns nothing
```
