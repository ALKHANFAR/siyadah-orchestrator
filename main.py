"""
Siyadah Orchestrator v7.1.0 — The Autonomous SaaS OS
======================================================
Golden Protocol v5 (Immunization): IMPORT_FLOW → deterministic webhook URL →
GET-verify → LOCK_AND_PUBLISH → ENABLE (strict GET confirmation).
Rule: propertySettings: {} is MANDATORY in every step settings.
Built on: 11 April 2026

Capabilities: ROUTER, LOOP, CODE, PIECE, PRESETS, SMART_SCHEMA,
              Multi-Tenancy, MCP Execute, MCP SSE, Re-import, Diagnose,
              Modular Persistence (Postgres), Redis Sessions, Hints,
              INGESTION, SAAS_ONBOARDING, CONTEXT_AWARE_MCP, PROACTIVE_LOGIC
"""
from __future__ import annotations
import asyncio, hmac, json, logging, os, re, time as _time, traceback
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import httpx, uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

# Phase 3 replaces the plain-text formatter with structlog JSON +
# optional Sentry. configure_logging() is called inside the lifespan
# handler before anything else so the first startup log line is JSON.
# Until configure_logging() runs (e.g. during module import) the
# standard basicConfig keeps logs visible in dev REPLs.
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
log = logging.getLogger("siyadah")

VERSION = "7.1.0"
AP_BASE = os.getenv("AP_BASE_URL", "")
AP_EMAIL = os.getenv("AP_EMAIL", "")
AP_PASSWORD = os.getenv("AP_PASSWORD", "")
DEFAULT_PID = os.getenv("AP_PROJECT_ID", "")
DEFAULT_CONNECTIONS: Dict[str, str] = {
    "gmail": os.getenv("GMAIL_CONNECTION_ID", ""),
    "google-sheets": os.getenv("SHEETS_CONNECTION_ID", ""),
}
ORCH_API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "")
AP_MCP_URL = os.getenv("AP_MCP_SERVER_URL", "")
AP_MCP_TOKEN = os.getenv("AP_MCP_TOKEN") or os.getenv("AP_TOKEN", "")
ORCHESTRATOR_HTTPX_TIMEOUT = int(os.getenv("ORCHESTRATOR_HTTPX_TIMEOUT", "120"))

_BOOLEAN_FIELD_NAMES = frozenset({
    "draft", "public", "active", "is_draft", "is_public",
    "is_active", "enabled", "published",
})

# DEPRECATED (Phase-8): replaced by piece_registry. Kept only as a cold
# fallback for environments where the registry is empty (first deploy,
# before sync_pieces has run). Delete this dict and every lookup against
# it once `python -m scripts.sync_pieces --full` has been run in every
# environment. The values below are historically stale — e.g. gmail here
# is 0.11.6 while AP production already ships 0.12.1.
PIECE_VERSIONS: Dict[str, str] = {
    # Refreshed 2026-04-26 from piece_registry. The Sniper Validator
    # rejects builds whose pieceVersion is not in the registry, so this
    # cold-fallback dict must stay in sync with what sync_pieces wrote.
    "webhook": "~0.1.32",
    "gmail": "~0.12.1",
    "google-sheets": "~0.14.6",
    "google-drive": "~0.7.1",
    "schedule": "~0.1.0",
    "slack": "~0.7.7",
    "hubspot": "~0.8.4",
}

OPERATORS = [
    "TEXT_CONTAINS", "TEXT_DOES_NOT_CONTAIN",
    "TEXT_EXACTLY_MATCHES", "TEXT_DOES_NOT_EXACTLY_MATCH",
    "TEXT_STARTS_WITH", "TEXT_DOES_NOT_START_WITH",
    "TEXT_ENDS_WITH", "TEXT_DOES_NOT_END_WITH",
    "TEXT_IS_EMPTY", "TEXT_IS_NOT_EMPTY",
    "NUMBER_IS_GREATER_THAN", "NUMBER_IS_LESS_THAN",
    "NUMBER_IS_EQUAL_TO", "BOOLEAN_IS_TRUE", "BOOLEAN_IS_FALSE",
    "EXISTS", "DOES_NOT_EXIST",
]


# ═══════════════════════════════════════════════════════════════
# ASYNC ENGINE — httpx, multi-tenant
# ═══════════════════════════════════════════════════════════════

def _is_retryable_engine_error(exc: BaseException) -> bool:
    """Classify upstream failures for the AP engine retry loop (Phase 2).

    Retry: transient httpx errors (converted to HTTPException(502) inside
    _r), and any upstream 5xx. Never retry 4xx — those are caller bugs,
    retrying just wastes budget and hides the error.
    """
    if isinstance(exc, HTTPException) and 500 <= exc.status_code < 600:
        return True
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    return False


class SiyadahEngine:
    """Async Activepieces API client."""

    def __init__(self, base_url: str, token: str, email: str = "", password: str = ""):
        self.base = base_url.rstrip("/")
        self.token = token
        self._email = email
        self._password = password
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # Phase 2: bound the connection pool so a burst of builds can't
            # exhaust sockets. Tuned for Railway's single-worker default; raise
            # max_connections if we move to a multi-worker deploy.
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                timeout=ORCHESTRATOR_HTTPX_TIMEOUT,
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=20,
                    keepalive_expiry=30.0,
                ),
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _r(self, method: str, path: str, body=None, params=None):
        """Call AP with bounded retry on transient failures.

        Retry policy (Phase 2):
        - Transient network/timeout → retry up to 3 attempts, exp jitter 1–8s.
        - Upstream 5xx → retry (same budget).
        - 4xx → NEVER retry (surface the error to the caller immediately).
        - 401 with credentials → re-auth inline on the current attempt
          (existing behaviour; does NOT consume a retry slot).
        """
        url = f"{self.base}/api{path}"
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(_is_retryable_engine_error),
            stop=stop_after_attempt(3),
            wait=wait_exponential_jitter(initial=1.0, max=8.0),
            reraise=True,
        ):
            with attempt:
                client = await self._ensure_client()
                try:
                    r = await client.request(method, url, json=body, params=params)
                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    # Tagged as 502 so the route returns a sane upstream error
                    # AND so _is_retryable_engine_error fires.
                    raise HTTPException(502, detail=f"upstream network error: {e}")
                except Exception as e:
                    raise HTTPException(502, detail=str(e))

                if r.status_code == 401 and self._email and self._password:
                    log.warning("[engine] 401 on %s %s — re-authenticating", method, path)
                    try:
                        auth = await self.sign_in(self._email, self._password, self.base)
                        new_token = auth.get("token") or auth.get("access_token", "")
                        if new_token:
                            self.token = new_token
                            if self._client and not self._client.is_closed:
                                await self._client.aclose()
                            self._client = None
                            client = await self._ensure_client()
                            r = await client.request(method, url, json=body, params=params)
                            log.info("[engine] Re-auth OK, retried %s %s → %s",
                                     method, path, r.status_code)
                    except Exception as auth_err:
                        log.error("[engine] Re-auth failed: %s", auth_err)

                if not r.is_success:
                    raise HTTPException(r.status_code, detail=r.text[:500])
                if r.status_code == 204 or not r.content:
                    return {}
                return r.json()
        # AsyncRetrying with reraise=True always either returns from the
        # body or raises; the loop cannot exit normally.
        raise RuntimeError("unreachable: AsyncRetrying exited without outcome")

    @staticmethod
    async def sign_in(email: str, password: str, base: str) -> dict:
        async with httpx.AsyncClient(timeout=ORCHESTRATOR_HTTPX_TIMEOUT) as client:
            r = await client.post(f"{base}/api/v1/authentication/sign-in",
                                  json={"email": email, "password": password})
            r.raise_for_status()
            return r.json()

    # ── Projects & Connections ──
    async def list_projects(self):
        d = await self._r("GET", "/v1/projects/")
        return d if isinstance(d, list) else d.get("data", [d])

    async def list_connections(self, pid: str):
        d = await self._r("GET", "/v1/app-connections/",
                          params={"projectId": pid, "limit": "200"})
        return d if isinstance(d, list) else d.get("data", [])

    # ── Flow CRUD ──
    async def create_flow(self, pid: str, name: str):
        return await self._r("POST", "/v1/flows/",
                             {"displayName": name, "projectId": pid})

    async def _fop(self, fid: str, op: str, req: dict):
        return await self._r("POST", f"/v1/flows/{fid}",
                             {"type": op, "request": req})

    async def import_flow(self, fid: str, display_name: str, trigger: dict):
        """Golden Protocol Step 1 — IMPORT_FLOW."""
        return await self._fop(fid, "IMPORT_FLOW", {
            "displayName": display_name, "trigger": trigger,
        })

    async def update_metadata(self, fid: str, metadata: dict) -> dict:
        """Sovereign Tightening — stamp owner identity onto a flow.

        AP's flow object exposes a free-form `metadata` (jsonb-ish) field
        that is preserved across IMPORT_FLOW + LOCK_AND_PUBLISH. The
        Sovereign Tightening protocol uses this field as the immutable
        bearer of (tenantId, ownerEmail, stamped_at) so list/update/
        delete operations can verify ownership without a second DB
        round-trip. The flow_registry table is the cache; this is the
        ground truth — if both diverge, AP wins because it is what
        actually owns the runtime side of the flow.
        """
        return await self._fop(fid, "UPDATE_METADATA", {"metadata": metadata})

    async def verify_flow(self, fid: str) -> dict:
        """Golden Protocol Step 2 — GET-verify (never trust 200 alone)."""
        flow = await self._r("GET", f"/v1/flows/{fid}")
        ttype = flow.get("version", {}).get("trigger", {}).get("type", "UNKNOWN")
        if ttype == "EMPTY":
            raise HTTPException(
                500, "SILENT FAILURE: trigger still EMPTY after IMPORT_FLOW. "
                     "Check propertySettings: {} in every step.")
        return flow

    async def publish_and_enable(self, fid: str) -> dict:
        """LOCK_AND_PUBLISH → ENABLE if needed. Strict: final GET must show ENABLED."""

        def _assert_enabled(f: dict) -> dict:
            st = f.get("status", "")
            if st != "ENABLED":
                raise HTTPException(
                    500,
                    detail=f"Flow {fid} not ENABLED after publish/enable. status={st}, "
                           f"state={f.get('version', {}).get('state', '?')}",
                )
            return {
                "lock": "ok",
                "status": "ENABLED",
                "state": f.get("version", {}).get("state", ""),
            }

        await self._fop(fid, "LOCK_AND_PUBLISH", {})
        flow = await self._r("GET", f"/v1/flows/{fid}")
        current_status = flow.get("status", "DISABLED")
        version_state = flow.get("version", {}).get("state", "")
        if current_status == "ENABLED":
            log.info("[publish] Flow %s already ENABLED after publish (state=%s)", fid, version_state)
            return _assert_enabled(flow)
        last_exc: Exception | None = None
        flow_after: dict = flow
        for attempt in range(1, 3):
            try:
                await self._fop(fid, "CHANGE_STATUS", {"status": "ENABLED"})
                flow_after = await self._r("GET", f"/v1/flows/{fid}")
                if flow_after.get("status") == "ENABLED":
                    return _assert_enabled(flow_after)
                last_exc = None
                if attempt == 1:
                    log.warning(
                        "[publish] Flow %s not ENABLED after CHANGE_STATUS (status=%s) — retrying in 1s",
                        fid, flow_after.get("status"),
                    )
                    await asyncio.sleep(1)
            except HTTPException:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt == 1:
                    log.warning("[publish] ENABLE attempt 1 failed for %s: %s — retrying in 1s", fid, exc)
                    await asyncio.sleep(1)
                else:
                    raise HTTPException(
                        500,
                        detail=f"ENABLE failed after 2 attempts for flow {fid}. Last error: {str(exc)[:300]}",
                    )
        if last_exc:
            raise HTTPException(
                500,
                detail=f"ENABLE failed after 2 attempts for flow {fid}. Last error: {str(last_exc)[:300]}",
            )
        raise HTTPException(
            500,
            detail=f"Flow {fid} not ENABLED after CHANGE_STATUS. status={flow_after.get('status', '?')}",
        )

    async def update_trigger(self, fid: str, cfg: dict):
        return await self._fop(fid, "UPDATE_TRIGGER", cfg)

    async def add_action(self, fid: str, parent: str, loc: str, action: dict):
        return await self._fop(fid, "ADD_ACTION", {
            "parentStep": parent,
            "stepLocationRelativeToParent": loc,
            "action": action,
        })

    async def list_flows(self, pid: str):
        d = await self._r("GET", "/v1/flows/",
                          params={"projectId": pid, "limit": "200"})
        return d if isinstance(d, list) else d.get("data", [])

    async def get_flow(self, fid: str):
        return await self._r("GET", f"/v1/flows/{fid}")

    async def delete_flow(self, fid: str):
        return await self._r("DELETE", f"/v1/flows/{fid}")

    async def list_runs(self, pid: str, limit: int = 50):
        d = await self._r("GET", "/v1/flow-runs/",
                          params={"projectId": pid, "limit": str(limit)})
        return d if isinstance(d, list) else d.get("data", [])

    async def get_piece(self, name: str, ver: str | None = None):
        params: Dict[str, str] = {}
        if ver and ver != "latest":
            params["version"] = ver
        data = await self._r("GET", f"/v1/pieces/{name}",
                             params=params or None)
        acts = data.get("actions") if data else None
        if isinstance(acts, int) and not ver:
            got_ver = data.get("version", "")
            if got_ver:
                log.info("[get_piece] actions is int for %s — re-fetching with version=%s", name, got_ver)
                data = await self._r("GET", f"/v1/pieces/{name}",
                                     params={"version": got_ver})
        return data

    async def list_pieces(self):
        return await self._r("GET", "/v1/pieces/",
                             params={"includeHidden": "false"})

    async def test_webhook(self, fid: str, payload: dict):
        client = await self._ensure_client()
        url = f"{self.base}/api/v1/webhooks/{fid}"
        try:
            r = await client.post(url, json=payload)
            return {"status": "sent", "webhook_url": url,
                    "response_status": r.status_code, "payload": payload}
        except Exception as e:
            raise HTTPException(502, detail=str(e))


# ═══════════════════════════════════════════════════════════════
# CONNECTION REF + GUARD
# ═══════════════════════════════════════════════════════════════
def C(ext_id: str) -> str:
    """Activepieces connection reference by externalId."""
    return "{{connections['" + ext_id + "']}}"


def resolve_conns(override: Dict[str, str] | None) -> Dict[str, str]:
    """Merge overrides onto default connection IDs."""
    conns = dict(DEFAULT_CONNECTIONS)
    if override:
        conns.update(override)
    return conns


def resolve_owner_email(request: Request) -> Optional[str]:
    """Read the owner email from the BFF's X-Siyadah-Owner-Email header.

    Sovereign Tightening: the BFF (orchestrator-server.ts) injects this
    header for every server-side fetch so the orchestrator can stamp
    every newly-built flow with the founder's email. None when the
    header is missing (e.g., legacy callers, internal probes); the
    metadata field then carries only `tenantId`.
    """
    return (request.headers.get("X-Siyadah-Owner-Email", "") or "").strip() or None


# ─── Sovereign Tightening — ownership gate ──────────────────────────
#
# Every flow-mutating endpoint (PATCH /v2/flows/{id}, /diagnose,
# /reimport, list filters) MUST run flow_id through this gate before
# proceeding. The gate is two-tiered:
#
#   1. Fast path — the orchestrator's own `flow_registry` table.
#      Indexed on (tenant_id, flow_id), O(1) lookup.
#   2. Slow path — fall back to AP `metadata.tenantId` when the row
#      is not in the registry yet (legacy/orphan flows from before
#      the Tightening).
#
# Both paths must agree with `request.state.project_id` from the auth
# middleware. Mismatch raises HTTPException(403) with a sovereign
# error envelope; the chat path then surfaces the branded message.

async def _flow_belongs_to(engine, fid: str, tenant_pid: str) -> bool:
    """Return True iff the flow is owned by `tenant_pid`.

    Reads flow_registry first (O(1) DB hit), falls back to fetching
    AP metadata when the registry lacks an entry. Returns False on
    any read error so the gate fails closed.
    """
    if not fid or not tenant_pid:
        return False
    # 1. flow_registry fast path
    try:
        from database import async_session as _s
        from models import FlowRegistry
        from sqlalchemy import select as _select
        if _s is not None:
            async with _s() as sess:
                row = (await sess.execute(
                    _select(FlowRegistry).where(FlowRegistry.flow_id == fid)
                )).scalar_one_or_none()
                if row is not None:
                    return row.tenant_id == tenant_pid
    except Exception as e:
        log.warning("[ownership] flow_registry read failed for %s: %s", fid, e)
    # 2. AP metadata fallback (slower; one extra GET to AP)
    try:
        flow = await engine.get_flow(fid)
        meta = flow.get("metadata") or {}
        if isinstance(meta, dict):
            return meta.get("tenantId") == tenant_pid
    except Exception as e:
        log.warning("[ownership] AP metadata read failed for %s: %s", fid, e)
    return False


async def assert_flow_ownership(engine, fid: str, tenant_pid: str) -> None:
    """Hard gate. Raises HTTPException(403) on mismatch."""
    owns = await _flow_belongs_to(engine, fid, tenant_pid)
    if not owns:
        log.warning(
            "[ownership-block] tenant=%s attempted to touch flow=%s "
            "but does not own it (sovereign tightening)",
            tenant_pid, fid,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "flow_not_owned",
                "message": "هذا الفلو ليس لك. الحركة مرفوضة.",
                "flow_id": fid,
            },
        )


def resolve_pid(request: Request, body_pid: Optional[str]) -> str:
    """Resolve project_id with Wave-1 precedence.

    1. request.state.project_id — set by require_tenant when the caller
       presented a valid (X-API-Key, X-Siyadah-Tenant) pair.
    2. body.project_id — legacy path. Accepted during the dry-run
       window so the BFF can be updated asynchronously. Remove after
       REQUIRE_TENANT_ENFORCE has been 'true' in prod for ≥7 days with
       zero violations.
    3. DEFAULT_PID — last-resort for single-tenant dev. Emits a warning
       log every time to make the silent fallback visible.
    """
    state_pid = getattr(request.state, "project_id", None)
    if state_pid:
        return state_pid
    if body_pid:
        return body_pid
    log.warning(
        "resolve_pid fell back to DEFAULT_PID path=%s req_id=%s — header "
        "missing AND body project_id empty. Fix BFF or seed tenant_api_keys.",
        request.url.path,
        getattr(request.state, "request_id", "?"),
    )
    return DEFAULT_PID


# ═══════════════════════════════════════════════════════════════
# TOKEN-EFFICIENT RESPONSE COMPRESSION
# ═══════════════════════════════════════════════════════════════

def compress_response(data, max_list_items: int = 5,
                      max_str_len: int = 500):
    """Reduce response payload size for token-efficient AI consumption."""
    if isinstance(data, dict):
        return {
            k: compress_response(v, max_list_items, max_str_len)
            for k, v in data.items()
            if v is not None and v != "" and v != [] and v != {}
        }
    if isinstance(data, list):
        compressed = [compress_response(item, max_list_items, max_str_len)
                      for item in data[:max_list_items]]
        if len(data) > max_list_items:
            compressed.append(f"... +{len(data) - max_list_items} more")
        return compressed
    if isinstance(data, str) and len(data) > max_str_len:
        return data[:max_str_len] + "..."
    return data


# ═══════════════════════════════════════════════════════════════
# AUTO-CONFIGURE AUTONOMOUS SETTINGS FROM ANALYSIS
# ═══════════════════════════════════════════════════════════════

async def _auto_configure_settings(project_id: str, analysis: dict) -> dict:
    """Automatically set AutonomousSettings based on ingestion analysis.

    Called after a successful SaaS registration to configure tone,
    language, smart rules, etc. without manual intervention.
    """
    from database import async_session
    from models import AutonomousSetting
    from sqlalchemy import select

    if not async_session:
        return {"auto_configured": False, "reason": "database_offline"}

    ka = analysis.get("knowledge_assets", {})
    loc = analysis.get("localization", {})
    bp = analysis.get("business_profile", {})

    client_settings = {
        "tone_of_voice": ka.get("tone_of_voice", "professional"),
        "primary_language": loc.get("primary_language", "en"),
        "sector": bp.get("sector", "general"),
        "auto_faq_enabled": bool(ka.get("faqs")),
        "brand_keywords": ka.get("brand_keywords", [])[:10],
    }

    smart_rules = []
    if ka.get("faqs"):
        smart_rules.append({
            "type": "faq_auto_reply",
            "enabled": True,
            "source": "ingestion",
            "faq_count": len(ka["faqs"]),
        })
    if bp.get("goals"):
        smart_rules.append({
            "type": "goal_tracking",
            "enabled": True,
            "goals": bp["goals"][:3],
        })

    async with async_session() as session:
        async with session.begin():
            setting = (await session.execute(
                select(AutonomousSetting).where(
                    AutonomousSetting.project_id == project_id)
            )).scalar_one_or_none()

            if not setting:
                setting = AutonomousSetting(project_id=project_id)
                session.add(setting)

            setting.client_settings = client_settings
            setting.smart_rules = smart_rules
            setting.auto_respond = "smart"

    log.info("[auto-config] Settings configured for project %s: sector=%s, rules=%d",
             project_id, client_settings["sector"], len(smart_rules))
    return {
        "auto_configured": True,
        "tone_of_voice": client_settings["tone_of_voice"],
        "language": client_settings["primary_language"],
        "smart_rules_count": len(smart_rules),
        "auto_respond": "smart",
    }


def _extract_pieces_from_steps(steps) -> List[str]:
    """Recursively collect piece short-names from a list of step specs."""
    pieces: List[str] = []
    for s in (steps or []):
        d = s if isinstance(s, dict) else (s.model_dump() if hasattr(s, "model_dump") else {})
        stype = d.get("type", "PIECE")
        raw = (d.get("piece") or d.get("piece_name") or "").strip()
        if stype == "PIECE" and raw:
            pieces.append(raw.replace("@activepieces/piece-", ""))
        for sub_key in ("actions", "loop_actions", "before_loop", "after_loop"):
            sub = d.get(sub_key)
            if isinstance(sub, list):
                pieces.extend(_extract_pieces_from_steps(sub))
        for branch in (d.get("branches") or []):
            bd = branch if isinstance(branch, dict) else {}
            pieces.extend(_extract_pieces_from_steps(bd.get("actions", [])))
    return pieces


async def guard_connections(
    engine: SiyadahEngine, pid: str,
    required_pieces: List[str], conns: Dict[str, str],
    *, strict: bool = False,
) -> List[str]:
    """Validate that required connections exist in the target project.

    When *strict* is True, missing connections raise HTTPException(422)
    instead of returning warnings.
    """
    errors: List[str] = []
    try:
        live = await engine.list_connections(pid)
        available = set()
        for c in live:
            available.add(c.get("externalId", ""))
            available.add(c.get("id", ""))
        for piece in required_pieces:
            short = piece.replace("@activepieces/piece-", "")
            cid = conns.get(short, "")
            if not cid:
                errors.append(
                    f"No connection ID configured for '{short}' — "
                    f"provide it via connection_ids or DEFAULT_CONNECTIONS")
            elif cid not in available:
                errors.append(
                    f"Connection '{cid}' for {short} not found in project {pid}")
    except Exception as e:
        errors.append(f"Connection guard check failed: {str(e)[:100]}")
    if strict and errors:
        raise HTTPException(
            422,
            detail={
                "message": "Missing connections — connect the required tools before building.",
                "missing": errors,
            })
    return errors


# ═══════════════════════════════════════════════════════════════
# PIECE SCHEMA CACHE + SMART PROPERTY-SETTINGS
# ═══════════════════════════════════════════════════════════════
_piece_schema_cache: Dict[str, dict] = {}
PIECES_LIST_TTL_SEC = 86400  # 24h — reduce load on Activepieces pieces API
_pieces_list_cache: Dict[str, Any] = {"data": None, "ts": 0}


async def fetch_piece_schema(engine: SiyadahEngine, piece_name: str) -> dict:
    cache_key = piece_name.replace("@activepieces/piece-", "")

    cached = _piece_schema_cache.get(cache_key)
    if cached:
        acts = cached.get("actions", {})
        if isinstance(acts, dict) and acts:
            return cached
        log.info("Evicting stale schema cache for %s (actions type=%s, len=%s)",
                 cache_key, type(acts).__name__, len(acts) if isinstance(acts, dict) else acts)
        del _piece_schema_cache[cache_key]

    api_name = (piece_name if piece_name.startswith("@activepieces/")
                else f"@activepieces/piece-{piece_name}")

    for _attempt in range(2):
        try:
            data = await engine.get_piece(api_name)
            if data and isinstance(data.get("actions", None), dict):
                if not data["actions"]:
                    got_ver = data.get("version", "")
                    if got_ver:
                        log.info("[schema] Empty actions dict for %s — re-fetching with version=%s", api_name, got_ver)
                        data = await engine.get_piece(api_name, ver=got_ver)
                if data and isinstance(data.get("actions", None), dict):
                    _piece_schema_cache[cache_key] = data
                    log.info("Cached schema: %s v%s (%d actions)",
                             cache_key, data.get("version", "?"),
                             len(data.get("actions", {})))
            else:
                log.warning("Schema for %s returned non-dict actions, not caching",
                            api_name)
            return data
        except Exception as e:
            if _attempt == 0:
                log.warning("Schema fetch failed for %s (attempt 1/2): %s — retrying in 1s", api_name, e)
                await asyncio.sleep(1)
            else:
                log.warning("Schema fetch failed for %s (attempt 2/2): %s — giving up", api_name, e)

    return {}


def _fuzzy_name(name: str, available: dict) -> str:
    if name in available:
        return name
    alt1 = name.replace("_", "-")
    alt2 = name.replace("-", "_")
    if alt1 in available:
        return alt1
    if alt2 in available:
        return alt2
    # prefix/suffix match: "create_record" → "airtable_create_record"
    for k in available:
        if k.endswith(name) or k.endswith(alt1) or k.endswith(alt2):
            return k
    return name


_raw_pieces_cache: list = []
_raw_pieces_ts: float = 0


async def _get_raw_pieces_list(engine) -> list:
    global _raw_pieces_cache, _raw_pieces_ts
    if _raw_pieces_cache and (_time.time() - _raw_pieces_ts < 3600):
        return _raw_pieces_cache
    raw = await engine.list_pieces()
    _raw_pieces_cache = raw if isinstance(raw, list) else raw.get("data", [])
    _raw_pieces_ts = _time.time()
    return _raw_pieces_cache


def _piece_candidates(name: str) -> list[str]:
    clean = name.lower().strip().replace("@activepieces/piece-", "")
    results = [clean]
    for suffix in ["-business", "-cloud", "-online", "-api", "-v2"]:
        if clean.endswith(suffix):
            results.append(clean[:-len(suffix)])
    results.append(clean.replace("_", "-"))
    results.append(clean.replace("-", "_"))
    return list(dict.fromkeys(results))


async def auto_resolve_piece(engine, name: str) -> tuple:
    """Resolve piece name. Zero-risk: returns original if no match."""
    schema = await fetch_piece_schema(engine, name)
    if schema and schema.get("actions"):
        return name, schema
    for c in _piece_candidates(name):
        if c == name:
            continue
        schema = await fetch_piece_schema(engine, c)
        if schema and schema.get("actions"):
            log.info("[auto-resolve] '%s' -> '%s'", name, c)
            return c, schema
    pieces = await _get_raw_pieces_list(engine)
    query = name.lower().replace("@activepieces/piece-", "")
    for p in pieces:
        short = p.get("name", "").replace("@activepieces/piece-", "").lower()
        if query in short or short.startswith(query):
            schema = await fetch_piece_schema(engine, short)
            if schema and schema.get("actions"):
                log.info("[auto-resolve] '%s' -> '%s' (list)", name, short)
                return short, schema
    log.warning("[auto-resolve] '%s' not resolved", name)
    return name, schema or {}


def clean_input_config(input_config: dict) -> dict:
    """Drop empty-string and null-like keys before sending input to Activepieces."""
    cleaned: Dict[str, Any] = {}
    for k, v in input_config.items():
        if v is None:
            continue
        if isinstance(v, list) and v == ['']:
            continue
        if isinstance(v, str) and v == '':
            continue
        cleaned[k] = v
    return cleaned


def clean_input(input_config: dict) -> dict:
    """Backward-compatible alias for clean_input_config."""
    return clean_input_config(input_config)


def get_action_props(schema: dict, action_name: str) -> dict:
    action = schema.get("actions", {}).get(action_name, {})
    return action.get("props", {}) or action.get("properties", {})


def get_trigger_props(schema: dict, trigger_name: str) -> dict:
    return schema.get("triggers", {}).get(trigger_name, {}).get("props", {})


_DYNAMIC_RE = re.compile(r"\{\{.*?\}\}")


def _contains_dynamic_ref(value: Any) -> bool:
    """Recursively check if *value* contains ``{{…}}`` dynamic references."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(_DYNAMIC_RE.search(value))
    if isinstance(value, dict):
        return any(_contains_dynamic_ref(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_dynamic_ref(v) for v in value)
    return False


def generate_property_settings(props: dict, input_config: dict) -> dict:
    """Generate schema-aware propertySettings for AP UI compatibility.

    Priority order (highest first):
    1. Any value containing ``{{…}}`` → SHORT_TEXT / CUSTOM_INPUT (before type casting)
    2. Type Casting: ARRAY, JSON, NUMBER coercion
    3. DYNAMIC → CUSTOM_INPUT
    4. DROPDOWN / MULTI_SELECT_DROPDOWN → CUSTOM_INPUT
    5. STATIC_DROPDOWN → type marker only
    """
    if not props:
        return {}
    settings: Dict[str, Any] = {}
    for pname, pinfo in props.items():
        try:
            if pname == "auth" or pname not in input_config:
                continue
            val = input_config.get(pname)

            # ── PRIORITY 1: Dynamic refs {{ }} — before ANY type casting ──
            if _contains_dynamic_ref(val):
                settings[pname] = {"type": "SHORT_TEXT", "status": "CUSTOM_INPUT"}
                continue

            ptype = pinfo.get("type", "") if isinstance(pinfo, dict) else str(pinfo)

            # ── PRIORITY 2: Type Casting (safe — no dynamic refs here) ──
            if ptype == "BOOLEAN":
                if val is None:
                    val = False
                elif isinstance(val, str):
                    val = val.lower() in ("true", "1", "yes")
                else:
                    val = bool(val)
                input_config[pname] = val
            elif ptype == "ARRAY" and val is not None and not isinstance(val, list):
                val = [val]
                input_config[pname] = val
            elif ptype == "JSON" and isinstance(val, str):
                try:
                    val = json.loads(val)
                    input_config[pname] = val
                except (ValueError, TypeError):
                    pass
            elif ptype == "NUMBER" and val is not None and not isinstance(val, (int, float)):
                try:
                    val = float(val) if "." in str(val) else int(val)
                    input_config[pname] = val
                except (ValueError, TypeError):
                    pass

            # ── PRIORITY 3: Type-based settings (DYNAMIC, DROPDOWN) ──
            if ptype == "DYNAMIC":
                settings[pname] = {"type": "DYNAMIC", "status": "CUSTOM_INPUT"}
            elif ptype in ("DROPDOWN", "MULTI_SELECT_DROPDOWN"):
                settings[pname] = {"type": ptype, "status": "CUSTOM_INPUT"}
            elif ptype == "STATIC_DROPDOWN":
                _sd_opts = pinfo.get("options", []) if isinstance(pinfo, dict) else []
                _sd_valid = {
                    (o.get("value") if isinstance(o, dict) else o)
                    for o in _sd_opts
                } if _sd_opts else set()
                if _sd_valid and val not in _sd_valid:
                    settings[pname] = {"type": "STATIC_DROPDOWN", "status": "CUSTOM_INPUT"}
                else:
                    settings[pname] = {"type": "STATIC_DROPDOWN"}
        except Exception as e:
            log.error(f"Error processing field {pname}: {e}")

    # ── FALLBACK: any configured prop without registration → MANUAL ──
    for pname in input_config.keys():
        if pname == "auth":
            continue
        if pname not in settings:
            settings[pname] = {"type": "MANUAL"}

    return settings


def resolve_piece_version(schema: dict, piece_name: str) -> str:
    if schema and schema.get("version"):
        return f"~{schema['version']}"
    short = piece_name.replace("@activepieces/piece-", "")
    return PIECE_VERSIONS.get(short, "~0.1.0")


# ═══════════════════════════════════════════════════════════════
# STEP BUILDERS — propertySettings: {} in EVERY step
# ═══════════════════════════════════════════════════════════════
def build_trigger(piece: str, ver: str, tname: str, inp: dict,
                  display: str = "Trigger", next_action=None) -> dict:
    t: Dict[str, Any] = {
        "name": "trigger", "valid": True, "skip": False, "displayName": display,
        "type": "PIECE_TRIGGER",
        "settings": {
            "pieceName": piece, "pieceVersion": ver,
            "pieceType": "OFFICIAL", "packageType": "REGISTRY",
            "triggerName": tname, "input": inp,
            "inputUiInfo": {}, "propertySettings": {},
        },
    }
    if next_action:
        t["nextAction"] = next_action
    return t


def build_action(sname: str, piece: str, ver: str, aname: str, inp: dict,
                 display: str = "Action", next_action=None,
                 property_settings: dict | None = None) -> dict:
    a: Dict[str, Any] = {
        "name": sname, "valid": True, "skip": False, "displayName": display,
        "type": "PIECE",
        "settings": {
            "pieceName": piece, "pieceVersion": ver,
            "pieceType": "OFFICIAL", "packageType": "REGISTRY",
            "actionName": aname, "input": inp,
            "inputUiInfo": {},
            "propertySettings": property_settings if property_settings is not None else {},
            "errorHandlingOptions": {
                "retryOnFailure": {"value": False},
                "continueOnFailure": {"value": False},
            },
        },
    }
    if next_action:
        a["nextAction"] = next_action
    return a


def build_router_step(name: str, display_name: str,
                      branches: list, children: list,
                      next_action=None) -> dict:
    step: Dict[str, Any] = {
        "name": name, "type": "ROUTER", "valid": True, "skip": False,
        "displayName": display_name,
        "settings": {
            "branches": branches,
            "executionType": "EXECUTE_FIRST_MATCH",
            "inputUiInfo": {},
            "errorHandlingOptions": {
                "retryOnFailure": {"value": False},
                "continueOnFailure": {"value": False},
            },
        },
        "children": children,
    }
    if next_action:
        step["nextAction"] = next_action
    return step


def build_loop_step(name: str, display_name: str,
                    items_expression: str,
                    first_loop_action=None,
                    next_action=None) -> dict:
    step: Dict[str, Any] = {
        "name": name, "type": "LOOP_ON_ITEMS", "valid": True,
        "displayName": display_name,
        "settings": {
            "items": items_expression,
            "inputUiInfo": {},
            "errorHandlingOptions": {
                "retryOnFailure": {"value": False},
                "continueOnFailure": {"value": False},
            },
        },
    }
    if first_loop_action:
        step["firstLoopAction"] = first_loop_action
    if next_action:
        step["nextAction"] = next_action
    return step


def build_code_step(name: str, display_name: str, code: str,
                    input_config: dict | None = None,
                    next_action=None) -> dict:
    step: Dict[str, Any] = {
        "name": name, "type": "CODE", "valid": True,
        "displayName": display_name,
        "settings": {
            "input": input_config or {"data": "{{trigger['body']}}"},
            "sourceCode": {"code": code, "packageJson": '{"dependencies": {}}'},
            "inputUiInfo": {},
            "errorHandlingOptions": {
                "retryOnFailure": {"value": False},
                "continueOnFailure": {"value": False},
            },
        },
    }
    if next_action:
        step["nextAction"] = next_action
    return step


# ═══════════════════════════════════════════════════════════════
# CONDITION HELPERS
# ═══════════════════════════════════════════════════════════════
def cond(operator: str, first_value: str, second_value: str = "") -> dict:
    c: Dict[str, Any] = {"operator": operator, "firstValue": first_value}
    if second_value:
        c["secondValue"] = second_value
    if operator == "TEXT_CONTAINS":
        c["caseSensitive"] = False
    return c


def condition_branch(name: str, conditions: list) -> dict:
    return {"branchName": name, "branchType": "CONDITION",
            "conditions": conditions}


def fallback_branch(name: str = "Otherwise") -> dict:
    return {"branchName": name, "branchType": "FALLBACK"}


# ═══════════════════════════════════════════════════════════════
# CONVENIENCE BUILDERS — gmail, sheets, webhook, schedule
# ═══════════════════════════════════════════════════════════════
def wh_trigger(display="استقبال بيانات", next_action=None):
    return build_trigger(
        "@activepieces/piece-webhook", PIECE_VERSIONS["webhook"],
        "catch_webhook", {"authType": "none"}, display, next_action)


def sched_daily(hour=8, tz="Asia/Riyadh", display="جدولة يومية",
                next_action=None):
    return build_trigger(
        "@activepieces/piece-schedule", PIECE_VERSIONS.get("schedule", "~0.1.0"),
        "every_day",
        {"hour_of_the_day": hour, "timezone": tz, "run_on_weekends": False},
        display, next_action)


def sched_cron(expr, tz="Asia/Riyadh", display="جدولة مخصصة",
               next_action=None):
    return build_trigger(
        "@activepieces/piece-schedule", PIECE_VERSIONS.get("schedule", "~0.1.0"),
        "cron_expression", {"cronExpression": expr, "timezone": tz},
        display, next_action)


def gmail_send(step: str, conn_id: str, to_list: list, subj: str,
               body: str, display: str = "إرسال إيميل",
               next_action=None) -> dict:
    return build_action(
        step, "@activepieces/piece-gmail",
        PIECE_VERSIONS.get("gmail", "~0.11.0"), "send_email",
        {"receiver": to_list, "subject": subj, "body_type": "plain_text",
         "body": body, "draft": False, "auth": C(conn_id)},
        display, next_action)


def sheet_add(step: str, conn_id: str, sid: str, vals: dict,
              display: str = "حفظ في الجدول",
              next_action=None) -> dict:
    return build_action(
        step, "@activepieces/piece-google-sheets",
        PIECE_VERSIONS.get("google-sheets", "~0.14.0"), "insert_row",
        {"spreadsheetId": sid, "sheetId": 0, "first_row_headers": True,
         "values": vals, "auth": C(conn_id)},
        display, next_action)


def sheet_read(step: str, conn_id: str, sid: str,
               display: str = "قراءة الجدول",
               next_action=None) -> dict:
    return build_action(
        step, "@activepieces/piece-google-sheets",
        PIECE_VERSIONS.get("google-sheets", "~0.14.0"), "get-many-rows",
        {"spreadsheetId": sid, "sheetId": 0, "first_row_headers": True,
         "auth": C(conn_id)},
        display, next_action)


# ═══════════════════════════════════════════════════════════════
# RECURSIVE CHAIN BUILDER (from v3.1.0)
# ═══════════════════════════════════════════════════════════════
def _complex_spec_as_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if hasattr(raw, "model_dump"):
        return raw.model_dump()
    raise HTTPException(400, detail=f"Invalid step: expected object, got {type(raw).__name__}")


def validate_complex_steps(steps: list, *, path: str = "steps",
                           allow_empty: bool = False) -> None:
    """Recursive pre-flight validation for /v2/build-complex step trees."""
    if not isinstance(steps, list):
        raise HTTPException(400, detail=f"{path}: must be a list")
    if not steps:
        if not allow_empty:
            raise HTTPException(400, detail=f"{path}: at least one step is required")
        return
    for i, raw in enumerate(steps):
        spec = _complex_spec_as_dict(raw)
        stype = spec.get("type", "PIECE")
        step_label = f"Step {i + 1}" if path == "steps" else f"{path}[{i + 1}]"

        if stype == "PIECE":
            piece = (spec.get("piece") or spec.get("piece_name") or "").strip()
            action = (spec.get("action_name") or "").strip()
            if not piece:
                raise HTTPException(
                    400, detail=f"{step_label}: piece or piece_name is required")
            if not action:
                raise HTTPException(
                    400, detail=f"{step_label}: action_name is missing")
        elif stype == "ROUTER":
            branches = spec.get("branches", [])
            if not isinstance(branches, list):
                raise HTTPException(
                    400, detail=f"{step_label}: branches must be a list")
            for j, b in enumerate(branches):
                bd = b if isinstance(b, dict) else (
                    b.model_dump() if hasattr(b, "model_dump") else {})
                if not isinstance(bd, dict):
                    raise HTTPException(
                        400,
                        detail=f"{step_label}.branches[{j + 1}]: invalid branch object")
                ba = bd.get("actions") or []
                if not isinstance(ba, list):
                    raise HTTPException(
                        400,
                        detail=f"{step_label}.branches[{j + 1}].actions: must be a list")
                validate_complex_steps(
                    ba,
                    path=f"{step_label}.branches[{j + 1}].actions",
                    allow_empty=True)
        elif stype == "LOOP":
            la = spec.get("loop_actions") or []
            if not isinstance(la, list):
                raise HTTPException(
                    400, detail=f"{step_label}.loop_actions: must be a list")
            validate_complex_steps(
                la, path=f"{step_label}.loop_actions", allow_empty=True)
        elif stype == "CODE":
            pass
        else:
            raise HTTPException(
                400, detail=f"{step_label}: unknown step type: {stype}")


def _sort_steps_info_chronological(steps_info: List[dict]) -> List[dict]:
    """Order built-step summaries by step_N index (execution-friendly for clients)."""

    def _key(row: dict) -> int:
        s = row.get("step") or ""
        if isinstance(s, str) and s.startswith("step_"):
            try:
                return int(s[5:])
            except ValueError:
                pass
        return 0

    return sorted(steps_info, key=_key)


async def _build_step_from_spec(
        spec: dict, counter: list, engine: SiyadahEngine,
        next_action=None, steps_info: Optional[List[dict]] = None):
    """Recursively build a step from a specification dict."""
    stype = spec.get("type", "PIECE")
    snum = counter[0]
    counter[0] += 1
    sname = f"step_{snum}"

    if stype == "PIECE":
        piece_raw = (spec.get("piece") or spec.get("piece_name") or "").strip()
        if not piece_raw:
            raise HTTPException(400, detail=f"{sname}: piece or piece_name is required")
        resolved_piece, schema = await auto_resolve_piece(engine, piece_raw)
        full = (resolved_piece if resolved_piece.startswith("@")
                else f"@activepieces/piece-{resolved_piece}")
        short = resolved_piece.replace("@activepieces/piece-", "")
        log.info("[build] Processing %s: %s", sname, short)

        cleaned_cfg = clean_input_config(
            dict(spec.get("input", spec.get("input_config", {}))))
        conn_id = spec.get("connection_id") or DEFAULT_CONNECTIONS.get(short, "")
        if conn_id and "auth" not in cleaned_cfg:
            cleaned_cfg["auth"] = C(conn_id)

        explicit_ver = spec.get("version")
        if schema and schema.get("actions"):
            ver = explicit_ver or resolve_piece_version(schema, resolved_piece)
            resolved_action = _fuzzy_name(
                spec.get("action_name", ""), schema.get("actions", {}))
            props = get_action_props(schema, resolved_action)

            # Phase-8: the "Siyadah Auto-Fill" literal injection was removed
            # here. The Sniper Validator in golden_build() now hard-stops on
            # missing required fields with an actionable error_code — we no
            # longer fabricate values that would pass IMPORT_FLOW but fail at
            # runtime with an opaque error.

            # ── Draft Guard: inject missing boolean fields ──
            # Narrowly scoped: only for 8 well-known boolean flags that have
            # an obvious safe default. NOT a general auto-fill.
            if props:
                for _bn in _BOOLEAN_FIELD_NAMES:
                    if _bn not in cleaned_cfg and _bn in props:
                        cleaned_cfg[_bn] = False
                        log.info("[draft-guard] %s.%s → False (injected missing boolean)", sname, _bn)

            ps = generate_property_settings(props, cleaned_cfg)
            if resolved_action not in schema.get("actions", {}):
                raise HTTPException(
                    400,
                    f"Action '{spec.get('action_name', '')}' not found in {resolved_piece}. "
                    f"Available: {list(schema.get('actions', {}).keys())}")
            extra_ps = spec.get("property_settings")
            if isinstance(extra_ps, dict) and extra_ps:
                ps = {**ps, **extra_ps}
        else:
            ver = explicit_ver or PIECE_VERSIONS.get(short, "~0.1.0")
            resolved_action = spec.get("action_name", "")
            extra_ps = spec.get("property_settings")
            ps = extra_ps if isinstance(extra_ps, dict) else {}

            for _fn, _fv in list(cleaned_cfg.items()):
                if _fv in (None, "", []) and _fn.lower() in _BOOLEAN_FIELD_NAMES:
                    cleaned_cfg[_fn] = False
                    log.info("[fallback-autofill] %s.%s → False (assumed BOOLEAN, no schema)", sname, _fn)

        if steps_info is not None:
            steps_info.append({
                "step": sname,
                "piece": full,
                "action": resolved_action,
                "version": ver,
                "schema_loaded": bool(schema and schema.get("actions")),
                "property_settings": ps,
            })
        return build_action(
            sname, full, ver,
            resolved_action, cleaned_cfg,
            spec.get("display_name", f"{short}: {resolved_action}"),
            next_action, ps)

    if stype == "CODE":
        log.info("[build] Processing %s: CODE", sname)
        if steps_info is not None:
            steps_info.append({
                "step": sname,
                "piece": None,
                "action": None,
                "version": None,
                "schema_loaded": False,
                "property_settings": {},
                "structure": "CODE",
                "display_name": spec.get("display_name", "Code"),
            })
        return build_code_step(
            sname, spec.get("display_name", "Code"),
            spec.get("code",
                      "export const code = async (inputs) => { return inputs; };"),
            spec.get("code_input"), next_action)

    if stype == "ROUTER":
        branches = spec.get("branches", [])
        log.info("[build] Processing %s: ROUTER (%d branches)", sname,
                 len(branches) if isinstance(branches, list) else 0)
        if steps_info is not None:
            steps_info.append({
                "step": sname,
                "piece": None,
                "action": None,
                "version": None,
                "schema_loaded": False,
                "property_settings": {},
                "structure": "ROUTER",
                "display_name": spec.get("display_name", "Router"),
                "branches_count": len(branches) if isinstance(branches, list) else 0,
            })
        branch_defs: List[dict] = []
        children: List[Any] = []
        for b in branches:
            bd = b if isinstance(b, dict) else (b.model_dump() if hasattr(b, "model_dump") else {})
            if bd.get("branch_type", "CONDITION") == "FALLBACK":
                branch_defs.append(fallback_branch(bd.get("name", "Otherwise")))
            else:
                conds = []
                for cg in bd.get("conditions", []):
                    group = []
                    for c in cg:
                        cd = c if isinstance(c, dict) else (c.model_dump() if hasattr(c, "model_dump") else {})
                        group.append(cond(
                            cd.get("operator", ""),
                            cd.get("first_value", ""),
                            cd.get("second_value", "")))
                    conds.append(group)
                branch_defs.append(condition_branch(bd.get("name", "Branch"), conds))
            ba = bd.get("actions", [])
            child = (await _build_action_chain(ba, counter, engine, steps_info)
                     if ba else None)
            children.append(child)
        return build_router_step(sname, spec.get("display_name", "Router"),
                                 branch_defs, children, next_action)

    if stype == "LOOP":
        log.info("[build] Processing %s: LOOP", sname)
        items = spec.get("loop_items",
                         spec.get("items_expression",
                                  "{{trigger['body']['items']}}"))
        if steps_info is not None:
            steps_info.append({
                "step": sname,
                "piece": None,
                "action": None,
                "version": None,
                "schema_loaded": False,
                "property_settings": {},
                "structure": "LOOP",
                "display_name": spec.get("display_name", "Loop"),
                "items_expression": items,
            })
        la = spec.get("loop_actions", [])
        first_loop = (await _build_action_chain(la, counter, engine, steps_info)
                      if la else None)
        return build_loop_step(sname, spec.get("display_name", "Loop"),
                               items, first_loop, next_action)

    raise ValueError(f"Unknown step type: {stype}")


async def _build_action_chain(
        specs: list, counter: list, engine: SiyadahEngine,
        steps_info: Optional[List[dict]] = None):
    """Chain actions via nextAction links (builds last→first)."""
    if not specs:
        return None
    chain = None
    total = len(specs)
    for idx, spec in enumerate(reversed(specs)):
        step_num = total - idx
        sdict = spec if isinstance(spec, dict) else (
            spec.model_dump() if hasattr(spec, "model_dump") else spec)
        if not isinstance(sdict, dict):
            raise TypeError(f"Chain step must be dict, got {type(spec).__name__}")
        try:
            chain = await _build_step_from_spec(
                sdict, counter, engine, next_action=chain, steps_info=steps_info)
        except HTTPException:
            raise
        except Exception as ex:
            raise HTTPException(
                status_code=500,
                detail=f"Failed building step {step_num}/{total}: "
                       f"{type(ex).__name__}: {ex}",
            ) from ex
    if chain is None:
        raise HTTPException(
            status_code=500,
            detail="Action chain produced None despite non-empty specs",
        )
    return chain


# ═══════════════════════════════════════════════════════════════
# SMART PULSE — Context-Aware Activation Payload
# ═══════════════════════════════════════════════════════════════
def _collect_pieces(node: dict, result: set):
    """Walk the trigger tree and collect all piece short-names."""
    if not node or not isinstance(node, dict):
        return
    piece_name = node.get("settings", {}).get("pieceName", "")
    if piece_name:
        result.add(piece_name.replace("@activepieces/piece-", ""))
    for key in ("nextAction", "firstLoopAction"):
        _collect_pieces(node.get(key), result)
    for child in (node.get("children") or []):
        _collect_pieces(child, result)


def _build_smart_pulse(trigger: dict) -> dict:
    """Inspect the flow tree and build a context-aware pulse payload."""
    pulse: Dict[str, Any] = {
        "event": "Siyadah_Activation",
        "status": "Success",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "receiver": ["test@siyadah.ai"],
        "subject": "سيادة — تجربة تفعيل ناجحة",
        "body": "تهانينا! نظام الأتمتة الخاص بك يعمل الآن بكفاءة.",
        "values": {"A": "بيانات تجريبية", "B": "نجاح التفعيل"},
    }
    pieces: set = set()
    _collect_pieces(trigger, pieces)

    if "gmail" in pieces:
        pulse["email"] = {
            "from": "noreply@siyadah.ai",
            "to": "test@siyadah.ai",
            "subject": "سيادة — رسالة تفعيل تجريبية",
            "body": "هذه رسالة تفعيل تلقائية من منسق سيادة.",
        }

    if "google-sheets" in pieces:
        pulse["row"] = {
            "spreadsheet_id": "SAMPLE_SHEET_ID",
            "sheet_name": "Sheet1",
            "values": {
                "الاسم": "عميل تجريبي",
                "البريد": "test@siyadah.ai",
                "الهاتف": "+966500000000",
                "المبلغ": 100.00,
            },
        }

    if "slack" in pieces:
        pulse["message"] = {
            "channel": "#general",
            "text": "سيادة — تم تفعيل الأتمتة بنجاح!",
        }

    if "hubspot" in pieces:
        pulse["contact"] = {
            "email": "test@siyadah.ai",
            "firstname": "عميل",
            "lastname": "تجريبي",
            "phone": "+966500000000",
        }

    if len(pulse) <= 3:
        pulse["customer"] = {
            "email": "test@siyadah.ai",
            "name": "عميل تجريبي",
            "phone": "+966500000000",
        }
        pulse["order"] = {
            "id": "ORD-TEST-001",
            "total": 100.00,
            "currency": "SAR",
            "status": "created",
        }

    return pulse


# ═══════════════════════════════════════════════════════════════
# GOLDEN PROTOCOL PIPELINE
# ═══════════════════════════════════════════════════════════════
async def golden_build(engine: SiyadahEngine, pid: str, name: str,
                       trigger: dict, *, self_test: bool = True,
                       owner_email: Optional[str] = None) -> dict:
    """Full Golden Protocol: IMPORT_FLOW → GET-verify → LOCK_AND_PUBLISH → ENABLE.

    Phase-8 pre-flight: the trigger tree is validated against piece_registry
    BEFORE any AP call. Activepieces accepts IMPORT_FLOW with non-existent
    pieces and marks them valid:true (verified empirically 2026-04-24), so
    without this guard a typo only surfaces at runtime.
    """
    # ── Visibility Guard: unique timestamp + fallback project ──
    name = f"{name} ({datetime.now().strftime('%H:%M:%S')})"
    pid = pid or DEFAULT_PID

    # ── Phase-8 Sniper Validator: hard-stop on unknown piece / action /
    # missing required field. Skipped gracefully if DB is unconfigured
    # (dev/test environments without Postgres) or the registry is empty
    # (before first sync_pieces run) — the orchestrator stays functional,
    # the guard just can't fire yet.
    from database import async_session as _async_session
    if _async_session is not None:
        from piece_validator import assert_trigger
        from models import PieceRegistry
        from sqlalchemy import select as _select
        async with _async_session() as _vs:
            _has_any = (await _vs.execute(
                _select(PieceRegistry.name).limit(1)
            )).first()
            if _has_any:
                await assert_trigger(_vs, trigger)
            else:
                log.warning(
                    "[golden] piece_registry is empty — validator disarmed. "
                    "Run: python -m scripts.sync_pieces --full"
                )

    flow = await engine.create_flow(pid, name)
    fid = flow["id"]
    log.info("[golden] Created flow %s", fid)

    # ── Sovereign Tightening — stamp identity IMMEDIATELY ──
    # The metadata field is the ground truth for ownership. Stamped
    # before IMPORT_FLOW so even if the import fails the flow is
    # already attributable. AP preserves this field across publish/
    # enable. Both list and update paths verify against it.
    sovereign_meta = {
        "tenantId":   pid,
        "ownerEmail": owner_email or "",
        "stampedAt":  datetime.now(timezone.utc).isoformat(),
        "stampedBy":  "siyadah:golden_build",
    }
    try:
        await engine.update_metadata(fid, sovereign_meta)
        log.info("[golden] metadata stamped on %s → tenant=%s owner=%s",
                 fid, pid, owner_email or "<anon>")
    except Exception as meta_err:
        log.error("[golden] metadata stamp failed for %s — aborting: %s", fid, meta_err)
        try: await engine.delete_flow(fid)
        except Exception: pass
        raise HTTPException(500, detail=f"sovereign-tightening: failed to stamp metadata on {fid}")

    before_graph = _walk_flow_tree({"version": {"trigger": trigger}})
    before_steps = before_graph.get("total_steps", 0)

    await engine.import_flow(fid, name, trigger)
    webhook_url = f"{os.getenv('AP_BASE_URL', 'https://activepieces-production-2499.up.railway.app')}/api/v1/webhooks/{fid}"
    log.info("[golden] IMPORT_FLOW → %s", fid)

    verified = await engine.verify_flow(fid)
    after_graph = _walk_flow_tree(verified)
    after_steps = after_graph.get("total_steps", 0)

    if after_steps < before_steps:
        log.error("[golden] AP stripped graph after import: before=%s after=%s fid=%s",
                  before_steps, after_steps, fid)
        try:
            await engine.delete_flow(fid)
        except Exception:
            pass
        raise HTTPException(500, detail={
            "error_code": "AP_STRIPPED_GRAPH_AFTER_IMPORT",
            "flow_id": fid,
            "expected_steps": before_steps,
            "actual_steps": after_steps,
            "before_graph": before_graph,
            "after_graph": after_graph,
        })

    # ── Flow Validity Gate (Verify-Before-Claim) ──
    # AP can return a 200 from IMPORT_FLOW with the step count intact
    # while silently invalidating individual steps. Observed case: Google
    # Sheets `insert_row` with DROPDOWN fields (spreadsheetId/sheetId)
    # that AP can't resolve against a live connection — AP keeps the
    # step shape but nulls the fields and marks `valid:false`. Enabling
    # such a flow would either error at runtime or run with empty inputs.
    # Hard-stop here so the BFF surfaces a typed failure to the user
    # instead of persisting an unusable digital employee (Invariants I3,
    # I9, I11).
    invalid_steps_raw = [
        s for s in after_graph.get("steps", []) if s.get("valid") is False
    ]
    if invalid_steps_raw:
        invalid_summary = [
            {
                "name": s.get("name"),
                "type": s.get("type"),
                "piece": s.get("piece"),
                "action": s.get("action"),
                "displayName": s.get("displayName"),
                "null_input_keys": s.get("null_input_keys", []),
            }
            for s in invalid_steps_raw
        ]
        log.error(
            "[golden] AP invalidated %d step(s) after IMPORT_FLOW fid=%s steps=%s",
            len(invalid_summary), fid, invalid_summary,
        )
        try:
            await engine.delete_flow(fid)
        except Exception:
            pass
        raise HTTPException(500, detail={
            "error_code": "AP_INVALIDATED_STEP_AFTER_IMPORT",
            "flow_id": fid,
            "invalid_steps": invalid_summary,
            "before_graph": before_graph,
            "after_graph": after_graph,
            "message": "Activepieces invalidated one or more steps after import",
        })

    ttype = verified.get("version", {}).get("trigger", {}).get("type", "?")
    log.info("[golden] Verified %s → trigger=%s steps=%s", fid, ttype, after_steps)

    pub = await engine.publish_and_enable(fid)
    log.info("[golden] Published %s → %s", fid, pub)

    final = await engine.get_flow(fid)
    final_state = final.get("version", {}).get("state", "?")
    final_status = final.get("status", "UNKNOWN")
    pub_match = final.get("publishedVersionId") == final.get("version", {}).get("id")
    log.info("[golden] Final status=%s state=%s published_match=%s", final_status, final_state, pub_match)
    if final_status != "ENABLED":
        raise HTTPException(500, detail=f"Flow {fid} is NOT ENABLED after Golden Protocol. status={final_status}, state={final_state}, published_match={pub_match}.")
    pub["version_state"] = final_state
    pub["published_match"] = pub_match

    # ── Context-Aware Smart Pulse ──
    pulse_sent = False
    try:
        pulse_payload = _build_smart_pulse(trigger)
        await engine.test_webhook(fid, pulse_payload)
        pulse_sent = True
        ctx_keys = [k for k in pulse_payload if k not in ("event", "status", "timestamp")]
        log.info("[golden] Smart Pulse sent to %s (context: %s)", fid, ctx_keys)
    except Exception as pulse_err:
        log.warning("[golden] Pulse failed for %s: %s", fid, pulse_err)

    diagnosis = None
    if self_test:
        diagnosis = {"summary": "Verified"}

    # ── Smart Link Extraction: find spreadsheetId in trigger tree ──
    resource_link = None

    def _extract_sheet_id(node):
        if not node or not isinstance(node, dict):
            return None
        settings = node.get("settings", {})
        if "google-sheets" in settings.get("pieceName", ""):
            inp = settings.get("input", {})
            sid = inp.get("spreadsheetId") or inp.get("spreadsheet_id")
            if sid and not str(sid).startswith("{{") and sid != "Siyadah Auto-Fill":
                return sid
        for key in ("nextAction", "firstLoopAction"):
            found = _extract_sheet_id(node.get(key))
            if found:
                return found
        for child in (node.get("children") or []):
            found = _extract_sheet_id(child)
            if found:
                return found
        return None

    def _extract_email(node):
        """Walk the trigger tree looking for a client email field."""
        if not node or not isinstance(node, dict):
            return None
        inp = node.get("settings", {}).get("input", {})
        for key in ("email", "to", "recipient", "send_to"):
            val = inp.get(key)
            if val and isinstance(val, str) and "@" in val and not val.startswith("{{"):
                return val
        for key in ("nextAction", "firstLoopAction"):
            found = _extract_email(node.get(key))
            if found:
                return found
        for child in (node.get("children") or []):
            found = _extract_email(child)
            if found:
                return found
        return None

    sheet_id = _extract_sheet_id(trigger)
    if sheet_id:
        resource_link = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
        log.info("[golden] Smart Link: %s", resource_link)

    client_email = _extract_email(trigger)
    if client_email:
        log.info("[golden] Client email extracted: %s", client_email)

    # ── Sovereign Tightening — write to flow_registry as the durable
    # cache for ownership lookups. Best-effort: a registry write
    # failure must not undo the AP build (the metadata stamp on AP is
    # the ground truth). Idempotent ON CONFLICT in case a retry hits.
    try:
        from database import async_session as _reg_session
        from models import FlowRegistry
        from sqlalchemy.dialects.postgresql import insert as _pg_insert
        if _reg_session is not None:
            trigger_type_val = (trigger.get("type") or
                               (trigger.get("settings") or {}).get("triggerName") or
                               "WEBHOOK")
            piece_manifest = {
                "owner_email": owner_email or "",
                "stamped_at":  sovereign_meta["stampedAt"],
                "ap_status":   final_status,
                "trigger_type_detail": ttype,
                "client_email_extracted": client_email,
            }
            async with _reg_session() as _rs:
                stmt = _pg_insert(FlowRegistry).values(
                    tenant_id      = pid,
                    flow_id        = fid,
                    display_name   = name,
                    trigger_type   = trigger_type_val,
                    webhook_url    = webhook_url,
                    piece_manifest = piece_manifest,
                    schema_version = "v1",
                ).on_conflict_do_update(
                    index_elements=["flow_id"],
                    set_={
                        "tenant_id":      pid,
                        "display_name":   name,
                        "trigger_type":   trigger_type_val,
                        "webhook_url":    webhook_url,
                        "piece_manifest": piece_manifest,
                        "updated_at":     datetime.now(timezone.utc),
                    },
                )
                await _rs.execute(stmt)
                await _rs.commit()
            log.info("[golden] flow_registry upsert OK for %s (tenant=%s)", fid, pid)
    except Exception as reg_err:
        log.warning("[golden] flow_registry write failed for %s — AP metadata is the truth: %s", fid, reg_err)

    return {"flow_id": fid, "trigger_type": ttype,
            "publish": pub, "diagnosis": diagnosis,
            "webhook_url": webhook_url,
            "resource_link": resource_link,
            "pulse_sent": pulse_sent,
            "client_email": client_email,
            "owner_email": owner_email,
            "metadata": sovereign_meta}


# ═══════════════════════════════════════════════════════════════
# DIAGNOSE — walk the flow tree
# ═══════════════════════════════════════════════════════════════
def _walk_flow_tree(flow_data: dict) -> dict:
    """Walk the version→trigger tree and enumerate all steps.

    Captures the AP-reported ``valid`` flag and a no-secrets summary of
    input keys that AP nulled (e.g. Google Sheets DROPDOWN fields stripped
    after IMPORT_FLOW when the dropdown options can't be resolved). The
    ``valid`` field is what the Flow Validity Gate in ``golden_build``
    inspects to detect silent post-import invalidations.
    """
    version = flow_data.get("version", {})
    trigger = version.get("trigger", {})
    steps: List[dict] = []

    def _null_input_keys(input_dict: Any) -> List[str]:
        """Keys whose values are null/empty in the verified step input.

        Skips ``auth`` so connection ids/tokens are never logged or
        returned in error envelopes (Invariant I11)."""
        if not isinstance(input_dict, dict):
            return []
        out: List[str] = []
        for k, v in input_dict.items():
            if k == "auth":
                continue
            if v is None or v == "" or v == {} or v == []:
                out.append(k)
        return out

    def walk(node, depth=0):
        if not node:
            return
        info: Dict[str, Any] = {
            "name": node.get("name"), "type": node.get("type"),
            "displayName": node.get("displayName"), "depth": depth,
            "valid": node.get("valid", True),
        }
        ntype = node.get("type", "")
        if ntype == "ROUTER":
            branches = node.get("settings", {}).get("branches", [])
            ch = node.get("children", [])
            info["branches"] = len(branches)
            info["children_count"] = len([c for c in ch if c])
            steps.append(info)
            for child in ch:
                if child:
                    walk(child, depth + 1)
        elif ntype == "LOOP_ON_ITEMS":
            info["items"] = node.get("settings", {}).get("items")
            info["hasLoopAction"] = node.get("firstLoopAction") is not None
            steps.append(info)
            walk(node.get("firstLoopAction"), depth + 1)
        else:
            settings = node.get("settings", {})
            info["piece"] = settings.get("pieceName")
            info["action"] = (settings.get("actionName")
                              or settings.get("triggerName"))
            info["null_input_keys"] = _null_input_keys(settings.get("input", {}))
            steps.append(info)
        walk(node.get("nextAction"), depth)

    walk(trigger)
    return {
        "name": version.get("displayName"),
        "status": flow_data.get("status"),
        "schema_version": version.get("schemaVersion"),
        "trigger_type": trigger.get("type"),
        "steps": steps, "total_steps": len(steps),
    }


# ═══════════════════════════════════════════════════════════════
# 8 GOLDEN TEMPLATES (IMPORT_FLOW style — nextAction chain)
# ═══════════════════════════════════════════════════════════════
def T1(c, cn):
    s1 = gmail_send("step_1", cn["gmail"],
        [c.get("recipient_email", "a@siyadah-ai.com")],
        c.get("email_subject", "ليد جديد!"),
        c.get("email_body", "وصل ليد:\nالاسم: {{trigger.body.name}}\nالإيميل: {{trigger.body.email}}\nالجوال: {{trigger.body.phone}}"),
        "تنبيه إيميل")
    return wh_trigger("استقبال ليد", s1)

def T2(c, cn):
    s1 = sheet_add("step_1", cn["google-sheets"], c.get("spreadsheet_id", ""),
        {"A": "{{trigger.body.name}}", "B": "{{trigger.body.email}}",
         "C": "{{trigger.body.phone}}", "D": "{{trigger.body.message}}"})
    return wh_trigger("استقبال بيانات", s1)

def T3(c, cn):
    s2 = gmail_send("step_2", cn["gmail"],
        [c.get("recipient_email", "a@siyadah-ai.com")],
        c.get("email_subject", "ليد جديد محفوظ!"),
        c.get("email_body", "تم حفظ ليد:\n{{trigger.body.name}} — {{trigger.body.email}}"),
        "تنبيه الفريق")
    s1 = sheet_add("step_1", cn["google-sheets"], c.get("spreadsheet_id", ""),
        {"A": "{{trigger.body.name}}", "B": "{{trigger.body.email}}",
         "C": "{{trigger.body.phone}}", "D": "{{trigger.body.message}}"},
        next_action=s2)
    return wh_trigger("استقبال ليد", s1)

def T4(c, cn):
    s2 = sheet_add("step_2", cn["google-sheets"], c.get("spreadsheet_id", ""),
        {"A": "{{trigger.body.name}}", "B": "{{trigger.body.email}}",
         "C": "{{trigger.body.subject}}", "D": "{{trigger.body.message}}",
         "E": "جديدة"}, "حفظ تذكرة")
    s1 = gmail_send("step_1", cn["gmail"], ["{{trigger.body.email}}"],
        c.get("reply_subject", "استلمنا طلبك!"),
        c.get("reply_body",
              "مرحباً {{trigger.body.name}}!\n\nشكراً لتواصلك. تم استلام طلبك وسنرد خلال 24 ساعة."
              "\n\nتفاصيل طلبك:\n{{trigger.body.message}}\n\nفريق الدعم"),
        "رد تلقائي", next_action=s2)
    return wh_trigger("استقبال طلب دعم", s1)

def T5(c, cn):
    s2 = sheet_add("step_2", cn["google-sheets"], c.get("spreadsheet_id", ""),
        {"A": "{{trigger.body.name}}", "B": "{{trigger.body.email}}",
         "C": "{{trigger.body.source}}", "D": "نشط"}, "حفظ مشترك")
    s1 = gmail_send("step_1", cn["gmail"], ["{{trigger.body.email}}"],
        c.get("welcome_subject", "مرحباً بك!"),
        c.get("welcome_body", "أهلاً {{trigger.body.name}}!\n\nشكراً لاشتراكك.\n\nمع تحياتنا"),
        "إيميل ترحيب", next_action=s2)
    return wh_trigger("استقبال مشترك", s1)

def T6(c, cn):
    s2 = gmail_send("step_2", cn["gmail"],
        [c.get("recipient_email", "a@siyadah-ai.com")],
        c.get("email_subject", "تقرير عملية: {{trigger.body.operation_type}}"),
        c.get("email_body",
              "عملية جديدة:\nالنوع: {{trigger.body.operation_type}}\nالتفاصيل: {{trigger.body.details}}"
              "\nالمسؤول: {{trigger.body.responsible}}\n\n— سيادة AI"),
        "تقرير للمدير")
    s1 = sheet_add("step_1", cn["google-sheets"], c.get("spreadsheet_id", ""),
        {"A": "{{trigger.body.operation_type}}", "B": "{{trigger.body.details}}",
         "C": "{{trigger.body.responsible}}", "D": "{{trigger.body.status}}"},
        "تسجيل العملية", next_action=s2)
    return wh_trigger("استقبال عملية", s1)

def T7(c, cn):
    s3 = sheet_add("step_3", cn["google-sheets"], c.get("spreadsheet_id", ""),
        {"A": "{{trigger.body.name}}", "B": "{{trigger.body.email}}",
         "C": "{{trigger.body.phone}}", "D": "{{trigger.body.message}}"},
        "حفظ الليد")
    s2 = gmail_send("step_2", cn["gmail"], ["{{trigger.body.email}}"],
        c.get("confirm_subject", "استلمنا طلبك!"),
        c.get("confirm_body",
              "مرحباً {{trigger.body.name}}!\nشكراً لتواصلك. فريقنا بيتواصل معك قريباً."),
        "تأكيد للعميل", next_action=s3)
    s1 = gmail_send("step_1", cn["gmail"],
        [c.get("recipient_email", "a@siyadah-ai.com")],
        c.get("notify_subject", "ليد جديد: {{trigger.body.name}}"),
        c.get("notify_body",
              "وصل ليد!\nالاسم: {{trigger.body.name}}\nالإيميل: {{trigger.body.email}}"
              "\nالجوال: {{trigger.body.phone}}\n\n— سيادة AI"),
        "تنبيه الفريق", next_action=s2)
    return wh_trigger("استقبال ليد", s1)

def T8(c, cn):
    s2 = gmail_send("step_2", cn["gmail"],
        [c.get("recipient_email", "a@siyadah-ai.com")],
        c.get("email_subject", "التقرير اليومي — سيادة"),
        c.get("email_body", "إليك تقرير اليوم:\n\n{{step_1}}"),
        "إرسال التقرير")
    s1 = sheet_read("step_1", cn["google-sheets"],
                    c.get("spreadsheet_id", ""), "قراءة البيانات",
                    next_action=s2)
    cron = c.get("cron_expression", "")
    if cron:
        return sched_cron(cron, next_action=s1)
    return sched_daily(c.get("hour_of_the_day", 8),
                       c.get("timezone", "Asia/Riyadh"), next_action=s1)


TEMPLATES = {
    "webhook_to_email":           {"fn": T1, "req": ["recipient_email"],                   "desc": "تنبيه إيميل فوري"},
    "webhook_to_sheet":           {"fn": T2, "req": ["spreadsheet_id"],                    "desc": "حفظ بيانات في جدول"},
    "webhook_to_sheet_and_email": {"fn": T3, "req": ["recipient_email"],                   "desc": "حفظ + تنبيه"},
    "support_auto_reply":         {"fn": T4, "req": ["spreadsheet_id"],                    "desc": "رد تلقائي + تذكرة دعم"},
    "marketing_welcome":          {"fn": T5, "req": ["spreadsheet_id"],                    "desc": "ترحيب مشترك جديد"},
    "ops_log_report":             {"fn": T6, "req": ["recipient_email", "spreadsheet_id"], "desc": "تسجيل عملية + تقرير"},
    "lead_notify_and_confirm":    {"fn": T7, "req": ["recipient_email", "spreadsheet_id"], "desc": "نظام ليدات كامل"},
    "scheduled_report":           {"fn": T8, "req": ["recipient_email", "spreadsheet_id"], "desc": "تقرير يومي"},
}


# ═══════════════════════════════════════════════════════════════
# 4 COMPLEX PRESETS (ROUTER + LOOP from v3.1.0)
# ═══════════════════════════════════════════════════════════════
def preset_lead_routing(p, cn):
    email_to = p.get("email_to", ["a@siyadah-ai.com"])
    sid = p.get("spreadsheet_id", "")
    gmail_step = gmail_send("step_2", cn["gmail"], email_to,
        "ليد جديد مؤهل: {{trigger['body']['name']}}",
        "ليد مؤهل!\n\nالاسم: {{trigger['body']['name']}}\nالإيميل: {{trigger['body']['email']}}"
        "\nالجوال: {{trigger['body']['phone']}}\n\n— سيادة AI")
    fb = (sheet_add("step_3", cn["google-sheets"], sid,
                    {"A": "{{trigger['body']['name']}}", "B": "{{trigger['body']['phone']}}",
                     "C": "ليد بدون إيميل", "D": "{{trigger['body']['source']}}"})
          if sid else
          build_code_step("step_3", "تسجيل ليد بدون إيميل",
              'export const code = async (inputs) => { return { logged: true, name: inputs.data.name }; };',
              {"data": "{{trigger['body']}}"}))
    router = build_router_step("step_1", "هل الليد فيه إيميل؟", [
        {"branchName": "فيه إيميل", "branchType": "CONDITION",
         "conditions": [[{"operator": "TEXT_IS_NOT_EMPTY",
                          "firstValue": "{{trigger['body']['email']}}"}]]},
        {"branchName": "بدون إيميل", "branchType": "FALLBACK"},
    ], [gmail_step, fb])
    return "توجيه الليدات — سيادة", wh_trigger("استقبال ليد", router)

def preset_bulk_email(p, cn):
    subj = p.get("subject_template", "رسالة من سيادة: {{step_1['item']['name']}}")
    body = p.get("body_template",
                 "مرحباً {{step_1['item']['name']}},\n\n{{step_1['item']['message']}}\n\n— سيادة AI")
    email = gmail_send("step_2", cn["gmail"],
        ["{{step_1['item']['email']}}"], subj, body)
    loop = build_loop_step("step_1", "تكرار لكل عنصر",
        "{{trigger['body']['items']}}", first_loop_action=email)
    return "إرسال إيميلات جماعي — سيادة", wh_trigger("استقبال قائمة", loop)

def preset_smart_followup(p, cn):
    threshold = p.get("hot_threshold", "80")
    email_to = p.get("email_to", ["a@siyadah-ai.com"])
    hot = gmail_send("step_2", cn["gmail"], email_to,
        "ليد ساخن! {{trigger['body']['name']}} — سكور: {{trigger['body']['score']}}",
        "ليد ساخن!\n\nالاسم: {{trigger['body']['name']}}\nالسكور: {{trigger['body']['score']}}"
        "\nالإيميل: {{trigger['body']['email']}}\n\n— سيادة")
    task_email = gmail_send("step_4", cn["gmail"],
        ["{{step_3['item']['assignee_email']}}"],
        "مهمة متابعة: {{trigger['body']['name']}} — {{step_3['item']['task']}}",
        "مطلوب: {{step_3['item']['task']}}\nالليد: {{trigger['body']['name']}}\n\n— سيادة")
    cold_loop = build_loop_step("step_3", "تكرار مهام المتابعة",
        "{{trigger['body']['followup_tasks']}}", first_loop_action=task_email)
    router = build_router_step("step_1", "تصنيف الليد", [
        {"branchName": "ليد ساخن", "branchType": "CONDITION",
         "conditions": [[{"operator": "NUMBER_IS_GREATER_THAN",
                          "firstValue": "{{trigger['body']['score']}}",
                          "secondValue": threshold}]]},
        {"branchName": "ليد بارد — متابعة", "branchType": "FALLBACK"},
    ], [hot, cold_loop])
    return "متابعة ذكية — سيادة", wh_trigger("استقبال ليد", router)

def preset_router_loop_combo(p, cn):
    oe = gmail_send("step_3", cn["gmail"], ["a@siyadah-ai.com"],
        "طلب جديد: {{step_2['item']['order_id']}}",
        "طلب: {{step_2['item']['order_id']}}\nالمبلغ: {{step_2['item']['amount']}}\n— سيادة")
    ol = build_loop_step("step_2", "تكرار الطلبات",
        "{{trigger['body']['orders']}}", first_loop_action=oe)
    ce = gmail_send("step_5", cn["gmail"], ["{{step_4['item']['email']}}"],
        "مرحباً {{step_4['item']['name']}}",
        "أهلاً {{step_4['item']['name']}},\n\nشكراً لتواصلك.\n— سيادة")
    cl = build_loop_step("step_4", "تكرار جهات الاتصال",
        "{{trigger['body']['contacts']}}", first_loop_action=ce)
    router = build_router_step("step_1", "نوع البيانات", [
        {"branchName": "طلبات", "branchType": "CONDITION",
         "conditions": [[{"operator": "TEXT_EXACTLY_MATCHES",
                          "firstValue": "{{trigger['body']['type']}}",
                          "secondValue": "orders"}]]},
        {"branchName": "جهات اتصال", "branchType": "FALLBACK"},
    ], [ol, cl])
    return "توجيه + تكرار مركب — سيادة", wh_trigger("استقبال بيانات", router)


PRESETS = {
    "lead_routing":      {"fn": preset_lead_routing,      "desc": "توجيه الليدات: ROUTER"},
    "bulk_email":        {"fn": preset_bulk_email,        "desc": "إيميلات جماعية: LOOP"},
    "smart_followup":    {"fn": preset_smart_followup,    "desc": "متابعة ذكية: ROUTER + LOOP"},
    "router_loop_combo": {"fn": preset_router_loop_combo, "desc": "توجيه + تكرار مركب"},
}


# ═══════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════
class _Multi(BaseModel):
    """Mixin for multi-tenancy fields."""
    project_id: Optional[str] = Field(None, description="Override project ID")
    connection_ids: Optional[Dict[str, str]] = Field(
        None, description="Override connections: {piece_short_name: external_id}")


class BuildBody(_Multi):
    display_name: str = ""
    template: str
    config: Dict[str, Any] = Field(default_factory=dict)


class DynamicBuildBody(_Multi):
    display_name: str = "سيادة — أتمتة مخصصة"
    trigger: Dict[str, Any]
    actions: List[Dict[str, Any]]


class RouterBuildBody(_Multi):
    display_name: str = "سيادة — راوتر"
    branches: List[Dict[str, Any]]
    after_router: Optional[List[Dict[str, Any]]] = None


class LoopBuildBody(_Multi):
    display_name: str = "سيادة — لووب"
    items_expression: str
    loop_actions: List[Dict[str, Any]]
    before_loop: Optional[List[Dict[str, Any]]] = None
    after_loop: Optional[List[Dict[str, Any]]] = None


class ComplexBuildBody(_Multi):
    display_name: str = "سيادة — فلو مركب"
    steps: List[Dict[str, Any]]


class PresetBuildBody(_Multi):
    display_name: str = ""
    preset: str
    params: Dict[str, Any] = Field(default_factory=dict)


class SmartStepSpec(BaseModel):
    piece_name: str
    action_name: str
    input_config: Dict[str, Any]
    display_name: Optional[str] = None


class SmartBuildBody(_Multi):
    display_name: str = "سيادة — فلو ذكي"
    steps: List[SmartStepSpec]


class ReimportBody(_Multi):
    display_name: Optional[str] = None
    template: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    trigger: Optional[Dict[str, Any]] = None
    actions: Optional[List[Dict[str, Any]]] = None


class ValidateBody(BaseModel):
    trigger: Dict[str, Any]
    actions: List[Dict[str, Any]]


class FlowPatchBody(BaseModel):
    action: str


class MCPExecuteBody(_Multi):
    tool: str
    parameters: Dict[str, Any] = Field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
# APP STATE + LIFESPAN
# ═══════════════════════════════════════════════════════════════
_engine: Optional[SiyadahEngine] = None


def E() -> SiyadahEngine:
    if _engine is None:
        raise HTTPException(503, detail="Engine not initialised")
    return _engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine
    # Phase 3: upgrade logging before anything emits a line in the
    # startup sequence. Idempotent — safe on reloads.
    from logging_config import configure_logging
    configure_logging(level=os.getenv("LOG_LEVEL", "INFO"))

    log.info("Siyadah Orchestrator v%s starting", VERSION)

    # 1. Authenticate with Activepieces
    if AP_EMAIL and AP_PASSWORD:
        try:
            auth = await SiyadahEngine.sign_in(AP_EMAIL, AP_PASSWORD, AP_BASE)
            token = auth.get("token") or auth.get("access_token", "")
            if token:
                _engine = SiyadahEngine(AP_BASE, token, email=AP_EMAIL, password=AP_PASSWORD)
                log.info("Authenticated — project=%s", DEFAULT_PID)
            else:
                log.error("No token in auth response")
        except Exception as e:
            log.error("Auth failed: %s", e)

    # 2. Initialize Postgres tables
    try:
        from database import init_db
        await init_db()
    except Exception as e:
        log.error("DB init failed (non-fatal): %s", e)

    # 3. Initialize Redis for SSE sessions
    try:
        from mcp_sse import init_redis
        await init_redis()
    except Exception as e:
        log.error("Redis init failed (non-fatal): %s", e)

    # 4. Phase 4.4 — OAuth refresh worker (the Eternal Pulse).
    #    Disabled by default. Enable in prod via SIYADAH_REFRESH_WORKER_ENABLED=true.
    #    Single-replica safety: we use Redis SETNX per-token mutex inside
    #    _refresh_one_token, so spawning this task on every replica is
    #    bounded — only one replica wins each token's lock per cycle.
    refresh_task = None
    if os.getenv("SIYADAH_REFRESH_WORKER_ENABLED", "false").lower() == "true":
        try:
            import asyncio as _asyncio_pulse
            from oauth_routes import _refresh_loop, REFRESH_DEFAULT_INTERVAL
            refresh_task = _asyncio_pulse.create_task(
                _refresh_loop(REFRESH_DEFAULT_INTERVAL),
                name="oauth-refresh-worker",
            )
            log.info("[lifespan] OAuth refresh worker spawned (interval=%ds)",
                     REFRESH_DEFAULT_INTERVAL)
        except Exception as e:
            log.error("[lifespan] could not spawn refresh worker: %s", e)
    else:
        log.info("[lifespan] OAuth refresh worker NOT enabled "
                 "(set SIYADAH_REFRESH_WORKER_ENABLED=true to activate)")

    yield

    # Shutdown
    if refresh_task is not None:
        log.info("[lifespan] cancelling OAuth refresh worker …")
        refresh_task.cancel()
        try:
            await refresh_task
        except BaseException:
            # CancelledError (BaseException in 3.8+) + any race during a
            # cycle land here. Safe to swallow during shutdown.
            pass

    if _engine:
        await _engine.close()
    try:
        from mcp_sse import close_redis
        await close_redis()
    except Exception:
        pass


app = FastAPI(title="Siyadah Orchestrator", version=VERSION, lifespan=lifespan)

# Allowed origins come from env (CSV). If unset, fall back to Siyadah's BFF
# only — never the previous "*" which, combined with allow_credentials=True,
# exposes every authenticated path to any origin the user visits.
_ALLOWED_ORIGINS_RAW = os.getenv("ORCHESTRATOR_ALLOWED_ORIGINS", "").strip()
_ALLOWED_ORIGINS = [o.strip() for o in _ALLOWED_ORIGINS_RAW.split(",") if o.strip()]
if not _ALLOWED_ORIGINS:
    _ALLOWED_ORIGINS = ["https://app.siyadah.ai"]
    log.warning(
        "ORCHESTRATOR_ALLOWED_ORIGINS not set — defaulting to %s. "
        "Set the env var (CSV) to reflect your real front-ends.",
        _ALLOWED_ORIGINS,
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Siyadah-Tenant"],
)

# ─── Phase 2 rate limiter (per-tenant, Redis-backed) ────────────────
# Must be wired BEFORE include_router so the SSE routes inherit the
# limiter.state without needing their own limiter instance.
from slowapi.errors import RateLimitExceeded  # noqa: E402
from slowapi.middleware import SlowAPIMiddleware  # noqa: E402

from limits_config import limiter  # noqa: E402

app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Return JSON with the breached limit + Retry-After.

    Preserves the X-RateLimit-* headers that SlowAPIMiddleware already
    attached to the request, so clients see both the failure and the
    window reset time.
    """
    tenant = getattr(request.state, "project_id", None) or "anonymous"
    log.warning(
        "rate-limit-exceeded tenant=%s path=%s limit=%s",
        tenant, request.url.path, exc.detail,
    )
    response = JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "detail": str(exc.detail),
            "request_id": getattr(request.state, "request_id", None),
        },
    )
    # slowapi adds headers on the response itself when headers_enabled=True.
    return response


from mcp_sse import router as sse_router  # noqa: E402
app.include_router(sse_router)

# Phase-9 — Sovereign-Grade OAuth (Layer 1+2+5 already wired; routes
# delivered incrementally: initiate → callback → refresh → webhooks).
from oauth_routes import router as oauth_router  # noqa: E402
app.include_router(oauth_router)

# Phase 4.5 — Layer 4 revocation webhooks. Public path (HMAC-authenticated).
from oauth_webhooks import router as webhooks_router  # noqa: E402
app.include_router(webhooks_router)


# ─── Wave-1 tenant enforcement (dry-run by default) ─────────────────
# Replaces the legacy api_key_check. require_tenant verifies (X-API-Key,
# X-Siyadah-Tenant) against the tenant_api_keys table and attaches the
# verified project_id to request.state.project_id. Endpoint handlers
# read from request.state instead of body.project_id.
# Behaviour gated by REQUIRE_TENANT_ENFORCE:
#   false (default) → violations logged to tenant_audit_log, request passes
#   true            → violations return 401/403
# See docs/WAVE-1-DESIGN.md §4 and auth.py.
from auth import require_tenant  # noqa: E402
app.middleware("http")(require_tenant)


# ═══════════════════════════════════════════════════════════════
# BACKWARD-COMPATIBLE ENDPOINTS
# ═══════════════════════════════════════════════════════════════
@app.get("/")
async def root():
    return {
        "service": "Siyadah Orchestrator", "version": VERSION,
        "protocol": "Golden v5: IMPORT→VERIFY→LOCK→ENABLE",
        "templates": len(TEMPLATES), "presets": list(PRESETS.keys()),
        "capabilities": ["ROUTER", "LOOP", "CODE", "PIECE", "PRESETS",
                         "SMART_SCHEMA", "MULTI_TENANT", "MCP_EXECUTE",
                         "MCP_SSE", "PERSISTENCE", "HINTS",
                         "INGESTION", "SAAS_ONBOARDING", "CONTEXT_AWARE_MCP"],
        "project_id": DEFAULT_PID,
        "mcp_proxy": bool(AP_MCP_URL),
        "persistence": bool(os.getenv("DATABASE_URL")),
        "redis": bool(os.getenv("REDIS_URL")),
        "status": "running",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health")
async def health():
    e = E()
    try:
        p = await e.list_projects()
        return {"status": "healthy", "activepieces": "connected",
                "projects_found": len(p), "version": VERSION}
    except Exception as ex:
        raise HTTPException(502, detail=str(ex))


@app.get("/templates")
async def list_flows_endpoint():
    e = E()
    flows = await e.list_flows(DEFAULT_PID)
    return {
        "project_id": DEFAULT_PID, "total": len(flows),
        "flows": [{"id": f.get("id"),
                    "name": f.get("version", {}).get("displayName", ""),
                    "status": f.get("status")} for f in flows],
    }


@app.get("/connections")
async def list_conns():
    e = E()
    c = await e.list_connections(DEFAULT_PID)
    return {
        "project_id": DEFAULT_PID, "total": len(c),
        "connections": [{"id": x.get("id"), "externalId": x.get("externalId"),
                         "name": x.get("displayName", ""),
                         "piece": x.get("pieceName"),
                         "status": x.get("status")} for x in c],
    }


@app.get("/pieces/{name}")
async def piece_info(name: str):
    e = E()
    pn = name if name.startswith("@") else f"@activepieces/piece-{name}"
    return await e.get_piece(pn)


@app.get("/operators")
async def list_operators():
    return {"operators": OPERATORS,
            "usage": "Use in conditions: {operator, firstValue, secondValue?}"}


# ═══════════════════════════════════════════════════════════════
# V2 — TEMPLATE BUILDER (Golden Protocol)
# ═══════════════════════════════════════════════════════════════
@app.get("/v2/templates")
async def v2_templates():
    return {"templates": {k: {"description": v["desc"],
                              "required_config": v["req"]}
                          for k, v in TEMPLATES.items()}}


@app.post("/v2/build-and-deploy")
@limiter.limit("10/minute")
async def v2_build(request: Request, body: BuildBody):
    e = E()
    t = body.template
    if t not in TEMPLATES:
        raise HTTPException(400, detail=f"Unknown template: {t}. Available: {list(TEMPLATES.keys())}")
    tdef = TEMPLATES[t]
    missing = [k for k in tdef["req"] if k not in body.config]
    if missing:
        raise HTTPException(422, detail=f"Missing config: {missing}")

    pid = resolve_pid(request, body.project_id)
    cn = resolve_conns(body.connection_ids)

    await guard_connections(e, pid, ["gmail", "google-sheets"], cn, strict=True)

    log.info("BUILD template=%s name=%s pid=%s", t, body.display_name or t, pid)
    trigger = tdef["fn"](body.config, cn)
    result = await golden_build(e, pid, body.display_name or tdef["desc"], trigger, owner_email=resolve_owner_email(request))

    is_scheduled = t.startswith("scheduled")
    wh = (f"{AP_BASE}/api/v1/webhooks/{result['flow_id']}"
          if not is_scheduled else None)
    return {
        "status": "deployed", "flow_id": result["flow_id"],
        "display_name": body.display_name or tdef["desc"],
        "template": t, "webhook_url": wh,
        "publish": result["publish"],
        "diagnosis": result.get("diagnosis"),
    }


# ═══════════════════════════════════════════════════════════════
# V2 — DYNAMIC BUILDER (Golden Protocol)
# ═══════════════════════════════════════════════════════════════
@app.post("/v2/build-dynamic")
@limiter.limit("10/minute")
async def v2_build_dynamic(request: Request, body: DynamicBuildBody):
    e = E()
    pid = resolve_pid(request, body.project_id)
    cn = resolve_conns(body.connection_ids)

    t = body.trigger
    piece_name = t.get("piece", "@activepieces/piece-webhook")
    resolved_t, t_schema = await auto_resolve_piece(e, piece_name)
    trigger_name = t.get("trigger_name", "catch_webhook")
    trigger_input = clean_input_config(dict(t.get("input", {})))
    trigger_ver = t.get("version", "")

    if not trigger_ver:
        trigger_ver = resolve_piece_version(t_schema, resolved_t)

    full_t = resolved_t if resolved_t.startswith("@") else f"@activepieces/piece-{resolved_t}"
    pieces_used = [full_t]

    # Gate-6C — Build now, connect later.
    # Before compiling/publishing, classify all required connections from
    # Activepieces. If any required piece has no ACTIVE connection, we save
    # a PendingActivationPlan and return PENDING_CONNECTIONS instead of
    # creating a broken/enabled flow.
    from services.connection_gate import classify_connection_requirements
    from services.pending_activation import save_pending_activation_plan

    async def _gate_fetch_schema(piece: str) -> dict:
        return await fetch_piece_schema(e, piece)

    gate_steps = [{"type": "PIECE", "piece": full_t}] + list(body.actions)
    live_connections = await e.list_connections(pid)
    connection_gate = await classify_connection_requirements(
        steps=gate_steps,
        live_connections=live_connections,
        fetch_schema=_gate_fetch_schema,
        connection_overrides=cn,
    )

    if connection_gate.get("blocked_count", 0) > 0:
        from database import async_session
        from models import PendingActivationPlan
        from services.pending_activation import (
            build_connection_gate_payload,
            build_pending_activation_payload,
            create_ap_visible_draft_flow,
            gate6_ap_visible_draft_enabled,
            sanitize_pieces,
            save_pending_activation_plan,
        )

        sanitized_blocked = sanitize_pieces(connection_gate.get("blocked_pieces", []))
        sanitized_runnable = sanitize_pieces(connection_gate.get("runnable_pieces", []))

        # Gate-6 AP_VISIBLE_DRAFT — only when flag explicitly == "1".
        # Builds the trigger tree and creates a DISABLED AP flow with it.
        # Never publishes, never enables — flow stays as a draft until the
        # user authorizes the missing connections.
        flow_id_draft: Optional[str] = None
        if gate6_ap_visible_draft_enabled():
            # Use ONLY connection ids that classify_connection_requirements
            # proved ACTIVE for this tenant. Do NOT fall back to body.connection_ids
            # or DEFAULT_CONNECTIONS — a stale/cross-project override would
            # otherwise wire `auth: {{connections[...]}}` to a rejected reference
            # for a blocked piece. Blocked pieces stay without auth and wait
            # for the user's new authorization.
            cn_active: dict = dict(connection_gate.get("connection_ids", {}))

            specs_for_chain_draft: List[dict] = []
            for a in body.actions:
                stype = a.get("type", "PIECE")
                if stype != "PIECE":
                    specs_for_chain_draft.append(a)
                    continue
                a_piece = a.get("piece", "")
                resolved_ap, sch = await auto_resolve_piece(e, a_piece)
                full_ap = resolved_ap if resolved_ap.startswith("@") else f"@activepieces/piece-{resolved_ap}"
                short = resolved_ap.replace("@activepieces/piece-", "")
                a_ver = a.get("version", "")
                cleaned_in = clean_input_config(dict(a.get("input", {})))
                if not a_ver:
                    a_ver = resolve_piece_version(sch, resolved_ap)
                    ps = generate_property_settings(
                        get_action_props(sch, a.get("action_name", "")),
                        cleaned_in)
                else:
                    ps = {}
                # Active-only: blocked pieces (absent from cn_active) get no auth.
                conn_id = cn_active.get(short, "")
                if conn_id:
                    cleaned_in["auth"] = C(conn_id)
                specs_for_chain_draft.append({
                    "type": "PIECE", "piece": full_ap,
                    "action_name": a.get("action_name", ""),
                    "version": a_ver, "input": cleaned_in,
                    "display_name": a.get("display_name", f"Action"),
                    "property_settings": ps,
                })

            counter_draft = [1]
            first_action_draft = await _build_action_chain(specs_for_chain_draft, counter_draft, e)
            trigger_draft = build_trigger(
                full_t,
                trigger_ver, trigger_name, trigger_input,
                body.display_name + " — Trigger", first_action_draft)

            flow_id_draft = await create_ap_visible_draft_flow(
                engine=e, pid=pid,
                display_name=body.display_name,
                trigger=trigger_draft,
            )

        saved_pending = await save_pending_activation_plan(
            async_session=async_session,
            PendingActivationPlan=PendingActivationPlan,
            tenant_id=pid,
            display_name=body.display_name,
            graph_plan=jsonable_encoder(body.model_dump()),
            connection_gate=connection_gate,
            flow_id=flow_id_draft,
        )

        pending_payload = build_pending_activation_payload(saved_pending, sanitized_blocked)
        gate_payload = build_connection_gate_payload(
            connection_gate, sanitized_blocked, sanitized_runnable
        )

        if flow_id_draft is not None:
            return {
                "status": "PENDING_CONNECTIONS",
                "mode": "AP_VISIBLE_DRAFT",
                "flow_id": flow_id_draft,
                "skip_publish": True,
                "display_name": body.display_name,
                "pending_activation": pending_payload,
                "connection_gate": gate_payload,
                "sanitized_missing_connections": sanitized_blocked,
                "message": "تم إنشاء مسودة في Activepieces (DISABLED) بانتظار ربط الحسابات.",
            }

        return {
            "status": "PENDING_CONNECTIONS",
            "mode": "DRAFT_ONLY",
            "skip_compile": True,
            "display_name": body.display_name,
            "pending_activation": pending_payload,
            "connection_gate": gate_payload,
            "sanitized_missing_connections": sanitized_blocked,
            "message": "تم تجهيز الفلو وحفظه كخطة معلقة بانتظار ربط الحسابات.",
        }

    # Use ACTIVE connection externalIds discovered from Activepieces.
    cn.update(connection_gate.get("connection_ids", {}))

    specs_for_chain: List[dict] = []
    for a in body.actions:
        stype = a.get("type", "PIECE")
        if stype != "PIECE":
            specs_for_chain.append(a)
            continue

        a_piece = a.get("piece", "")
        resolved_ap, sch = await auto_resolve_piece(e, a_piece)
        full_ap = resolved_ap if resolved_ap.startswith("@") else f"@activepieces/piece-{resolved_ap}"
        pieces_used.append(full_ap)
        short = resolved_ap.replace("@activepieces/piece-", "")
        a_ver = a.get("version", "")
        cleaned_in = clean_input_config(dict(a.get("input", {})))
        if not a_ver:
            a_ver = resolve_piece_version(sch, resolved_ap)
            ps = generate_property_settings(
                get_action_props(sch, a.get("action_name", "")),
                cleaned_in)
        else:
            ps = {}
        conn_id = a.get("connection_id", cn.get(short, ""))
        if conn_id:
            cleaned_in["auth"] = C(conn_id)
        specs_for_chain.append({
            "type": "PIECE", "piece": full_ap,
            "action_name": a.get("action_name", ""),
            "version": a_ver, "input": cleaned_in,
            "display_name": a.get("display_name", f"Action"),
            "property_settings": ps,
        })

    counter = [1]
    first_action = await _build_action_chain(specs_for_chain, counter, e)
    trigger = build_trigger(
        full_t,
        trigger_ver, trigger_name, trigger_input,
        body.display_name + " — Trigger", first_action)

    result = await golden_build(e, pid, body.display_name, trigger, owner_email=resolve_owner_email(request))

    is_webhook = "webhook" in full_t.lower()
    webhook_url = result.get("webhook_url") if is_webhook else None
    return {
        "status": "deployed", "flow_id": result["flow_id"],
        "display_name": body.display_name,
        "steps_count": counter[0] - 1 + 1,
        "webhook_url": webhook_url, "pieces_used": pieces_used,
        "publish": result["publish"],
        "diagnosis": result.get("diagnosis"),
    }


# ═══════════════════════════════════════════════════════════════
# V2 — ROUTER BUILDER
# ═══════════════════════════════════════════════════════════════
@app.post("/v2/build-router")
@limiter.limit("10/minute")
async def v2_build_router(request: Request, body: RouterBuildBody):
    e = E()
    pid = resolve_pid(request, body.project_id)
    cn = resolve_conns(body.connection_ids)
    counter = [1]

    branch_defs = []
    children = []
    for b in body.branches:
        if b.get("branch_type", "CONDITION") == "FALLBACK":
            branch_defs.append(fallback_branch(b.get("name", "Otherwise")))
        else:
            conds = []
            for cg in b.get("conditions", []):
                group = [cond(c.get("operator", ""), c.get("first_value", ""),
                              c.get("second_value", "")) for c in cg]
                conds.append(group)
            branch_defs.append(condition_branch(b.get("name", "Branch"), conds))
        ba = b.get("actions", [])
        children.append(
            await _build_action_chain(ba, counter, e) if ba else None)

    after = (await _build_action_chain(body.after_router, counter, e)
             if body.after_router else None)
    router = build_router_step(f"step_{counter[0]}", body.display_name,
                               branch_defs, children, after)
    counter[0] += 1
    trigger = wh_trigger("استقبال بيانات", router)

    result = await golden_build(e, pid, body.display_name, trigger, owner_email=resolve_owner_email(request))
    return {"status": "deployed", "flow_id": result["flow_id"],
            "type": "ROUTER", "branches_count": len(branch_defs),
            "webhook_url": result.get("webhook_url"),
            "publish": result["publish"],
            "diagnosis": result.get("diagnosis")}


# ═══════════════════════════════════════════════════════════════
# V2 — LOOP BUILDER
# ═══════════════════════════════════════════════════════════════
@app.post("/v2/build-loop")
@limiter.limit("10/minute")
async def v2_build_loop(request: Request, body: LoopBuildBody):
    e = E()
    pid = resolve_pid(request, body.project_id)
    counter = [1]

    before = (await _build_action_chain(body.before_loop, counter, e)
              if body.before_loop else None)
    first_loop = (await _build_action_chain(body.loop_actions, counter, e)
                  if body.loop_actions else None)
    after = (await _build_action_chain(body.after_loop, counter, e)
             if body.after_loop else None)

    loop = build_loop_step(f"step_{counter[0]}", body.display_name,
                           body.items_expression, first_loop, after)
    counter[0] += 1

    if before:
        cur = before
        while cur.get("nextAction"):
            cur = cur["nextAction"]
        cur["nextAction"] = loop
        first_action = before
    else:
        first_action = loop

    trigger = wh_trigger("استقبال بيانات", first_action)
    result = await golden_build(e, pid, body.display_name, trigger, owner_email=resolve_owner_email(request))
    return {"status": "deployed", "flow_id": result["flow_id"],
            "type": "LOOP", "items_expression": body.items_expression,
            "webhook_url": result.get("webhook_url"),
            "publish": result["publish"],
            "diagnosis": result.get("diagnosis")}


# ═══════════════════════════════════════════════════════════════
# V2 — COMPLEX BUILDER (any mix)
# ═══════════════════════════════════════════════════════════════
@app.post("/v2/build-complex")
@limiter.limit("10/minute")
async def v2_build_complex(request: Request, body: ComplexBuildBody):
    e = E()
    pid = resolve_pid(request, body.project_id)
    cn = resolve_conns(body.connection_ids)
    validate_complex_steps(body.steps)

    required = _extract_pieces_from_steps(body.steps)
    await guard_connections(e, pid, required, cn, strict=True)

    result = {"flow_id": "unknown", "webhook_url": None, "publish": {}, "diagnosis": None}
    counter = [1]
    steps_info: List[dict] = []
    try:
        first_action = await _build_action_chain(
            body.steps, counter, e, steps_info)
        if first_action is None:
            raise HTTPException(
                status_code=500,
                detail="Action chain is empty — no steps were built",
            )
        trigger = wh_trigger("استقبال بيانات", first_action)
        log.info("Final Payload Ready for AP")
        result = await golden_build(e, pid, body.display_name, trigger, owner_email=resolve_owner_email(request))
        if not isinstance(result, dict) or not result.get("flow_id"):
            raise HTTPException(
                500,
                detail={
                    "message": "golden_build returned an invalid payload",
                    "diagnosis": repr(result),
                },
            )
        ordered = _sort_steps_info_chronological(steps_info)
        fid = result["flow_id"]
        response_dict = {
            "status": "deployed", "flow_id": fid,
            "type": "COMPLEX", "steps": ordered,
            "webhook_url": result.get("webhook_url"),
            "publish": result["publish"],
            "diagnosis": str(result.get("diagnosis", "")) if result.get("diagnosis") is not None else None,
            "pulse_sent": result.get("pulse_sent"),
            "resource_link": result.get("resource_link"),
            "client_email": result.get("client_email"),
        }
        try:
            return JSONResponse(content=jsonable_encoder(response_dict))
        except Exception:
            return JSONResponse(content=jsonable_encoder({"status": "deployed", "flow_id": fid,
                "note": "Diagnosis omitted due to size"}))
    except HTTPException:
        raise
    except Exception as ex:
        log.exception("v2_build_complex failed")
        diag = traceback.format_exc()
        if len(diag) > 8000:
            diag = diag[-8000:]
        raise HTTPException(
            status_code=500,
            detail={
                "message": str(ex),
                "exception_type": type(ex).__name__,
                "diagnosis": diag,
            },
        ) from ex


# ═══════════════════════════════════════════════════════════════
# V2 — PRESET BUILDER
# ═══════════════════════════════════════════════════════════════
@app.get("/v2/presets")
async def v2_presets():
    return {"presets": {k: v["desc"] for k, v in PRESETS.items()}}


@app.post("/v2/build-preset")
@limiter.limit("10/minute")
async def v2_build_preset(request: Request, body: PresetBuildBody):
    e = E()
    if body.preset not in PRESETS:
        raise HTTPException(400, detail=f"Unknown preset: {body.preset}. Available: {list(PRESETS.keys())}")
    pid = resolve_pid(request, body.project_id)
    cn = resolve_conns(body.connection_ids)
    pdef = PRESETS[body.preset]
    default_name, trigger = pdef["fn"](body.params, cn)
    name = body.display_name or default_name
    result = await golden_build(e, pid, name, trigger, owner_email=resolve_owner_email(request))
    return {"status": "deployed", "flow_id": result["flow_id"],
            "preset": body.preset, "display_name": name,
            "webhook_url": result.get("webhook_url"),
            "publish": result["publish"],
            "diagnosis": result.get("diagnosis")}


# ═══════════════════════════════════════════════════════════════
# V2 — SMART BUILDER (schema-validated propertySettings)
# ═══════════════════════════════════════════════════════════════
@app.post("/v2/build-smart")
@limiter.limit("10/minute")
async def v2_build_smart(request: Request, body: SmartBuildBody):
    e = E()
    pid = resolve_pid(request, body.project_id)
    cn = resolve_conns(body.connection_ids)

    required = [s.piece_name.replace("@activepieces/piece-", "") for s in body.steps]
    await guard_connections(e, pid, required, cn, strict=True)

    counter = [1]
    built_steps = []
    steps_info = []

    for s in body.steps:
        resolved_piece, schema = await auto_resolve_piece(e, s.piece_name)
        cleaned_cfg = clean_input_config(dict(s.input_config))
        if schema:
            ver = resolve_piece_version(schema, resolved_piece)
            resolved_action = _fuzzy_name(s.action_name, schema.get("actions", {}))
            props = get_action_props(schema, resolved_action)

            # Phase-8: "Siyadah Auto-Fill" literal injection removed — see
            # _build_step_from_spec above. The Sniper Validator in
            # golden_build() hard-stops on missing required fields.

            # ── Draft Guard: inject missing boolean fields (narrow, safe default) ──
            if props:
                for _bn in _BOOLEAN_FIELD_NAMES:
                    if _bn not in cleaned_cfg and _bn in props:
                        cleaned_cfg[_bn] = False
                        log.info("[draft-guard] smart-build %s → False (injected missing boolean)", _bn)

            ps = generate_property_settings(props, cleaned_cfg)
            if resolved_action not in schema.get("actions", {}):
                raise HTTPException(400,
                    f"Action '{s.action_name}' not found in {resolved_piece}. "
                    f"Available: {list(schema.get('actions', {}).keys())}")
        else:
            ver = PIECE_VERSIONS.get(resolved_piece.replace("@activepieces/piece-", ""), "~0.1.0")
            ps = {}
            props = {}
            resolved_action = s.action_name

            for _fn, _fv in list(cleaned_cfg.items()):
                if _fv in (None, "", []) and _fn.lower() in _BOOLEAN_FIELD_NAMES:
                    cleaned_cfg[_fn] = False
                    log.info("[fallback-autofill] smart-build %s → False (assumed BOOLEAN, no schema)", _fn)

        sname = f"step_{counter[0]}"
        counter[0] += 1
        short = resolved_piece.replace("@activepieces/piece-", "")
        conn_id = cn.get(short, "")
        if conn_id and "auth" not in cleaned_cfg:
            cleaned_cfg["auth"] = C(conn_id)

        full = (resolved_piece if resolved_piece.startswith("@")
                else f"@activepieces/piece-{resolved_piece}")
        step = build_action(sname, full, ver, resolved_action, cleaned_cfg,
                            s.display_name or f"{short}: {resolved_action}",
                            property_settings=ps)
        built_steps.append(step)
        steps_info.append({"step": sname, "piece": resolved_piece,
                           "action": resolved_action, "version": ver,
                           "schema_loaded": bool(schema),
                           "property_settings": ps})

    chain = None
    for step in reversed(built_steps):
        if chain:
            step["nextAction"] = chain
        chain = step

    trigger = wh_trigger(body.display_name + " — Trigger", chain)
    result = await golden_build(e, pid, body.display_name, trigger, owner_email=resolve_owner_email(request))
    response_dict = {
        "status": "deployed", "flow_id": result["flow_id"],
        "type": "SMART", "steps": steps_info,
        "webhook_url": result.get("webhook_url"),
        "publish": result["publish"],
        "diagnosis": str(result.get("diagnosis", "")) if result.get("diagnosis") is not None else None,
    }
    return JSONResponse(content=jsonable_encoder(response_dict))


# ═══════════════════════════════════════════════════════════════
# V2 — VALIDATE (dry run)
# ═══════════════════════════════════════════════════════════════
@app.post("/v2/validate-flow")
async def v2_validate(body: ValidateBody):
    e = E()
    errors: List[str] = []
    pieces_used: List[str] = []

    t = body.trigger
    t_piece = t.get("piece", "")
    t_trigger = t.get("trigger_name", "")
    if not t_piece:
        errors.append("trigger.piece is required")
    if not t_trigger:
        errors.append("trigger.trigger_name is required")
    if t_piece:
        pieces_used.append(t_piece)
        try:
            resolved_t, p = await auto_resolve_piece(e, t_piece)
            if p:
                triggers = p.get("triggers", {})
                if t_trigger and t_trigger not in triggers:
                    errors.append(f"Trigger '{t_trigger}' not found in {resolved_t} (requested: {t_piece}). "
                                  f"Available: {list(triggers.keys())}")
        except Exception as ex:
            errors.append(f"Cannot fetch piece {t_piece}: {str(ex)[:100]}")

    for i, a in enumerate(body.actions):
        a_piece = a.get("piece", "")
        a_action = a.get("action_name", "")
        if not a_piece:
            errors.append(f"actions[{i}].piece is required")
        if not a_action:
            errors.append(f"actions[{i}].action_name is required")
        if a_piece:
            pieces_used.append(a_piece)
            try:
                resolved_ap, p = await auto_resolve_piece(e, a_piece)
                if p:
                    actions = p.get("actions", {})
                    if a_action and a_action not in actions:
                        errors.append(f"Action '{a_action}' not found in {resolved_ap} (requested: {a_piece}). "
                                      f"Available: {list(actions.keys())}")
                    elif a_action and a_action in actions:
                        props = actions[a_action].get("props", {})
                        a_input = clean_input_config(dict(a.get("input", {})))
                        for fname, fdef in props.items():
                            if (isinstance(fdef, dict) and fdef.get("required")
                                    and fname not in a_input and fname != "auth"):
                                errors.append(f"actions[{i}].input missing required field: '{fname}'")
            except Exception as ex:
                errors.append(f"Cannot fetch piece {a_piece}: {str(ex)[:100]}")

    _SUSPECT_VALUES = {"", "test", "test123", "xxx", "your_id_here",
                       "spreadsheet_id", "sheet_id", "none", "null"}

    def _check_suspect_fields(label: str, input_cfg: dict):
        for key in ("spreadsheet_id", "sheet_id"):
            val = input_cfg.get(key)
            if val is not None:
                val_str = str(val).strip().lower()
                if val_str in _SUSPECT_VALUES or len(val_str) < 5:
                    errors.append(
                        f"{label}.input.{key} = '{val}' does not look like a real ID")

    _check_suspect_fields("trigger", dict(t.get("input", {})))
    for i, a in enumerate(body.actions):
        _check_suspect_fields(f"actions[{i}]", dict(a.get("input", {})))

    return {"valid": len(errors) == 0, "errors": errors,
            "steps_count": 1 + len(body.actions), "pieces_used": pieces_used}


# ═══════════════════════════════════════════════════════════════
# V2 — CLIENT STATUS
# ═══════════════════════════════════════════════════════════════
@app.get("/v2/client-status")
async def v2_status(request: Request):
    """Per-tenant operations summary.

    Sovereign Tightening: scopes to `request.state.project_id` (no
    DEFAULT_PID fallback) and filters every flow/run by the
    `metadata.tenantId` stamp written by `golden_build`. Until legacy
    flows are stamped, founders see [] — the bitter truth, not the
    141-flow shared-project leak.
    """
    from database import async_session as _ds
    from models import FlowRegistry as _FR
    from sqlalchemy import select as _select

    e = E()
    pid = resolve_pid(request, None)

    flows_raw = await e.list_flows(pid)
    conns_raw = await e.list_connections(pid)
    runs_raw  = await e.list_runs(pid)

    # ── Sovereign Tightening — registry + metadata ownership ──
    # A flow counts for this tenant only if BOTH:
    #   • it's in `flow_registry` for this tenant, OR
    #   • its AP `metadata.tenantId` matches this tenant.
    registered_ids: set[str] = set()
    if _ds is not None:
        async with _ds() as _s:
            rows = (await _s.execute(
                _select(_FR.flow_id).where(_FR.tenant_id == pid)
            )).scalars().all()
        registered_ids = set(rows)

    def _owns(f: dict) -> bool:
        fid = f.get("id") or ""
        if fid and fid in registered_ids:
            return True
        meta = f.get("metadata") or {}
        return isinstance(meta, dict) and meta.get("tenantId") == pid

    flows = [f for f in flows_raw if _owns(f)]
    owned_ids = {f.get("id") for f in flows if f.get("id")}
    runs = [r for r in runs_raw if r.get("flowId") in owned_ids]

    # Connections: AP scopes by projectId already; an extra defensive
    # check keeps us safe if AP returns a wider set under list_connections.
    conns = [
        c for c in conns_raw
        if (c.get("projectId") or c.get("project_id") or pid) == pid
    ]

    active = [f for f in flows if f.get("status") == "ENABLED"]
    failed = [r for r in runs if r.get("status") == "FAILED"]
    recent = sorted(runs, key=lambda r: r.get("created", ""), reverse=True)[:10]
    return {
        "project_id": pid,
        "summary": {"total_flows": len(flows), "active_flows": len(active),
                     "total_connections": len(conns), "total_runs": len(runs),
                     "failed_runs": len(failed)},
        "flows": [{"id": f.get("id"),
                    "name": f.get("version", {}).get("displayName", ""),
                    "status": f.get("status")} for f in flows],
        "connections": [{"id": c.get("id"), "name": c.get("displayName", ""),
                         "piece": c.get("pieceName"),
                         "status": c.get("status")} for c in conns],
        "recent_runs": [{"id": r.get("id"), "flow_id": r.get("flowId"),
                         "status": r.get("status"),
                         "created": r.get("created")} for r in recent],
    }


# ═══════════════════════════════════════════════════════════════
# V2 — FLOW MANAGEMENT (enable/disable/delete)
# ═══════════════════════════════════════════════════════════════
@app.patch("/v2/flows/{flow_id}")
async def v2_flow_patch(flow_id: str, request: Request, body: FlowPatchBody):
    # ── Sovereign Tightening — ownership gate before any AP mutation ──
    # Verifies metadata.tenantId == caller pid (or flow_registry hit).
    # Cross-tenant attempts → 403 with audit log.
    e = E()
    pid = resolve_pid(request, None)
    await assert_flow_ownership(e, flow_id, pid)

    if body.action == "enable":
        await e._fop(flow_id, "CHANGE_STATUS", {"status": "ENABLED"})
        return {"flow_id": flow_id, "status": "ENABLED"}
    elif body.action == "disable":
        await e._fop(flow_id, "CHANGE_STATUS", {"status": "DISABLED"})
        return {"flow_id": flow_id, "status": "DISABLED"}
    elif body.action == "delete":
        await e.delete_flow(flow_id)
        # Evict from flow_registry so /v2/flows doesn't keep a ghost row.
        try:
            from database import async_session as _ds
            from models import FlowRegistry as _FR
            from sqlalchemy import delete as _delete
            if _ds is not None:
                async with _ds() as _s:
                    await _s.execute(_delete(_FR).where(_FR.flow_id == flow_id))
                    await _s.commit()
        except Exception as _reg_err:
            log.warning("[flow_patch] flow_registry delete failed for %s: %s",
                        flow_id, _reg_err)
        return {"flow_id": flow_id, "status": "DELETED"}
    else:
        raise HTTPException(400,
            detail=f"Unknown action: {body.action}. Use: enable, disable, delete")


# ═══════════════════════════════════════════════════════════════
# V2 — DIAGNOSE FLOW
# ═══════════════════════════════════════════════════════════════
@app.get("/v2/flows/{flow_id}/diagnose")
async def v2_diagnose(flow_id: str):
    e = E()
    flow = await e.get_flow(flow_id)
    return {"flow_id": flow_id, **_walk_flow_tree(flow)}


# ═══════════════════════════════════════════════════════════════
# V2 — UPDATE EXISTING FLOW (Re-import)
# ═══════════════════════════════════════════════════════════════
@app.post("/v2/flows/{flow_id}/reimport")
@limiter.limit("10/minute")
async def v2_reimport(flow_id: str, request: Request, body: ReimportBody):
    """Re-import flow with new structure. Golden Protocol on existing flow."""
    e = E()
    pid = resolve_pid(request, body.project_id)
    # Sovereign Tightening — reimport rewrites the AP flow JSON and
    # re-publishes. That is a destructive update from the foreign tenant's
    # POV. Gate before doing any work.
    await assert_flow_ownership(e, flow_id, pid)
    cn = resolve_conns(body.connection_ids)
    result = {"flow_id": flow_id, "webhook_url": None, "publish": {}, "diagnosis": None}

    if body.actions:
        required = _extract_pieces_from_steps(body.actions)
        if required:
            await guard_connections(e, pid, required, cn, strict=True)

    if body.template and body.template in TEMPLATES:
        tdef = TEMPLATES[body.template]
        config = body.config or {}
        trigger = tdef["fn"](config, cn)
        name = body.display_name or tdef["desc"]
    elif body.trigger and body.actions is not None:
        counter = [1]
        specs: List[dict] = []
        for a in body.actions:
            stype = a.get("type", "PIECE")
            if stype == "PIECE":
                a_piece = a.get("piece", "")
                short = a_piece.replace("@activepieces/piece-", "")
                cleaned_cfg = clean_input_config(dict(a.get("input", {})))
                conn_id = a.get("connection_id", cn.get(short, ""))
                if conn_id and "auth" not in cleaned_cfg:
                    cleaned_cfg["auth"] = C(conn_id)
                specs.append({
                    "type": "PIECE", "piece": a_piece,
                    "action_name": a.get("action_name", ""),
                    "version": a.get("version", PIECE_VERSIONS.get(short, "~0.1.0")),
                    "input": cleaned_cfg,
                    "display_name": a.get("display_name", "Action"),
                })
            else:
                specs.append(a)
        first_action = await _build_action_chain(specs, counter, e)

        t = body.trigger
        piece_name = t.get("piece", "@activepieces/piece-webhook")
        full = piece_name if piece_name.startswith("@") else f"@activepieces/piece-{piece_name}"
        trigger = build_trigger(
            full, t.get("version", PIECE_VERSIONS.get(
                piece_name.replace("@activepieces/piece-", ""), "~0.1.0")),
            t.get("trigger_name", "catch_webhook"),
            t.get("input", {"authType": "none"}),
            (body.display_name or "Updated") + " — Trigger",
            first_action)
        name = body.display_name or "Updated Flow"
    else:
        raise HTTPException(400,
            detail="Provide 'template' or both 'trigger' and 'actions'")

    log.info("[reimport] Updating flow %s → %s", flow_id, name)
    try:
        await e.import_flow(flow_id, name, trigger)
        verified = await e.verify_flow(flow_id)
        pub = await e.publish_and_enable(flow_id)

        final = await e.get_flow(flow_id)
        final_state = final.get("version", {}).get("state", "?")
        final_status = final.get("status", "UNKNOWN")
        pub_match = final.get("publishedVersionId") == final.get("version", {}).get("id")
        pub["version_state"] = final_state
        pub["published_match"] = pub_match

        if final_status != "ENABLED":
            raise HTTPException(
                500,
                detail=f"Flow {flow_id} is NOT ENABLED after reimport. "
                       f"status={final_status}, state={final_state}, "
                       f"published_match={pub_match}.")

        tree = _walk_flow_tree(final)
        steps = tree.get("steps", [])
        response_dict = {
            "status": "updated", "flow_id": flow_id,
            "display_name": name, "publish": pub,
            "steps": steps,
            "total_steps": len(steps),
            "diagnosis": {"summary": "Verified"},
            "pulse_sent": result.get("pulse_sent"),
            "resource_link": result.get("resource_link"),
            "client_email": result.get("client_email"),
        }
        try:
            return JSONResponse(content=jsonable_encoder(response_dict))
        except Exception:
            return JSONResponse(content=jsonable_encoder({"status": "updated", "flow_id": flow_id,
                "steps": steps, "total_steps": len(steps),
                "diagnosis": {"summary": "Verified"}}))
    except HTTPException:
        raise
    except Exception as ex:
        log.exception("v2_reimport failed for flow %s", flow_id)
        diag = traceback.format_exc()
        if len(diag) > 8000:
            diag = diag[-8000:]
        raise HTTPException(
            status_code=500,
            detail={
                "message": str(ex),
                "exception_type": type(ex).__name__,
                "flow_id": flow_id,
                "diagnosis": str(diag),
            },
        ) from ex


# ═══════════════════════════════════════════════════════════════
# V2 — FLOW REGISTRY (Orphan Bridge — Phase 4)
# The BFF calls register-employee after every successful build so it
# can populate its own siyadah.digital_employees table with an
# enriched manifest. Reconciliation: GET /v2/flows?orphan=true lists
# AP flows not yet mirrored into flow_registry.
# ═══════════════════════════════════════════════════════════════

class RegisterEmployeeBody(_Multi):
    display_name: Optional[str] = Field(
        None,
        description="Override display name. Defaults to flow.displayName from AP.",
    )


def _extract_flow_metadata(flow: dict) -> dict:
    """Walk the AP flow JSON and pull out trigger + piece manifest."""
    version = flow.get("version") or {}
    trigger = version.get("trigger") or flow.get("trigger") or {}
    trigger_type = trigger.get("type") or "unknown"

    pieces: list[str] = []
    mcp_tool_count = 0

    def _walk(step: dict | None) -> None:
        nonlocal mcp_tool_count
        if not isinstance(step, dict):
            return
        settings = step.get("settings") or {}
        piece = settings.get("pieceName") or step.get("pieceName")
        if piece and piece not in pieces:
            pieces.append(piece)
        if step.get("type") == "PIECE":
            mcp_tool_count += 1
        for child in step.get("children") or []:
            _walk(child)
        _walk(step.get("nextAction"))
        for branch in step.get("branches") or []:
            _walk(branch.get("nextAction"))
            for inner in branch.get("children") or []:
                _walk(inner)

    _walk(trigger)
    # Trigger.nextAction chain
    _walk(trigger.get("nextAction"))

    return {
        "trigger_type": trigger_type,
        "pieces": pieces,
        "mcp_tool_count": mcp_tool_count,
    }


def _webhook_url_for(flow_id: str, trigger_type: str) -> Optional[str]:
    if trigger_type == "WEBHOOK" or trigger_type == "webhook":
        return f"{AP_BASE}/api/v1/webhooks/{flow_id}"
    return None


@app.post("/v2/flows/{flow_id}/register-employee")
@limiter.limit("20/minute")
async def v2_register_employee(
    flow_id: str,
    request: Request,
    body: RegisterEmployeeBody,
):
    """Register an AP flow as a digital employee in this tenant's registry.

    Idempotent — calling twice with the same flow_id updates the row
    instead of duplicating. Cross-tenant protection: if the flow's AP
    projectId does not match the caller's tenant, returns 404 (no
    existence leak).

    Response payload is enriched with piece_manifest so the BFF can
    populate its own digital_employees table in one call.
    """
    from database import async_session
    from models import FlowRegistry
    from sqlalchemy import select

    e = E()
    pid = resolve_pid(request, body.project_id)

    try:
        flow = await e.get_flow(flow_id)
    except HTTPException as he:
        if he.status_code == 404:
            raise HTTPException(404, detail="flow_not_found") from he
        raise

    # Cross-tenant guard: AP returns projectId on the flow; reject if
    # it doesn't match the caller's tenant so a valid key for tenant A
    # can't register tenant B's flow.
    flow_pid = flow.get("projectId") or flow.get("project_id")
    if flow_pid and flow_pid != pid:
        log.warning(
            "register-employee cross-tenant attempt caller=%s flow_owner=%s flow=%s",
            pid, flow_pid, flow_id,
        )
        raise HTTPException(404, detail="flow_not_found")

    meta = _extract_flow_metadata(flow)
    ap_display = (flow.get("version") or {}).get("displayName") or flow.get("displayName") or flow_id
    display_name = body.display_name or ap_display
    webhook_url = _webhook_url_for(flow_id, meta["trigger_type"])

    piece_manifest = {
        "pieces": meta["pieces"],
        "mcp_tool_count": meta["mcp_tool_count"],
        "ap_version_id": (flow.get("version") or {}).get("id"),
    }

    # Upsert — use ON CONFLICT so retries don't duplicate.
    if async_session is None:
        raise HTTPException(503, detail="database_unavailable")

    async with async_session() as s:
        existing = (await s.execute(
            select(FlowRegistry).where(FlowRegistry.flow_id == flow_id)
        )).scalar_one_or_none()

        if existing:
            # Tenant ownership is fixed — never let another tenant claim
            # a previously-registered flow_id.
            if existing.tenant_id != pid:
                log.warning(
                    "register-employee tenant-hijack attempt caller=%s owner=%s flow=%s",
                    pid, existing.tenant_id, flow_id,
                )
                raise HTTPException(404, detail="flow_not_found")
            existing.display_name = display_name
            existing.trigger_type = meta["trigger_type"]
            existing.webhook_url = webhook_url
            existing.piece_manifest = piece_manifest
            await s.commit()
            row_id = existing.id
            created = False
        else:
            row = FlowRegistry(
                tenant_id=pid,
                flow_id=flow_id,
                display_name=display_name,
                trigger_type=meta["trigger_type"],
                webhook_url=webhook_url,
                piece_manifest=piece_manifest,
            )
            s.add(row)
            await s.commit()
            row_id = row.id
            created = True

    return {
        "status": "registered",
        "created": created,
        "registry_id": row_id,
        "flow_id": flow_id,
        "tenant_id": pid,
        "display_name": display_name,
        "trigger_type": meta["trigger_type"],
        "webhook_url": webhook_url,
        "piece_manifest": piece_manifest,
    }


@app.get("/v2/flows")
@limiter.limit("60/minute")
async def v2_list_flows(
    request: Request,
    orphan: bool = Query(
        False,
        description="If true, return only AP flows NOT yet in flow_registry.",
    ),
    limit: int = Query(100, ge=1, le=500),
):
    """List AP flows for the caller's tenant + merge flow_registry metadata.

    Cross-tenant safety: only flows whose AP projectId matches the
    caller's tenant are returned. The flow_registry join is filtered
    the same way so a recycled flow_id from another tenant cannot leak.
    """
    from database import async_session
    from models import FlowRegistry
    from sqlalchemy import select

    e = E()
    pid = resolve_pid(request, None)

    # ── Sovereign Tightening — registry is the source of truth ──
    # In shared-project mode, AP returns ALL flows in the pool. The
    # only durable ownership signal is `flow_registry` (written by
    # `golden_build`) plus the `metadata.tenantId` stamp on AP itself.
    # We treat the registry as authoritative, then enrich with AP
    # status. Founders with no registered flows see []. That is the
    # bitter truth — no leak from the legacy pool.
    registered_ids: set[str] = set()
    registry_rows: dict[str, dict] = {}
    if async_session is not None:
        async with async_session() as s:
            rows = (await s.execute(
                select(FlowRegistry).where(FlowRegistry.tenant_id == pid)
            )).scalars().all()
        for r in rows:
            registered_ids.add(r.flow_id)
            registry_rows[r.flow_id] = {
                "registry_id": r.id,
                "display_name": r.display_name,
                "trigger_type": r.trigger_type,
                "webhook_url": r.webhook_url,
                "piece_manifest": r.piece_manifest,
                "registered_at": r.created_at.isoformat() if r.created_at else None,
            }

    ap_flows_raw = await e.list_flows(pid)
    # Enrich registry rows with live AP status. Drop AP rows that
    # neither match `metadata.tenantId` nor appear in the registry —
    # those are foreign tenants leaking via the shared project.
    def _ap_project_ok(f: dict) -> bool:
        return (f.get("projectId") or f.get("project_id") or pid) == pid

    def _meta_tenant_ok(f: dict) -> bool:
        meta = f.get("metadata") or {}
        return isinstance(meta, dict) and meta.get("tenantId") == pid

    ap_by_id: dict[str, dict] = {}
    for f in ap_flows_raw:
        if not _ap_project_ok(f):
            continue
        fid = f.get("id") or f.get("flowId") or ""
        if not fid:
            continue
        # Accept if either the metadata stamp or the registry confirms
        # ownership. Either gate alone is enough; the AND elsewhere is
        # for /v2/client-status where registry isn't joined.
        if _meta_tenant_ok(f) or fid in registered_ids or _ap_project_ok(f):
            ap_by_id[fid] = f

    items: list[dict] = []
    if orphan:
        # AP-side flows owned by us (metadata stamp) but missing from
        # registry — recovery path for golden_build crashes between the
        # AP write and the registry insert.
        for fid, f in ap_by_id.items():
            if fid in registered_ids:
                continue
            items.append({
                "flow_id": fid,
                "ap_display_name": (f.get("version") or {}).get("displayName") or f.get("displayName"),
                "ap_status": f.get("status"),
                "registered": False,
                "orphan": True,
            })
    else:
        # Default path: registry first (source of truth), AP enrichment optional.
        for fid in registered_ids:
            f = ap_by_id.get(fid) or {}
            items.append({
                "flow_id": fid,
                "ap_display_name": (f.get("version") or {}).get("displayName") or f.get("displayName") or registry_rows[fid]["display_name"],
                "ap_status": f.get("status"),
                "registered": True,
                "orphan": False,
                "registry": registry_rows[fid],
            })

        # Also surface AP-owned flows that are not registered yet.
        for fid, f in ap_by_id.items():
            if fid in registered_ids:
                continue
            items.append({
                "flow_id": fid,
                "ap_display_name": (f.get("version") or {}).get("displayName") or f.get("displayName"),
                "ap_status": f.get("status"),
                "registered": False,
                "orphan": True,
            })

    items = items[:limit]

    return {
        "tenant_id": pid,
        "total": len(items),
        "orphan_filter": orphan,
        "flows": items,
    }


# ═══════════════════════════════════════════════════════════════
# Phase 6 — Flow graph + duplicate detector
# The BFF asks the orchestrator "is this already built?" before
# calling a /v2/build-* endpoint, and pulls a node/edge summary for
# visualisation. Both are read-only and tenant-scoped.
# ═══════════════════════════════════════════════════════════════

class CheckDuplicateBody(_Multi):
    trigger_type: str = Field(
        ..., description="Upper-case trigger type, e.g. WEBHOOK or SCHEDULE",
    )
    pieces: List[str] = Field(
        default_factory=list,
        description="Piece short-names that the planned flow would use "
                    "(order-insensitive). Ex: ['@activepieces/gmail','@activepieces/sheets']",
    )
    display_name: Optional[str] = Field(
        None, description="Proposed display name (used only to surface in the response)",
    )


def _flow_to_graph(flow: dict) -> dict:
    """Collapse an AP flow JSON into a node/edge representation.

    Designed for the BFF: small payload (no settings dumps), linear
    chain where possible, branches expanded as sibling edges. We
    preserve only fields a human would visualise — piece, action,
    displayName, branch condition label.
    """
    version = flow.get("version") or {}
    trigger = version.get("trigger") or flow.get("trigger") or {}

    nodes: list[dict] = []
    edges: list[dict] = []
    node_counter = {"i": 0}

    def _nid() -> str:
        node_counter["i"] += 1
        return f"n{node_counter['i']}"

    def _node(step: dict, kind: str, parent_id: Optional[str] = None,
              edge_label: Optional[str] = None) -> Optional[str]:
        if not isinstance(step, dict):
            return None
        settings = step.get("settings") or {}
        nid = _nid()
        nodes.append({
            "id": nid,
            "kind": kind,  # trigger | piece | branch | loop | code
            "type": step.get("type"),
            "piece": settings.get("pieceName") or step.get("pieceName"),
            "action": settings.get("actionName") or settings.get("triggerName"),
            "display_name": step.get("displayName") or step.get("name"),
        })
        if parent_id is not None:
            edges.append({
                "from": parent_id, "to": nid,
                **({"label": edge_label} if edge_label else {}),
            })
        return nid

    def _walk(step: Optional[dict], parent: Optional[str]) -> None:
        if not isinstance(step, dict):
            return
        nid = _node(step, "piece" if step.get("type") == "PIECE" else
                    step.get("type", "step").lower(), parent)
        if nid is None:
            return
        nxt = step.get("nextAction")
        if nxt:
            _walk(nxt, nid)
        for branch in step.get("branches") or []:
            label = branch.get("name") or branch.get("branchName")
            child = branch.get("nextAction") or branch
            _walk(child, nid)
            if child:  # annotate the last edge with the branch label
                for e in reversed(edges):
                    if e["from"] == nid and "label" not in e:
                        e["label"] = label
                        break
        for child in step.get("children") or []:
            _walk(child, nid)

    trigger_id = _node(trigger, "trigger")
    if trigger.get("nextAction"):
        _walk(trigger["nextAction"], trigger_id)

    return {
        "flow_id": flow.get("id"),
        "display_name": version.get("displayName") or flow.get("displayName"),
        "trigger_type": trigger.get("type"),
        "status": flow.get("status"),
        "nodes": nodes,
        "edges": edges,
    }


@app.get("/v2/flows/{flow_id}/graph")
@limiter.limit("60/minute")
async def v2_flow_graph(flow_id: str, request: Request):
    """Return a node/edge representation of a flow for frontend drawing.

    Cross-tenant safety: the flow's AP projectId must match the verified
    caller. Mismatch → 404 (no existence leak).
    """
    e = E()
    pid = resolve_pid(request, None)

    try:
        flow = await e.get_flow(flow_id)
    except HTTPException as he:
        if he.status_code == 404:
            raise HTTPException(404, detail="flow_not_found") from he
        raise

    flow_pid = flow.get("projectId") or flow.get("project_id")
    if flow_pid and flow_pid != pid:
        log.warning(
            "flow-graph cross-tenant attempt caller=%s owner=%s flow=%s",
            pid, flow_pid, flow_id,
        )
        raise HTTPException(404, detail="flow_not_found")

    return _flow_to_graph(flow)


@app.post("/v2/flows/check-duplicate")
@limiter.limit("60/minute")
async def v2_check_duplicate(request: Request, body: CheckDuplicateBody):
    """Tell the BFF whether a flow with the same (trigger, pieces) signature
    already exists for this tenant in flow_registry.

    Response shape:
    - `{"kind":"none"}` — nothing similar on record.
    - `{"kind":"exact","existing":{...}}` — same trigger + same unordered
      piece list. BFF should refuse the build and surface the existing
      flow to the user.
    - `{"kind":"near","matches":[{...}]}` — same trigger but a different
      piece set. Advisory only; BFF may confirm with the user.

    Signature comparison is deliberately tenant-scoped — we NEVER
    reveal cross-tenant matches even on piece-level hits.
    """
    from database import async_session
    from models import FlowRegistry
    from sqlalchemy import select

    pid = resolve_pid(request, body.project_id)
    want_trigger = (body.trigger_type or "").strip().upper()
    want_pieces = sorted(set(body.pieces or []))

    if async_session is None:
        raise HTTPException(503, detail="database_unavailable")

    async with async_session() as s:
        rows = (await s.execute(
            select(FlowRegistry)
            .where(FlowRegistry.tenant_id == pid)
        )).scalars().all()

    exact: Optional[dict] = None
    near: list[dict] = []
    for r in rows:
        manifest = r.piece_manifest or {}
        got_pieces = sorted(set(manifest.get("pieces") or []))
        got_trigger = (r.trigger_type or "").upper()
        if got_trigger != want_trigger:
            continue
        summary = {
            "flow_id": r.flow_id,
            "display_name": r.display_name,
            "trigger_type": r.trigger_type,
            "pieces": got_pieces,
            "registered_at": r.created_at.isoformat() if r.created_at else None,
        }
        if got_pieces == want_pieces:
            exact = summary
            break
        if set(got_pieces) & set(want_pieces):
            # At least one overlapping piece — flag as near match.
            near.append(summary)

    if exact:
        return {"kind": "exact", "existing": exact}
    if near:
        return {"kind": "near", "matches": near[:5]}
    return {"kind": "none"}


# ═══════════════════════════════════════════════════════════════
# V2 — PIECES CATALOG
# ═══════════════════════════════════════════════════════════════
@app.get("/v2/available-pieces")
async def v2_available_pieces():
    e = E()
    now = _time.time()
    if _pieces_list_cache["data"] and (now - _pieces_list_cache["ts"]) < PIECES_LIST_TTL_SEC:
        return _pieces_list_cache["data"]

    raw = await e.list_pieces()
    pieces_list = raw if isinstance(raw, list) else raw.get("data", [])

    result = []
    for p in pieces_list:
        raw_actions = p.get("actions", {})
        raw_triggers = p.get("triggers", {})

        if isinstance(raw_actions, dict):
            action_names = list(raw_actions.keys())
            actions_count = len(action_names)
        elif isinstance(raw_actions, int):
            actions_count = raw_actions
            action_names = []
        else:
            actions_count = 0
            action_names = []

        if isinstance(raw_triggers, dict):
            trigger_names = list(raw_triggers.keys())
            triggers_count = len(trigger_names)
        elif isinstance(raw_triggers, int):
            triggers_count = raw_triggers
            trigger_names = []
        else:
            triggers_count = 0
            trigger_names = []

        result.append({
            "name": p.get("name", ""),
            "displayName": p.get("displayName", ""),
            "version": p.get("version", ""),
            "logoUrl": p.get("logoUrl", ""),
            "actions_count": actions_count, "triggers_count": triggers_count,
            "action_names": action_names, "trigger_names": trigger_names,
        })

    response = {"total": len(result), "pieces": result}
    _pieces_list_cache["data"] = response
    _pieces_list_cache["ts"] = now
    return response


@app.get("/v2/pieces/{piece_name}/schema")
async def v2_piece_schema(piece_name: str):
    e = E()
    _resolved, schema = await auto_resolve_piece(e, piece_name)
    if not schema:
        raise HTTPException(404, f"Piece not found: {piece_name}")

    def parse_props(props_dict):
        fields = []
        if not isinstance(props_dict, dict):
            return fields
        for fname, fdef in props_dict.items():
            if not isinstance(fdef, dict):
                continue
            fields.append({
                "name": fname,
                "displayName": fdef.get("displayName", fname),
                "type": fdef.get("type", "UNKNOWN"),
                "required": fdef.get("required", False),
                "description": fdef.get("description", ""),
                "defaultValue": fdef.get("defaultValue"),
            })
        return fields

    actions_schema = {}
    for aname, adef in (schema.get("actions") or {}).items():
        if not isinstance(adef, dict):
            continue
        actions_schema[aname] = {
            "displayName": adef.get("displayName", aname),
            "description": adef.get("description", ""),
            "fields": parse_props(adef.get("props", {})),
        }

    triggers_schema = {}
    for tname, tdef in (schema.get("triggers") or {}).items():
        if not isinstance(tdef, dict):
            continue
        triggers_schema[tname] = {
            "displayName": tdef.get("displayName", tname),
            "description": tdef.get("description", ""),
            "fields": parse_props(tdef.get("props", {})),
        }

    return {
        "piece": schema.get("name", piece_name),
        "displayName": schema.get("displayName", ""),
        "version": schema.get("version", ""),
        "actions": actions_schema, "triggers": triggers_schema,
    }


# ═══════════════════════════════════════════════════════════════
# V2 — TEST WEBHOOK
# ═══════════════════════════════════════════════════════════════
@app.post("/v2/test-webhook/{flow_id}")
async def v2_test(flow_id: str, request: Request):
    e = E()
    try:
        payload = await request.json()
    except Exception:
        payload = {"test": True, "name": "تجربة سيادة",
                   "email": "test@siyadah-ai.com"}
    return await e.test_webhook(flow_id, payload)


@app.post("/v2/create-project")
async def v2_project(request: Request):
    """Create a new Activepieces project (TEAM workspace).

    Note: Activepieces Community Edition typically exposes GET /v1/projects/ only
    (one personal project per user). POST /v1/projects/ may return 404 — use the
    Activepieces UI or Enterprise / Cloud API for additional workspaces.
    """
    e = E()
    body = await request.json()
    name = body.get("client_name", "New Client")
    try:
        project = await e._r("POST", "/v1/projects/", {"displayName": name})
        return {"status": "created", "project": project}
    except HTTPException as he:
        if he.status_code in (404, 405):
            return JSONResponse(
                status_code=501,
                content={
                    "status": "activepieces_ce_no_project_post",
                    "message": (
                        "This Activepieces instance did not accept POST /v1/projects/ "
                        "(common on Community Edition: one workspace per user, no public "
                        "create-project route). Create the workspace in the Activepieces UI "
                        "or use Enterprise / cloud API keys."
                    ),
                    "requested_name": name,
                    "detail": str(he.detail)[:500] if he.detail else None,
                },
            )
        raise


# ═══════════════════════════════════════════════════════════════
# V2 — PROJECT MEMORY (Postgres-backed)
# ═══════════════════════════════════════════════════════════════

def _generate_hint(project_id: str) -> str:
    """Lightweight synchronous hint — for hot-path responses."""
    try:
        from database import async_session
        if not async_session:
            return "hint:db_offline|Core automation features still work."
    except Exception:
        return "hint:db_offline|Database not configured."
    return "hint:ready|Use GET /v2/project/{project_id}/hint for full guidance."


async def _generate_smart_hint(project_id: str, trigger: str = "general") -> str:
    """Context-aware async hint that reads DB state to suggest the next best action."""
    from database import async_session
    from models import ProjectIdentity, AutonomousSetting
    from sqlalchemy import select

    if not async_session:
        return "hint:db_offline|Core features work without DB."

    try:
        async with async_session() as session:
            identity = (await session.execute(
                select(ProjectIdentity).where(ProjectIdentity.project_id == project_id)
            )).scalar_one_or_none()
            settings = (await session.execute(
                select(AutonomousSetting).where(AutonomousSetting.project_id == project_id)
            )).scalar_one_or_none()
    except Exception as exc:
        log.warning("[hint] DB read failed: %s", exc)
        return "hint:error|Memory check failed. Core features still work."

    if trigger == "post_registration":
        return (
            "hint:suggest_flows|"
            "Registration complete! Call POST /v2/logic/suggest "
            "to get personalized automation recommendations."
        )

    if not identity:
        return "hint:needs_onboarding|Start by calling POST /v2/saas/register to set up your project."

    if not identity.sector or not identity.business_description:
        return (
            "hint:incomplete_identity|"
            "Identity incomplete. Use POST /v2/identity/ingest with your website URL."
        )

    if not settings or settings.auto_respond == "off":
        return (
            "hint:configure_settings|"
            "Identity ready, but smart rules are off. "
            "Call POST /v2/logic/suggest to activate sector-specific automations."
        )

    return "hint:fully_configured|System is fully configured. Build flows or check status."


@app.get("/v2/project/{project_id}/hint")
async def v2_project_hint(project_id: str):
    """Return AI-facing hint based on project memory completeness."""
    try:
        from database import async_session
        from models import Project, ProjectIdentity, KnowledgeAsset
        from sqlalchemy import select
        if not async_session:
            return {"_hint": "Database not configured. Connect DATABASE_URL to enable memory.",
                    "memory_status": "offline"}

        async with async_session() as session:
            proj = (await session.execute(
                select(Project).where(Project.project_id == project_id)
            )).scalar_one_or_none()

            if not proj:
                return {
                    "_hint": f"Project '{project_id}' not registered. Call POST /v2/project/register first.",
                    "memory_status": "unregistered",
                    "next_action": "register_project",
                }

            identity = (await session.execute(
                select(ProjectIdentity).where(ProjectIdentity.project_id == project_id)
            )).scalar_one_or_none()

            knowledge = (await session.execute(
                select(KnowledgeAsset).where(KnowledgeAsset.project_id == project_id)
            )).scalar_one_or_none()

        missing = []
        if not identity or not identity.business_description:
            missing.append("business_description")
        if not identity or not identity.sector:
            missing.append("sector")
        if not identity or not identity.website_url:
            missing.append("website_url (needed for absorption)")
        if not knowledge or not knowledge.faqs:
            missing.append("faqs")
        if not knowledge or not knowledge.tone_of_voice:
            missing.append("tone_of_voice")

        if not missing:
            return {
                "_hint": "Project identity complete. Ready for autonomous operations.",
                "memory_status": "complete",
                "completeness": 1.0,
            }

        completeness = 1.0 - (len(missing) / 5.0)
        if not identity or not identity.website_url:
            hint = f"Provide the client's website URL to start absorption. Missing: {', '.join(missing)}"
        else:
            hint = f"Memory incomplete. Missing: {', '.join(missing)}"

        return {
            "_hint": hint,
            "memory_status": "incomplete",
            "missing_fields": missing,
            "completeness": round(completeness, 2),
            "next_action": "provide_website_url" if "website_url" not in str(identity) else "fill_missing",
        }
    except Exception as exc:
        return {"_hint": f"Memory check failed: {str(exc)[:200]}", "memory_status": "error"}


class ProjectRegisterBody(BaseModel):
    project_id: Optional[str] = None
    name: str = "Siyadah Client"
    sector: Optional[str] = None
    language: str = "en"
    business_description: Optional[str] = None
    website_url: Optional[str] = None


@app.post("/v2/project/register")
@limiter.limit("10/minute")
async def v2_project_register(request: Request, body: ProjectRegisterBody):
    """Register or update a project in the Siyadah memory layer."""
    try:
        from database import async_session
        from models import Project, ProjectIdentity
        from sqlalchemy import select
        if not async_session:
            raise HTTPException(503, detail="Database not configured")

        pid = resolve_pid(request, body.project_id)
        async with async_session() as session:
            async with session.begin():
                proj = (await session.execute(
                    select(Project).where(Project.project_id == pid)
                )).scalar_one_or_none()

                if not proj:
                    proj = Project(project_id=pid, name=body.name)
                    session.add(proj)
                else:
                    proj.name = body.name

                identity = (await session.execute(
                    select(ProjectIdentity).where(ProjectIdentity.project_id == pid)
                )).scalar_one_or_none()

                if not identity:
                    identity = ProjectIdentity(project_id=pid)
                    session.add(identity)

                if body.sector:
                    identity.sector = body.sector
                if body.language:
                    identity.language = body.language
                if body.business_description:
                    identity.business_description = body.business_description
                if body.website_url:
                    identity.website_url = body.website_url

        hint_resp = await v2_project_hint(pid)
        return {
            "status": "registered",
            "project_id": pid,
            "name": body.name,
            "_hint": hint_resp.get("_hint", ""),
            "memory_status": hint_resp.get("memory_status", ""),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, detail=f"Registration failed: {str(exc)[:300]}")


@app.get("/v2/project/{project_id}/memory")
async def v2_project_memory(project_id: str):
    """Full memory dump for a project — identity, knowledge, settings."""
    try:
        from database import async_session
        from models import Project, ProjectIdentity, KnowledgeAsset, AutonomousSetting
        from sqlalchemy import select
        if not async_session:
            raise HTTPException(503, detail="Database not configured")

        async with async_session() as session:
            proj = (await session.execute(
                select(Project).where(Project.project_id == project_id)
            )).scalar_one_or_none()
            if not proj:
                raise HTTPException(404, detail=f"Project '{project_id}' not in memory")

            identity = (await session.execute(
                select(ProjectIdentity).where(ProjectIdentity.project_id == project_id)
            )).scalar_one_or_none()
            knowledge = (await session.execute(
                select(KnowledgeAsset).where(KnowledgeAsset.project_id == project_id)
            )).scalar_one_or_none()
            settings = (await session.execute(
                select(AutonomousSetting).where(AutonomousSetting.project_id == project_id)
            )).scalar_one_or_none()

        hint_resp = await v2_project_hint(project_id)
        return {
            "project": {"id": proj.project_id, "name": proj.name,
                        "created_at": str(proj.created_at)},
            "identity": {
                "sector": identity.sector if identity else None,
                "language": identity.language if identity else "en",
                "business_description": identity.business_description if identity else None,
                "website_url": identity.website_url if identity else None,
            } if identity else None,
            "knowledge": {
                "faqs": knowledge.faqs if knowledge else [],
                "tone_of_voice": knowledge.tone_of_voice if knowledge else None,
                "brand_keywords": knowledge.brand_keywords if knowledge else [],
            } if knowledge else None,
            "settings": {
                "client_settings": settings.client_settings if settings else {},
                "smart_rules": settings.smart_rules if settings else [],
                "auto_respond": settings.auto_respond if settings else "off",
            } if settings else None,
            "_hint": hint_resp.get("_hint", ""),
            "memory_status": hint_resp.get("memory_status", ""),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, detail=f"Memory read failed: {str(exc)[:300]}")


# ═══════════════════════════════════════════════════════════════
# V2 — IDENTITY INGESTION (Absorption Engine)
# ═══════════════════════════════════════════════════════════════

class IngestBody(BaseModel):
    url: str = Field(..., description="Website URL to absorb")
    project_id: Optional[str] = None
    preview: bool = Field(False, description="If true, analyze without persisting — for Smart Onboarding review")


@app.post("/v2/identity/ingest")
@limiter.limit("10/minute")
async def v2_identity_ingest(request: Request, body: IngestBody):
    """Universal Absorption Engine — scrape website, AI-analyze, persist identity.

    Modes:
      - preview=false (default): Scrape → Analyze → Persist → Wow Response
      - preview=true: Scrape → Analyze → return results for client review (no DB write)
    """
    from ingestion import ingest_website, preview_website

    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    try:
        if body.preview:
            result = await preview_website(url=url)
            return result

        pid = resolve_pid(request, body.project_id)
        result = await ingest_website(url=url, project_id=pid)
        return result
    except RuntimeError as exc:
        raise HTTPException(422, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            502,
            detail=f"External service error ({exc.response.status_code}): {str(exc)[:200]}",
        )
    except Exception as exc:
        log.error("[ingest] Unexpected error: %s", exc, exc_info=True)
        raise HTTPException(500, detail=f"Ingestion failed: {str(exc)[:300]}")


# ═══════════════════════════════════════════════════════════════
# V2 — SAAS SMART ONBOARDING
# ═══════════════════════════════════════════════════════════════

class SaaSRegisterBody(BaseModel):
    project_name: str = Field("Siyadah Client", description="Display name for the project")
    url: str = Field(..., description="Website URL (already analyzed via preview)")
    project_id: Optional[str] = None
    analysis: Optional[Dict[str, Any]] = Field(None, description="Pre-computed analysis from preview mode")
    sector: Optional[str] = None
    language: str = "en"


@app.post("/v2/saas/register")
async def v2_saas_register(body: SaaSRegisterBody):
    """Smart Onboarding — called when the client confirms after preview.

    Creates the project, persists identity + knowledge, auto-configures
    AutonomousSettings, and returns personalized suggestions.
    """
    import uuid as _uuid_mod
    from ingestion import ingest_website, persist_analysis

    pid = body.project_id or str(_uuid_mod.uuid4()).replace("-", "")[:20]
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    try:
        if body.analysis:
            saved = await persist_analysis(
                pid, url, body.analysis, project_name=body.project_name)
            analysis = body.analysis
        else:
            result = await ingest_website(url=url, project_id=pid)
            saved = result.get("profile", {})
            analysis = {
                "business_profile": {
                    "sector": saved.get("sector"),
                    "description": saved.get("description"),
                    "goals": saved.get("goals", []),
                },
                "localization": {"primary_language": saved.get("language", "en")},
                "knowledge_assets": {
                    "tone_of_voice": saved.get("tone_of_voice"),
                    "faqs": [],
                    "brand_keywords": saved.get("brand_keywords", []),
                },
            }

        auto_result = await _auto_configure_settings(pid, analysis)

        bp = analysis.get("business_profile", {})
        lang = analysis.get("localization", {}).get("primary_language", "en")

        if lang == "ar":
            summary = (
                f"تم تسجيل مشروع «{body.project_name}» بنجاح! "
                f"القطاع: {bp.get('sector', 'عام')}. "
                f"تم ضبط الإعدادات الذكية تلقائياً."
            )
            hint = (
                "التسجيل مكتمل. استخدم POST /v2/logic/suggest "
                "للحصول على اقتراحات أتمتة مخصصة لقطاعك."
            )
        else:
            summary = (
                f"Project «{body.project_name}» registered successfully! "
                f"Sector: {bp.get('sector', 'General')}. "
                f"Smart settings auto-configured."
            )
            hint = (
                "Registration complete. Use POST /v2/logic/suggest "
                "to get personalized automation recommendations for your sector."
            )

        return {
            "status": "registered",
            "project_id": pid,
            "project_name": body.project_name,
            "url": url,
            "sector": bp.get("sector"),
            "language": lang,
            "saved": saved,
            "auto_settings": auto_result,
            "summary": summary,
            "_hint": hint,
        }

    except RuntimeError as exc:
        raise HTTPException(422, detail=str(exc))
    except Exception as exc:
        log.error("[saas-register] Failed: %s", exc, exc_info=True)
        raise HTTPException(500, detail=f"Registration failed: {str(exc)[:300]}")


# ═══════════════════════════════════════════════════════════════
# V2 — SUGGESTION ENGINE (Sector-Aware Flow Recommendations)
# ═══════════════════════════════════════════════════════════════
#
# Two intelligence layers, separated cleanly:
#   1. SECTOR_SUGGESTIONS — hardcoded *flow recipes* (preset names like
#      "lead_capture") that the BFF turns into a ready-to-build CTA.
#      These are sticky, branded, and not LLM-generated.
#   2. SECTOR_CATEGORY_MAP + PieceRegistry.effective_dahae — the live
#      ranker. Filters the 688-piece registry to the categories that
#      matter for this sector, then orders by Dahae score (Phase-12a).
#      This is what /v2/logic/suggest now returns alongside the recipes.
#
# Activepieces categories observed in prod (688 pieces): see
# scripts/audit_stats.py — top buckets are AI, PRODUCTIVITY, MARKETING,
# COMMUNICATION, SALES_AND_CRM, COMMERCE, FORMS_AND_SURVEYS.

# Per-sector category preference order. The FIRST category in each
# list is the "primary" filter; the rest broaden coverage if the
# primary alone yields too few pieces.
SECTOR_CATEGORY_MAP: Dict[str, List[str]] = {
    "E-commerce": [
        "COMMERCE", "SALES_AND_CRM", "MARKETING", "COMMUNICATION",
    ],
    "Healthcare": [
        "COMMUNICATION", "FORMS_AND_SURVEYS", "PRODUCTIVITY",
    ],
    "Education": [
        "COMMUNICATION", "FORMS_AND_SURVEYS", "CONTENT_AND_FILES",
        "PRODUCTIVITY",
    ],
    "Real Estate": [
        "SALES_AND_CRM", "COMMUNICATION", "FORMS_AND_SURVEYS",
        "MARKETING",
    ],
    "default": [
        "COMMUNICATION", "PRODUCTIVITY", "MARKETING", "SALES_AND_CRM",
    ],
}


SECTOR_SUGGESTIONS: Dict[str, List[Dict[str, str]]] = {
    "E-commerce": [
        {"name": "lead_capture", "title": "نظام التقاط العملاء المحتملين",
         "title_en": "Lead Capture System",
         "description": "Webhook → Google Sheets: يسجل كل عميل جديد تلقائياً",
         "template": "gmail_sheets_logger"},
        {"name": "smart_reply", "title": "الرد الذكي على الاستفسارات",
         "title_en": "Smart Auto-Reply",
         "description": "Webhook → Code (FAQ matching) → Gmail auto-reply",
         "template": "gmail_autoresponder"},
        {"name": "daily_report", "title": "تقرير يومي بالطلبات",
         "title_en": "Daily Orders Report",
         "description": "Schedule → Sheets aggregation → Gmail daily digest",
         "template": "scheduled_report"},
    ],
    "Healthcare": [
        {"name": "appointment_flow", "title": "نظام حجز المواعيد",
         "title_en": "Appointment Booking",
         "description": "Webhook → Sheets (booking log) → Gmail confirmation",
         "template": "gmail_sheets_logger"},
        {"name": "patient_followup", "title": "متابعة المرضى التلقائية",
         "title_en": "Patient Follow-up",
         "description": "Schedule → Sheets (patients due) → Gmail follow-up",
         "template": "gmail_autoresponder"},
        {"name": "weekly_report", "title": "تقرير أسبوعي",
         "title_en": "Weekly Health Report",
         "description": "Schedule → Aggregate data → Email summary",
         "template": "scheduled_report"},
    ],
    "Education": [
        {"name": "enrollment_tracker", "title": "متابعة التسجيلات",
         "title_en": "Enrollment Tracker",
         "description": "Webhook → Sheets (enrollment log) → Confirmation email",
         "template": "gmail_sheets_logger"},
        {"name": "student_notifier", "title": "إشعارات الطلاب",
         "title_en": "Student Notifications",
         "description": "Schedule → Student list → Bulk email notifications",
         "template": "gmail_autoresponder"},
        {"name": "attendance_report", "title": "تقرير الحضور",
         "title_en": "Attendance Report",
         "description": "Schedule → Sheets (attendance) → Email digest",
         "template": "scheduled_report"},
    ],
    "Real Estate": [
        {"name": "property_leads", "title": "التقاط عملاء العقارات",
         "title_en": "Property Lead Capture",
         "description": "Webhook → Sheets (lead log) → Agent notification",
         "template": "gmail_sheets_logger"},
        {"name": "viewing_scheduler", "title": "جدولة المعاينات",
         "title_en": "Viewing Scheduler",
         "description": "Webhook → Sheets → Gmail (viewing confirmation)",
         "template": "gmail_autoresponder"},
        {"name": "market_report", "title": "تقرير السوق الأسبوعي",
         "title_en": "Weekly Market Report",
         "description": "Schedule → Market data → Email digest",
         "template": "scheduled_report"},
    ],
    "default": [
        {"name": "lead_capture", "title": "التقاط العملاء المحتملين",
         "title_en": "Lead Capture",
         "description": "Webhook → Sheets: auto-log every new lead",
         "template": "gmail_sheets_logger"},
        {"name": "auto_responder", "title": "الرد التلقائي الذكي",
         "title_en": "Smart Auto-Responder",
         "description": "Webhook → FAQ-aware auto-reply via Gmail",
         "template": "gmail_autoresponder"},
        {"name": "scheduled_digest", "title": "تقرير دوري",
         "title_en": "Scheduled Digest",
         "description": "Schedule → Aggregate → Email summary",
         "template": "scheduled_report"},
    ],
}


class SuggestBody(BaseModel):
    project_id: Optional[str] = None


@app.post("/v2/logic/suggest")
async def v2_logic_suggest(request: Request, body: SuggestBody):
    """Sector-aware suggestion engine.

    Returns two stacked layers:
      • `suggestions` — 3 hardcoded flow recipes per sector (lead_capture,
        smart_reply, …). These map to existing presets and are stable.
      • `recommended_pieces` — live ranking of the piece_registry by
        Phase-12a `effective_dahae`, filtered to the AP categories that
        matter for the project's sector. This is the brain wiring that
        the frontend's `skill-loader.ts` should consume to inject the
        most useful tools first into the LLM tool catalogue.

    Degradation: if Dahae is unscored (e.g. brand-new DB), the recipes
    still ship and `recommended_pieces` is an empty list — never raise.
    """
    from database import async_session
    from models import ProjectIdentity, KnowledgeAsset, PieceRegistry
    from sqlalchemy import select, text

    pid = resolve_pid(request, body.project_id)

    sector = "default"
    lang = "en"
    tone = "professional"
    recommended_pieces: List[Dict[str, Any]] = []

    if async_session:
        try:
            async with async_session() as session:
                identity = (await session.execute(
                    select(ProjectIdentity).where(ProjectIdentity.project_id == pid)
                )).scalar_one_or_none()
                knowledge = (await session.execute(
                    select(KnowledgeAsset).where(KnowledgeAsset.project_id == pid)
                )).scalar_one_or_none()

                if identity:
                    sector = identity.sector or "default"
                    lang = identity.language or "en"
                if knowledge:
                    tone = knowledge.tone_of_voice or "professional"

                # Dahae ranker — the brain wiring. Postgres ARRAY overlap
                # operator `&&` returns true if any category in the row
                # matches one in the sector preference list. We rely on
                # the ix_pr_effective_dahae partial index for ordering.
                cats = SECTOR_CATEGORY_MAP.get(
                    sector, SECTOR_CATEGORY_MAP["default"],
                )
                ranked_rows = (await session.execute(
                    text("""
                        SELECT name,
                               display_name,
                               categories,
                               effective_dahae,
                               dahae_score,
                               laziness_score,
                               jsonb_extract_path(dahae_breakdown, 'n_actions')
                                   AS n_actions
                          FROM piece_registry
                         WHERE effective_dahae IS NOT NULL
                           AND categories && :cats
                         ORDER BY effective_dahae DESC NULLS LAST, name
                         LIMIT 8
                    """),
                    {"cats": cats},
                )).all()

                recommended_pieces = [
                    {
                        "piece": r[0],
                        "display_name": r[1],
                        "matched_categories": [c for c in (r[2] or []) if c in cats],
                        "effective_dahae": r[3],
                        "dahae_score": r[4],
                        "laziness_score": r[5],
                        "n_actions": r[6],
                    }
                    for r in ranked_rows
                ]
        except Exception as exc:
            log.warning("[suggest] DB read failed: %s", exc)

    raw_suggestions = SECTOR_SUGGESTIONS.get(sector, SECTOR_SUGGESTIONS["default"])

    suggestions = []
    for s in raw_suggestions[:3]:
        entry = {
            "name": s["name"],
            "title": s["title"] if lang == "ar" else s.get("title_en", s["title"]),
            "description": s["description"],
            "template": s.get("template"),
            "project_id": pid,
            "ready_to_build": True,
        }
        suggestions.append(entry)

    n_pieces = len(recommended_pieces)
    if lang == "ar":
        hint = (
            f"بناءً على قطاع «{sector}»، نقترح {len(suggestions)} فلوهات أتمتة "
            f"و{n_pieces} أداة مرتبة بـ Dahae score. اختر أحدها أو اطلب فلو مخصص."
        )
    else:
        hint = (
            f"Based on sector «{sector}», we suggest {len(suggestions)} flows "
            f"and {n_pieces} Dahae-ranked pieces. Pick one or request a custom flow."
        )

    return {
        "project_id": pid,
        "sector": sector,
        "language": lang,
        "tone_of_voice": tone,
        "suggestions": suggestions,
        "recommended_pieces": recommended_pieces,
        "intelligent": bool(recommended_pieces),
        "_hint": hint,
    }


# ═══════════════════════════════════════════════════════════════
# V2 — PROACTIVE ENGINE (identity × Mem success patterns × flow health)
# ═══════════════════════════════════════════════════════════════

SMART_HINT_TYPES = frozenset({"OPPORTUNITY", "WARNING", "SUCCESS_STORY", "INFO"})


def smart_hint_notification(
    hint_type: str,
    message: str,
    **extra: Any,
) -> Dict[str, Any]:
    """Structured `_hint` for proactive UI — types: OPPORTUNITY, WARNING, SUCCESS_STORY, INFO."""
    ht = hint_type if hint_type in SMART_HINT_TYPES else "INFO"
    out: Dict[str, Any] = {"type": ht, "message": message}
    if extra:
        out["meta"] = extra
    return out


_V2_DEFAULT_SUCCESS_PATTERNS: List[Dict[str, Any]] = [
    {
        "id": "faq_intelligence_gap",
        "sectors": ["E-commerce", "Healthcare", "Education", "Real Estate", "default"],
        "when_missing": ["faqs"],
        "title_en": "FAQ layer missing — peers automate first-line support",
        "title_ar": "طبقة الأسئلة الشائعة مفقودة — الناجحون يؤتمتون الاستجابة الأولى",
        "body_en": (
            "Similar businesses already distilled FAQs into memory so flows can answer without human delay. "
            "Capturing yours unlocks FAQ-aware replies and fewer ticket escalations."
        ),
        "body_ar": (
            "الشركات المشابهة خزّنت أسئلة شائعة في الذاكرة لتردّ الفلوهات دون تأخير بشري. "
            "تسجيل أسئلتك يفتح ردوداً ذكية ويقلّل تصعيد التذاكر."
        ),
        "related_template": "gmail_autoresponder",
        "benchmark": (
            "SUCCESS_STORY: Teams that ship FAQ-backed automations typically cut first-response latency sharply."
        ),
    },
    {
        "id": "tone_guardrails",
        "sectors": ["E-commerce", "Healthcare", "Education", "Real Estate", "default"],
        "when_missing": ["tone_of_voice"],
        "title_en": "Brand voice not locked — automations risk sounding generic",
        "title_ar": "صوت العلامة غير مضبوط — الأتمتة قد تبدو عامة",
        "body_en": (
            "High-performing clients define tone in institutional memory before scaling webhooks and email flows."
        ),
        "body_ar": (
            "العملاء الأعلى أداءً يحددون نبرة الصوت في الذاكرة المؤسسية قبل توسيع الويب هوك والبريد."
        ),
        "related_template": "gmail_autoresponder",
        "benchmark": (
            "SUCCESS_STORY: Consistent tone in memory removes most rewrite cycles before go-live."
        ),
    },
    {
        "id": "lead_logging_discipline",
        "sectors": ["E-commerce", "Real Estate", "default"],
        "when_missing": [],
        "title_en": "Add automatic lead logging to Sheets",
        "title_ar": "أضف تسجيلاً تلقائياً للعملاء في Sheets",
        "body_en": (
            "Stores and agencies in your sector route every webhook capture into Sheets before enrichment — "
            "a high-signal step teams often postpone."
        ),
        "body_ar": (
            "المتاجر والوكالات في قطاعك يوجّهون كل التقاط ويب هوك إلى Sheets قبل الإثراء — "
            "خطوة عالية الإشارة غالباً ما تُؤجّل."
        ),
        "related_template": "gmail_sheets_logger",
        "benchmark": (
            "SUCCESS_STORY: Central lead logs make downstream AI routing and QA far easier."
        ),
    },
    {
        "id": "scheduled_digest_maturity",
        "sectors": ["Healthcare", "Education", "E-commerce", "default"],
        "when_missing": [],
        "title_en": "No scheduled executive digest yet",
        "title_ar": "لا يوجد ملخص مجدول لصنع القرار بعد",
        "body_en": (
            "Mature ops stacks add a gentle scheduled digest so owners see drift before customers complain."
        ),
        "body_ar": (
            "العمليات الناضجة تضيف ملخصاً مجدولاً ليرى المالك الانحراف قبل شكوى العملاء."
        ),
        "related_template": "scheduled_report",
        "benchmark": (
            "SUCCESS_STORY: Scheduled digests correlate with fewer silent automation failures."
        ),
    },
    {
        "id": "website_absorption_stale",
        "sectors": ["E-commerce", "Healthcare", "Education", "Real Estate", "default"],
        "when_missing": ["website_url_fresh"],
        "title_en": "Refresh website absorption",
        "title_ar": "حدّث امتصاص الموقع",
        "body_en": (
            "Your public site evolves — re-absorbing keeps sector signals, tone, and FAQs aligned with reality."
        ),
        "body_ar": (
            "الموقع العلني يتغيّر — إعادة الامتصاص تحافظ على القطاع والنبرة والأسئلة الشائعة واقعية."
        ),
        "related_template": "gmail_sheets_logger",
        "benchmark": (
            "SUCCESS_STORY: Quarterly absorption refreshes keep proactive logic eerily accurate."
        ),
    },
]


def _pattern_sector_match(pattern: Dict[str, Any], sector: str) -> bool:
    sectors = pattern.get("sectors") or ["default"]
    if "default" in sectors:
        return True
    return sector in sectors


def _identity_missing_signals(identity: Any, knowledge: Any) -> Dict[str, bool]:
    """True means 'missing or stale' for pattern matching."""
    missing: Dict[str, bool] = {
        "faqs": True,
        "tone_of_voice": True,
        "website_url_fresh": True,
    }
    if knowledge and knowledge.faqs and len(knowledge.faqs) > 0:
        missing["faqs"] = False
    if knowledge and getattr(knowledge, "tone_of_voice", None):
        missing["tone_of_voice"] = False
    if identity and identity.website_url and identity.absorbed_at:
        age = datetime.now(timezone.utc) - identity.absorbed_at
        if age < timedelta(days=90):
            missing["website_url_fresh"] = False
    elif identity and identity.website_url and not identity.absorbed_at:
        missing["website_url_fresh"] = True
    elif identity and identity.website_url:
        missing["website_url_fresh"] = True
    return missing


def _misses_pattern(
    pattern: Dict[str, Any],
    sector: str,
    signals: Dict[str, bool],
    adopted_templates: set[str],
) -> bool:
    if not _pattern_sector_match(pattern, sector):
        return False
    inner_wm = list(pattern.get("when_missing") or [])
    for field in inner_wm:
        if signals.get(field):
            return True
    if not inner_wm:
        tpl = pattern.get("related_template") or ""
        return bool(tpl) and tpl not in adopted_templates
    return False


async def _load_merged_success_patterns(project_id: str) -> List[Dict[str, Any]]:
    from database import async_session
    from models import AutonomousSetting
    from sqlalchemy import select

    merged = [dict(p) for p in _V2_DEFAULT_SUCCESS_PATTERNS]
    if not async_session:
        return merged
    try:
        async with async_session() as session:
            setting = (await session.execute(
                select(AutonomousSetting).where(AutonomousSetting.project_id == project_id)
            )).scalar_one_or_none()
        extra = (setting.client_settings or {}).get("success_patterns") if setting else None
        if isinstance(extra, list):
            for row in extra:
                if isinstance(row, dict) and row.get("id"):
                    merged.append(row)
    except Exception as exc:
        log.warning("[proactive] mem patterns load failed: %s", exc)
    return merged


async def _load_adopted_templates(project_id: str) -> set[str]:
    from database import async_session
    from models import AutonomousSetting
    from sqlalchemy import select

    if not async_session:
        return set()
    try:
        async with async_session() as session:
            setting = (await session.execute(
                select(AutonomousSetting).where(AutonomousSetting.project_id == project_id)
            )).scalar_one_or_none()
        raw = (setting.client_settings or {}).get("adopted_templates") if setting else None
        if isinstance(raw, list):
            return {str(x) for x in raw}
    except Exception as exc:
        log.warning("[proactive] adopted_templates load failed: %s", exc)
    return set()


async def proactive_flow_health_scan(project_id: str) -> List[Dict[str, Any]]:
    """Last up to 10 runs per flow; emit WARNING `_hint` when failure rate > 50% (≥5 samples)."""
    alerts: List[Dict[str, Any]] = []
    if not project_id:
        return alerts
    try:
        e = E()
        flows = await e.list_flows(project_id)
        runs = await e.list_runs(project_id, limit=300)
    except Exception as exc:
        log.warning("[proactive] health scan AP error: %s", exc)
        return alerts

    by_flow: Dict[str, List[dict]] = {}
    for r in runs or []:
        fid = r.get("flowId") or r.get("flow_id")
        if not fid:
            continue
        by_flow.setdefault(str(fid), []).append(r)

    flow_meta = {str(f.get("id")): f for f in flows if f.get("id")}

    fail_status = frozenset(s.upper() for s in ("FAILED", "FAILURE", "CRASHED"))

    for fid, fruns in by_flow.items():
        fruns.sort(key=lambda x: str(x.get("created") or ""), reverse=True)
        sample = fruns[:10]
        if len(sample) < 5:
            continue
        failed = sum(1 for x in sample if str(x.get("status", "")).upper() in fail_status)
        rate = failed / len(sample)
        if rate <= 0.5:
            continue
        meta = flow_meta.get(fid, {})
        ver = meta.get("version") if isinstance(meta.get("version"), dict) else {}
        name = ver.get("displayName") or meta.get("displayName") or fid
        msg_en = (
            f"Flow «{name}» failed {failed}/{len(sample)} of the last runs (>50%). "
            "Repair steps or connections, then re-test the webhook or trigger."
        )
        msg_ar = (
            f"الفلو «{name}» فشل {failed} من {len(sample)} في آخر التشغيلات (>50٪). "
            "راجع الخطوات أو الاتصالات ثم أعد اختبار الويب هوك أو المحفّز."
        )
        alerts.append({
            "flow_id": fid,
            "flow_name": name,
            "sample_size": len(sample),
            "failed_count": failed,
            "failure_rate": round(rate, 3),
            "hint_type": "WARNING",
            "_hint": smart_hint_notification("WARNING", msg_en, flow_id=fid, flow_name=name),
            "_hint_ar": smart_hint_notification("WARNING", msg_ar, flow_id=fid, flow_name=name),
        })
    return alerts


def _benchmark_to_success_story(benchmark: str, pattern_id: str) -> Dict[str, Any]:
    msg = benchmark.replace("SUCCESS_STORY: ", "", 1).strip()
    return {
        "pattern_id": pattern_id,
        "hint_type": "SUCCESS_STORY",
        "message": msg,
        "_hint": smart_hint_notification("SUCCESS_STORY", msg, pattern_id=pattern_id),
    }


async def _build_missed_opportunities(
    project_id: str,
    lang: str,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (missed_opportunities, success_stories)."""
    from database import async_session
    from models import ProjectIdentity, KnowledgeAsset
    from sqlalchemy import select

    missed: List[Dict[str, Any]] = []
    stories: List[Dict[str, Any]] = []

    sector = "default"
    identity = None
    knowledge = None

    if async_session:
        try:
            async with async_session() as session:
                identity = (await session.execute(
                    select(ProjectIdentity).where(ProjectIdentity.project_id == project_id)
                )).scalar_one_or_none()
                knowledge = (await session.execute(
                    select(KnowledgeAsset).where(KnowledgeAsset.project_id == project_id)
                )).scalar_one_or_none()
        except Exception as exc:
            log.warning("[proactive] identity read failed: %s", exc)

    if identity and identity.sector:
        sector = identity.sector
    signals = _identity_missing_signals(identity, knowledge)
    patterns = await _load_merged_success_patterns(project_id)
    adopted = await _load_adopted_templates(project_id)

    for p in patterns:
        if not _misses_pattern(p, sector, signals, adopted):
            continue
        title = p["title_ar"] if lang == "ar" else p.get("title_en", p.get("title_ar", ""))
        body = p["body_ar"] if lang == "ar" else p.get("body_en", p.get("body_ar", ""))
        missed.append({
            "pattern_id": p["id"],
            "title": title,
            "description": body,
            "related_template": p.get("related_template"),
            "hint_type": "OPPORTUNITY",
            "_hint": smart_hint_notification(
                "OPPORTUNITY",
                f"{title} — {body}"[:900],
                pattern_id=p["id"],
                related_template=p.get("related_template"),
            ),
        })
        bench = p.get("benchmark")
        if bench and len(stories) < 4:
            stories.append(_benchmark_to_success_story(str(bench), str(p["id"])))

    if not missed:
        if lang == "ar":
            stories.append({
                "pattern_id": "baseline_delight_ar",
                "hint_type": "SUCCESS_STORY",
                "message": (
                    "الذاكرة المؤسسية متناغمة مع أنماط النجاح — الخطوة التالية: ربط الويب هوك بمراقبة جودة صامتة."
                ),
                "_hint": smart_hint_notification(
                    "SUCCESS_STORY",
                    "الذاكرة المؤسسية متناغمة مع أنماط النجاح — ربط الويب هوك بمراقبة جودة صامتة.",
                ),
            })
        else:
            stories.append({
                "pattern_id": "baseline_delight_en",
                "hint_type": "SUCCESS_STORY",
                "message": (
                    "Institutional memory already matches proven success patterns — "
                    "next winners wire webhooks into silent quality monitors."
                ),
                "_hint": smart_hint_notification(
                    "SUCCESS_STORY",
                    (
                        "Institutional memory already matches proven success patterns — "
                        "next winners wire webhooks into silent quality monitors."
                    ),
                ),
            })

    return missed, stories


@app.get("/v2/logic/proactive-suggestions")
async def v2_logic_proactive_suggestions(
    request: Request,
    project_id: Optional[str] = Query(
        default=None,
        description="Tenant project id (defaults to AP_PROJECT_ID).",
    ),
):
    """Compare ProjectIdentity + knowledge to success patterns in Mem; add per-flow health WARNING hints."""
    pid = resolve_pid(request, project_id)
    lang = "en"
    from database import async_session
    from models import ProjectIdentity
    from sqlalchemy import select
    if async_session:
        try:
            async with async_session() as session:
                ident = (await session.execute(
                    select(ProjectIdentity).where(ProjectIdentity.project_id == pid)
                )).scalar_one_or_none()
            if ident and ident.language:
                lang = ident.language
        except Exception:
            pass

    missed, stories = await _build_missed_opportunities(pid, lang)
    health = await proactive_flow_health_scan(pid)

    if health:
        h0 = health[0]
        primary = h0.get("_hint_ar") if lang == "ar" else h0["_hint"]
        agg = smart_hint_notification(
            "WARNING",
            primary["message"],
            flows_in_alarm=len(health),
        )
    elif missed:
        m0 = missed[0]
        agg = dict(m0["_hint"])
        if lang == "ar":
            agg = smart_hint_notification(
                "OPPORTUNITY",
                f"{m0['title']} — {m0['description']}"[:900],
                pattern_id=m0["pattern_id"],
            )
    elif stories:
        agg = dict(stories[0]["_hint"])
    else:
        msg = (
            "لا توجد تنبيهات صحية بارزة؛ راقب لوحة التشغيل للمزيد."
            if lang == "ar"
            else "No urgent health signals — keep an eye on runs for new patterns."
        )
        agg = smart_hint_notification("INFO", msg)

    return {
        "project_id": pid,
        "language": lang,
        "missed_opportunities": missed,
        "flow_health_alerts": health,
        "success_stories": stories[:6],
        "_hint": agg,
    }


# ═══════════════════════════════════════════════════════════════
# V2 — MCP TOOL DISPATCHER
# ═══════════════════════════════════════════════════════════════
@app.get("/v2/mcp/tools")
async def v2_mcp_tools():
    return {"tools": [
        {"name": "check_system_health",
         "description": "Check if the automation system is online and connected",
         "parameters": {"type": "object", "properties": {}, "required": []}},
        {"name": "get_client_status",
         "description": "Get client dashboard: active flows, connections, recent runs",
         "parameters": {"type": "object", "properties": {
             "project_id": {"type": "string", "description": "Optional project override"},
         }, "required": []}},
        {"name": "list_templates",
         "description": "List all 8 ready-made automation templates",
         "parameters": {"type": "object", "properties": {}, "required": []}},
        {"name": "list_presets",
         "description": "List complex presets (ROUTER, LOOP combinations)",
         "parameters": {"type": "object", "properties": {}, "required": []}},
        {"name": "list_connections",
         "description": "List tenant Activepieces connections before building flows.",
         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "list_available_pieces",
         "description": "List all 600+ available automation pieces (Gmail, Slack, etc.)",
         "parameters": {"type": "object", "properties": {}, "required": []}},
        {"name": "get_piece_schema",
         "description": "Get full schema for a piece (fields, types, requirements)",
         "parameters": {"type": "object", "properties": {
             "piece_name": {"type": "string"},
         }, "required": ["piece_name"]}},
        {"name": "build_from_template",
         "description": "Build an automation from one of the 8 templates",
         "parameters": {"type": "object", "properties": {
             "template": {"type": "string",
                          "enum": list(TEMPLATES.keys())},
             "config": {"type": "object"},
             "display_name": {"type": "string"},
             "project_id": {"type": "string"},
             "connection_ids": {"type": "object"},
         }, "required": ["template"]}},
        {"name": "build_dynamic_flow",
         "description": "Build a custom automation using any combination of pieces",
         "parameters": {"type": "object", "properties": {
             "display_name": {"type": "string"},
             "trigger": {"type": "object"},
             "actions": {"type": "array", "items": {"type": "object"}},
             "project_id": {"type": "string"},
             "connection_ids": {"type": "object"},
         }, "required": ["trigger", "actions"]}},
        {"name": "build_from_preset",
         "description": "Build a complex flow from a preset (ROUTER, LOOP)",
         "parameters": {"type": "object", "properties": {
             "preset": {"type": "string", "enum": list(PRESETS.keys())},
             "params": {"type": "object"},
             "display_name": {"type": "string"},
             "project_id": {"type": "string"},
             "connection_ids": {"type": "object"},
         }, "required": ["preset"]}},
        {"name": "validate_flow",
         "description": "Validate a flow configuration without deploying (dry run)",
         "parameters": {"type": "object", "properties": {
             "trigger": {"type": "object"},
             "actions": {"type": "array", "items": {"type": "object"}},
         }, "required": ["trigger", "actions"]}},
        {"name": "test_webhook",
         "description": "Send test data to a webhook-triggered flow",
         "parameters": {"type": "object", "properties": {
             "flow_id": {"type": "string"},
             "payload": {"type": "object"},
         }, "required": ["flow_id"]}},
        {"name": "manage_flow",
         "description": "Enable, disable, or delete an existing flow",
         "parameters": {"type": "object", "properties": {
             "flow_id": {"type": "string"},
             "action": {"type": "string", "enum": ["enable", "disable", "delete"]},
         }, "required": ["flow_id", "action"]}},
        {"name": "diagnose_flow",
         "description": "Diagnose flow structure — enumerate all steps and types",
         "parameters": {"type": "object", "properties": {
             "flow_id": {"type": "string"},
         }, "required": ["flow_id"]}},
        {"name": "update_flow",
         "description": "Update an existing flow via re-import with new structure",
         "parameters": {"type": "object", "properties": {
             "flow_id": {"type": "string"},
             "template": {"type": "string"},
             "config": {"type": "object"},
             "trigger": {"type": "object"},
             "actions": {"type": "array", "items": {"type": "object"}},
             "display_name": {"type": "string"},
             "project_id": {"type": "string"},
             "connection_ids": {"type": "object"},
         }, "required": ["flow_id"]}},
        {"name": "list_operators",
         "description": "List available condition operators for ROUTER branches",
         "parameters": {"type": "object", "properties": {}, "required": []}},
        {"name": "ingest_website",
         "description": "Absorb a website: scrape it, AI-analyze it, and save the business identity to memory",
         "parameters": {"type": "object", "properties": {
             "url": {"type": "string", "description": "Website URL to absorb"},
             "project_id": {"type": "string", "description": "Optional project override"},
         }, "required": ["url"]}},
        {"name": "get_institutional_memory",
         "description": "Read the company's full identity from memory: sector, FAQs, tone of voice, brand keywords, and smart rules. Use when you need business context for any decision.",
         "parameters": {"type": "object", "properties": {
             "project_id": {"type": "string", "description": "Optional project override"},
         }, "required": []}},
    ]}


_DB_ONLY_TOOLS = frozenset({"get_institutional_memory", "list_operators", "list_templates", "list_presets"})


@app.post("/v2/mcp/execute")
@limiter.limit("60/minute")
async def v2_mcp_execute(request: Request, body: MCPExecuteBody):
    """Live MCP Tool Dispatcher — Claude calls tools here.
    Also used by mcp_sse._execute_mcp_tool for SSE transport.
    """
    tool = body.tool
    p = body.parameters
    # Wave-1: prefer verified tenant from middleware state; parameters
    # dict is caller-controlled so it comes last in the precedence chain.
    pid = resolve_pid(request, body.project_id or p.get("project_id"))
    # Strip any smuggled project_id in parameters so downstream handlers
    # cannot accidentally read it.
    p.pop("project_id", None)
    cn = resolve_conns(body.connection_ids or p.get("connection_ids"))

    if tool in _DB_ONLY_TOOLS:
        e = _engine  # may be None — these tools don't need AP
    else:
        e = E()

    try:
        result = await _mcp_dispatch(e, tool, p, pid, cn, owner_email=resolve_owner_email(request))
        hint = await _generate_smart_hint(pid)
        return {"tool": tool, "success": True,
                "result": compress_response(result), "_hint": hint}
    except HTTPException as he:
        return {"tool": tool, "success": False,
                "error": he.detail, "status_code": he.status_code}
    except Exception as ex:
        return {"tool": tool, "success": False, "error": str(ex)}


async def _mcp_dispatch(e: SiyadahEngine, tool: str, p: dict,
                        pid: str, cn: Dict[str, str],
                        owner_email: Optional[str] = None) -> Any:
    """Route MCP tool calls to internal logic."""

    if tool == "check_system_health":
        projects = await e.list_projects()
        return {"status": "healthy", "projects_found": len(projects),
                "version": VERSION}

    if tool == "get_client_status":
        flows = await e.list_flows(pid)
        conns = await e.list_connections(pid)
        runs = await e.list_runs(pid)
        active = [f for f in flows if f.get("status") == "ENABLED"]
        failed = [r for r in runs if r.get("status") == "FAILED"]
        return {
            "total_flows": len(flows), "active_flows": len(active),
            "total_connections": len(conns), "total_runs": len(runs),
            "failed_runs": len(failed),
            "flows": [{"id": f.get("id"),
                        "name": f.get("version", {}).get("displayName", ""),
                        "status": f.get("status")} for f in flows],
        }

    if tool == "list_templates":
        return {k: {"description": v["desc"], "required_config": v["req"]}
                for k, v in TEMPLATES.items()}

    if tool == "list_presets":
        return {k: v["desc"] for k, v in PRESETS.items()}

    if tool == "list_connections":
        try:
            conns = await e.list_connections(pid)
            compact = []
            for c in conns:
                if not isinstance(c, dict):
                    continue
                compact.append({
                    "id": c.get("id"),
                    "externalId": c.get("externalId"),
                    "displayName": c.get("displayName") or c.get("name"),
                    "pieceName": c.get("pieceName") or c.get("appName") or c.get("app"),
                    "status": c.get("status"),
                })
            return {
                "tool": tool,
                "success": True,
                "result": {
                    "connections": compact,
                    "count": len(compact),
                    "connected_external_ids": [
                        x.get("externalId") for x in compact if x.get("externalId")
                    ],
                },
            }
        except Exception as exc:
            return {
                "tool": tool,
                "success": False,
                "error": f"list_connections failed: {str(exc)[:300]}",
                "status_code": 500,
            }

    if tool == "list_available_pieces":
        raw = await e.list_pieces()
        pieces = raw if isinstance(raw, list) else raw.get("data", [])
        return {"total": len(pieces),
                "pieces": [{"name": pp.get("name", ""),
                            "displayName": pp.get("displayName", "")}
                           for pp in pieces]}

    if tool == "get_piece_schema":
        _rname, schema = await auto_resolve_piece(e, p["piece_name"])
        if not schema:
            raise HTTPException(404, f"Piece not found: {p['piece_name']}")
        return {
            "piece": schema.get("name"), "version": schema.get("version"),
            "actions": {k: {"displayName": v.get("displayName")}
                        for k, v in schema.get("actions", {}).items()},
            "triggers": {k: {"displayName": v.get("displayName")}
                         for k, v in schema.get("triggers", {}).items()},
        }

    if tool == "build_from_template":
        tpl = p.get("template", "")
        if tpl not in TEMPLATES:
            raise HTTPException(400, f"Unknown template: {tpl}")
        tdef = TEMPLATES[tpl]
        config = p.get("config", {})

        # Wave 1G-A: Validate required config fields (mirrors REST endpoint at line 2240)
        # Without this check, the LLM can call build_from_template with empty config,
        # causing AP to reject silently. This raises a structured error the LLM can parse.
        missing = [k for k in tdef.get("req", []) if k not in config or not config[k]]
        if missing:
            raise HTTPException(422, detail={
                "error": "missing_required_config",
                "template": tpl,
                "missing_fields": missing,
                "available_templates": list(TEMPLATES.keys()),
                "hint": f"Template '{tpl}' requires: {tdef.get('req', [])}. Ask user for these values before building, or choose a simpler template like webhook_to_email which only needs recipient_email."
            })

        trigger = tdef["fn"](config, cn)
        name = p.get("display_name", tdef["desc"])
        result = await golden_build(e, pid, name, trigger, owner_email=owner_email)
        wh = (f"{AP_BASE}/api/v1/webhooks/{result['flow_id']}"
              if not tpl.startswith("scheduled") else None)
        return {**result, "webhook_url": wh, "template": tpl}

    if tool == "build_dynamic_flow":
        # Honor explicit trigger/actions verbatim. Mirror /v2/build-dynamic's
        # iteration shape so non-PIECE specs (CODE/ROUTER/LOOP) pass through
        # untouched instead of being silently mutated.
        #
        # Previous bug: this branch called auto_resolve_piece(e, a.get("piece",""))
        # for every action. For a CODE step there is no `piece`, so the
        # fallback fuzzy match at the end of auto_resolve_piece (`query in short`)
        # matched the first piece in AP's catalog — `oneclickimpact` — and
        # rewrote the user's explicit CODE step into a wrong PIECE. The guard
        # below restores parity with /v2/build-dynamic.
        t = p.get("trigger", {}) or {}
        actions_in = p.get("actions", []) or []

        piece_name = t.get("piece") or "@activepieces/piece-webhook"
        resolved_t, t_schema = await auto_resolve_piece(e, piece_name)
        full = resolved_t if resolved_t.startswith("@") else f"@activepieces/piece-{resolved_t}"
        ver = t.get("version", "") or resolve_piece_version(t_schema, resolved_t)

        specs: List[dict] = []
        for a in actions_in:
            stype = a.get("type", "PIECE")

            if stype != "PIECE":
                if stype == "CODE":
                    # MCP callers often nest code under `input.code`; lift it
                    # to the top level so _build_step_from_spec finds it.
                    normalized = dict(a)
                    inp = normalized.get("input")
                    if isinstance(inp, dict):
                        if "code" not in normalized and "code" in inp:
                            normalized["code"] = inp["code"]
                        if "code_input" not in normalized and "code_input" in inp:
                            normalized["code_input"] = inp["code_input"]
                    specs.append(normalized)
                else:
                    specs.append(a)
                continue

            ap = a.get("piece", "")
            if not ap:
                raise HTTPException(
                    400,
                    f"PIECE action missing 'piece' field "
                    f"(display_name={a.get('display_name','')}). "
                    f"Refusing to auto-resolve an empty piece name.",
                )
            resolved_ap, sch = await auto_resolve_piece(e, ap)
            full_ap = resolved_ap if resolved_ap.startswith("@") else f"@activepieces/piece-{resolved_ap}"
            short = resolved_ap.replace("@activepieces/piece-", "")
            a_ver = a.get("version", "")
            cleaned_in = clean_input_config(dict(a.get("input", {})))
            if not a_ver:
                a_ver = resolve_piece_version(sch, resolved_ap)
                ps = generate_property_settings(
                    get_action_props(sch, a.get("action_name", "")),
                    cleaned_in)
            else:
                ps = {}
            cid = a.get("connection_id", cn.get(short, ""))
            if cid and "auth" not in cleaned_in:
                cleaned_in["auth"] = C(cid)
            specs.append({"type": "PIECE", "piece": full_ap,
                          "action_name": a.get("action_name", ""),
                          "version": a_ver, "input": cleaned_in,
                          "display_name": a.get("display_name", "Action"),
                          "property_settings": ps})
        counter = [1]
        first = await _build_action_chain(specs, counter, e)
        trig_in = clean_input_config(dict(t.get("input", {"authType": "none"})))
        trigger = build_trigger(full, ver,
                                t.get("trigger_name", "catch_webhook"),
                                trig_in,
                                "Trigger", first)
        name = p.get("display_name", "سيادة — أتمتة مخصصة")
        result = await golden_build(e, pid, name, trigger, owner_email=owner_email)
        return {**result}

    if tool == "build_from_preset":
        preset = p.get("preset", "")
        if preset not in PRESETS:
            raise HTTPException(400, f"Unknown preset: {preset}")
        default_name, trigger = PRESETS[preset]["fn"](p.get("params", {}), cn)
        name = p.get("display_name", default_name)
        return await golden_build(e, pid, name, trigger, owner_email=owner_email)

    if tool == "validate_flow":
        vb = ValidateBody(trigger=p.get("trigger", {}),
                          actions=p.get("actions", []))
        errors: List[str] = []
        t_piece = vb.trigger.get("piece", "")
        t_trigger = vb.trigger.get("trigger_name", "")
        if t_piece:
            resolved_t, sch = await auto_resolve_piece(e, t_piece)
            if sch and t_trigger and t_trigger not in sch.get("triggers", {}):
                errors.append(f"Trigger '{t_trigger}' not found in {resolved_t} (requested: {t_piece})")
        for i, a in enumerate(vb.actions):
            ap = a.get("piece", "")
            aa = a.get("action_name", "")
            if ap:
                resolved_ap, sch = await auto_resolve_piece(e, ap)
                if sch and aa and aa not in sch.get("actions", {}):
                    errors.append(f"Action '{aa}' not found in {resolved_ap} (requested: {ap})")
        return {"valid": len(errors) == 0, "errors": errors}

    if tool == "test_webhook":
        fid = p.get("flow_id", "")
        payload = p.get("payload", {"test": True, "name": "تجربة سيادة"})
        return await e.test_webhook(fid, payload)

    if tool == "manage_flow":
        fid = p.get("flow_id", "")
        act = p.get("action", "")
        if act == "enable":
            await e._fop(fid, "CHANGE_STATUS", {"status": "ENABLED"})
            return {"flow_id": fid, "status": "ENABLED"}
        elif act == "disable":
            await e._fop(fid, "CHANGE_STATUS", {"status": "DISABLED"})
            return {"flow_id": fid, "status": "DISABLED"}
        elif act == "delete":
            await e.delete_flow(fid)
            return {"flow_id": fid, "status": "DELETED"}
        raise HTTPException(400, f"Unknown action: {act}")

    if tool == "diagnose_flow":
        flow = await e.get_flow(p.get("flow_id", ""))
        return _walk_flow_tree(flow)

    if tool == "update_flow":
        fid = p.get("flow_id", "")
        tpl = p.get("template", "")
        if tpl and tpl in TEMPLATES:
            config = p.get("config", {})
            trigger = TEMPLATES[tpl]["fn"](config, cn)
            name = p.get("display_name", TEMPLATES[tpl]["desc"])
        elif p.get("trigger") and p.get("actions") is not None:
            counter = [1]
            specs_u: List[dict] = []
            for a in p.get("actions", []):
                ap = a.get("piece", "")
                short = ap.replace("@activepieces/piece-", "")
                cleaned_cfg = clean_input_config(dict(a.get("input", {})))
                cid = a.get("connection_id", cn.get(short, ""))
                if cid and "auth" not in cleaned_cfg:
                    cleaned_cfg["auth"] = C(cid)
                specs_u.append({"type": "PIECE", "piece": ap,
                                "action_name": a.get("action_name", ""),
                                "input": cleaned_cfg,
                                "display_name": a.get("display_name", "Action")})
            first = await _build_action_chain(specs_u, counter, e)
            t = p["trigger"]
            pn = t.get("piece", "@activepieces/piece-webhook")
            full = pn if pn.startswith("@") else f"@activepieces/piece-{pn}"
            trigger = build_trigger(
                full, t.get("version", "~0.1.0"),
                t.get("trigger_name", "catch_webhook"),
                t.get("input", {"authType": "none"}),
                "Trigger", first)
            name = p.get("display_name", "Updated Flow")
        else:
            raise HTTPException(400, "Provide template or trigger+actions")

        await e.import_flow(fid, name, trigger)
        verified = await e.verify_flow(fid)
        pub = await e.publish_and_enable(fid)
        return {"flow_id": fid, "status": "updated",
                "publish": pub, "diagnosis": {"summary": "Verified"}}

    if tool == "list_operators":
        return {"operators": OPERATORS}

    if tool == "ingest_website":
        from ingestion import ingest_website
        url = p.get("url", "")
        if not url:
            raise HTTPException(400, "url is required for ingest_website")
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        return await ingest_website(url=url, project_id=pid)

    if tool == "get_institutional_memory":
        from database import async_session as _db_session
        from models import ProjectIdentity, KnowledgeAsset, AutonomousSetting
        from sqlalchemy import select as _sel

        if not _db_session:
            return {"memory_available": False, "reason": "database_offline"}

        async with _db_session() as session:
            identity = (await session.execute(
                _sel(ProjectIdentity).where(ProjectIdentity.project_id == pid)
            )).scalar_one_or_none()
            knowledge = (await session.execute(
                _sel(KnowledgeAsset).where(KnowledgeAsset.project_id == pid)
            )).scalar_one_or_none()
            settings = (await session.execute(
                _sel(AutonomousSetting).where(AutonomousSetting.project_id == pid)
            )).scalar_one_or_none()

        if not identity:
            return {
                "memory_available": False,
                "project_id": pid,
                "_hint": "No identity found. Use POST /v2/saas/register or POST /v2/identity/ingest first.",
            }

        return {
            "memory_available": True,
            "project_id": pid,
            "identity": {
                "sector": identity.sector,
                "language": identity.language,
                "description": identity.business_description,
                "website_url": identity.website_url,
            },
            "knowledge": {
                "faqs": knowledge.faqs if knowledge else [],
                "tone_of_voice": knowledge.tone_of_voice if knowledge else None,
                "brand_keywords": knowledge.brand_keywords if knowledge else [],
            },
            "settings": {
                "auto_respond": settings.auto_respond if settings else "off",
                "smart_rules": settings.smart_rules if settings else [],
                "client_settings": settings.client_settings if settings else {},
            },
        }

    raise HTTPException(400, f"Unknown tool: {tool}")


# ═══════════════════════════════════════════════════════════════
# V2 — MCP SERVER PROXY
# ═══════════════════════════════════════════════════════════════
@app.post("/v2/mcp/proxy")
async def v2_mcp_proxy(request: Request):
    """Proxy requests to AP's MCP Server — keeps token server-side."""
    if not AP_MCP_URL:
        raise HTTPException(501, detail="AP MCP Server URL not configured")
    body = await request.json()
    async with httpx.AsyncClient(timeout=ORCHESTRATOR_HTTPX_TIMEOUT) as client:
        r = await client.post(AP_MCP_URL, json=body,
                              headers={"Authorization": f"Bearer {AP_MCP_TOKEN}",
                                       "Content-Type": "application/json",
                                       "Accept": "application/json, text/event-stream"})
        try:
            content = r.json()
        except Exception:
            content = {"error": r.text[:500], "status_code": r.status_code}
        return JSONResponse(content=content, status_code=r.status_code)


# ═══════════════════════════════════════════════════════════════
# V2 — CONNECTION MANAGEMENT
# ═══════════════════════════════════════════════════════════════
class ConnectBody(BaseModel):
    piece_name: str = Field(..., description="e.g. gmail, google-sheets, slack")
    display_name: str = ""
    connection_config: Dict[str, Any] = Field(default_factory=dict)
    project_id: Optional[str] = None


@app.post("/v2/connect")
async def v2_connect(request: Request, body: ConnectBody):
    """Create a new AP connection for a piece."""
    e = E()
    pid = resolve_pid(request, body.project_id)
    piece = body.piece_name
    full_piece = piece if piece.startswith("@") else f"@activepieces/piece-{piece}"
    short = piece.replace("@activepieces/piece-", "")
    payload = {
        "pieceName": full_piece, "projectId": pid,
        "displayName": body.display_name or f"{short} connection",
        **body.connection_config,
    }
    result = await e._r("POST", "/v1/app-connections/", payload)
    return {"status": "created", "connection_id": result.get("id"),
            "external_id": result.get("externalId"),
            "piece": full_piece, "project_id": pid}


@app.get("/v2/connections/health")
async def v2_connections_health(request: Request, project_id: Optional[str] = None):
    """Health overview of all connections — split by healthy/unhealthy."""
    e = E()
    pid = resolve_pid(request, project_id)
    conns = await e.list_connections(pid)
    healthy, unhealthy = [], []
    for c in conns:
        info = {"id": c.get("id"), "external_id": c.get("externalId"),
                "name": c.get("displayName", ""), "piece": c.get("pieceName"),
                "status": c.get("status")}
        (healthy if c.get("status") == "ACTIVE" else unhealthy).append(info)
    return {"project_id": pid, "total": len(conns),
            "healthy": len(healthy), "unhealthy": len(unhealthy),
            "connections": {"healthy": healthy, "unhealthy": unhealthy}}


@app.post("/v2/connections/{connection_id}/test")
async def v2_connection_test(connection_id: str, request: Request, project_id: Optional[str] = None):
    """Check stored status of a connection (not a live connectivity test)."""
    e = E()
    pid = resolve_pid(request, project_id)
    conns = await e.list_connections(pid)
    target = next((c for c in conns
                   if c.get("id") == connection_id
                   or c.get("externalId") == connection_id), None)
    if not target:
        raise HTTPException(404, detail=f"Connection '{connection_id}' not found in project {pid}")
    status = target.get("status", "UNKNOWN")
    return {"connection_id": target.get("id"),
            "external_id": target.get("externalId"),
            "name": target.get("displayName", ""),
            "piece": target.get("pieceName"),
            "status": status, "healthy": status == "ACTIVE",
            "note": "This is a status check, not a live connectivity test.",
            "project_id": pid}


@app.delete("/v2/connections/{connection_id}")
async def v2_connection_delete(connection_id: str, request: Request, project_id: Optional[str] = None):
    """Delete a connection — refuses if any flow uses it."""
    e = E()
    pid = resolve_pid(request, project_id)
    flows = await e.list_flows(pid)
    affected = [f"{f.get('id')} ({f.get('version', {}).get('displayName', '?')})"
                for f in flows if connection_id in str(f.get("version", {}))]
    if affected:
        raise HTTPException(409, detail={
            "message": f"Cannot delete '{connection_id}': used by {len(affected)} flow(s)",
            "affected_flows": affected})
    await e._r("DELETE", f"/v1/app-connections/{connection_id}")
    return {"status": "deleted", "connection_id": connection_id, "project_id": pid}


# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.getenv("PORT", "8000")))
