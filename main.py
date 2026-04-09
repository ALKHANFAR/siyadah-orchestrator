"""
Siyadah Orchestrator v5.4.0 — Async Multi-Tenant Engine
========================================================
Golden Protocol v5 (Immunization): IMPORT_FLOW → deterministic webhook URL →
GET-verify → LOCK_AND_PUBLISH → ENABLE (strict GET confirmation).
Rule: propertySettings: {} is MANDATORY in every step settings.
Built on: 30 March 2026

Capabilities: ROUTER, LOOP, CODE, PIECE, PRESETS, SMART_SCHEMA,
              Multi-Tenancy, MCP Execute, Re-import, Diagnose
"""
from __future__ import annotations
import asyncio, logging, os, re, time as _time, traceback
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import httpx, uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
log = logging.getLogger("siyadah")

VERSION = "5.4.0"
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
AP_MCP_TOKEN = os.getenv("AP_MCP_TOKEN", "")
ORCHESTRATOR_HTTPX_TIMEOUT = int(os.getenv("ORCHESTRATOR_HTTPX_TIMEOUT", "120"))

PIECE_VERSIONS: Dict[str, str] = {
    "webhook": "~0.1.31",
    "gmail": "~0.11.6",
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
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                timeout=ORCHESTRATOR_HTTPX_TIMEOUT,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _r(self, method: str, path: str, body=None, params=None):
        client = await self._ensure_client()
        url = f"{self.base}/api{path}"
        try:
            r = await client.request(method, url, json=body, params=params)
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
                    log.info("[engine] Re-auth successful, retried %s %s → %s", method, path, r.status_code)
            except Exception as auth_err:
                log.error("[engine] Re-auth failed: %s", auth_err)
        if not r.is_success:
            raise HTTPException(r.status_code, detail=r.text[:500])
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

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

    async def list_runs(self, pid: str):
        d = await self._r("GET", "/v1/flow-runs/",
                          params={"projectId": pid, "limit": "50"})
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
        log.warning("Schema fetch failed for %s: %s", api_name, e)
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
    1. Any value containing ``{{…}}`` (string, array, dict) → SHORT_TEXT / CUSTOM_INPUT
    2. DROPDOWN / MULTI_SELECT_DROPDOWN → CUSTOM_INPUT
    3. DYNAMIC → CUSTOM_INPUT
    4. STATIC_DROPDOWN → type marker only
    """
    if not props:
        return {}
    settings: Dict[str, Any] = {}
    for pname, pinfo in props.items():
        if pname == "auth" or pname not in input_config:
            continue
        val = input_config.get(pname)
        if _contains_dynamic_ref(val):
            settings[pname] = {"type": "SHORT_TEXT", "status": "CUSTOM_INPUT"}
            continue
        ptype = pinfo.get("type", "") if isinstance(pinfo, dict) else str(pinfo)
        if ptype in ("DROPDOWN", "MULTI_SELECT_DROPDOWN"):
            settings[pname] = {"type": ptype, "status": "CUSTOM_INPUT"}
        elif ptype == "DYNAMIC":
            settings[pname] = {"type": "DYNAMIC", "status": "CUSTOM_INPUT"}
        elif ptype == "STATIC_DROPDOWN":
            settings[pname] = {"type": "STATIC_DROPDOWN"}
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
        "name": "trigger", "valid": True, "displayName": display,
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
        "name": sname, "valid": True, "displayName": display,
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
        "name": name, "type": "ROUTER", "valid": True,
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
    for spec in reversed(specs):
        sdict = spec if isinstance(spec, dict) else (
            spec.model_dump() if hasattr(spec, "model_dump") else spec)
        if not isinstance(sdict, dict):
            raise TypeError(f"Chain step must be dict, got {type(spec).__name__}")
        chain = await _build_step_from_spec(
            sdict, counter, engine, next_action=chain, steps_info=steps_info)
    return chain


# ═══════════════════════════════════════════════════════════════
# GOLDEN PROTOCOL PIPELINE
# ═══════════════════════════════════════════════════════════════
async def golden_build(engine: SiyadahEngine, pid: str, name: str,
                       trigger: dict, *, self_test: bool = True) -> dict:
    """Full Golden Protocol: IMPORT_FLOW → GET-verify → LOCK_AND_PUBLISH → ENABLE."""
    flow = await engine.create_flow(pid, name)
    fid = flow["id"]
    log.info("[golden] Created flow %s", fid)

    await engine.import_flow(fid, name, trigger)
    webhook_url = f"https://activepieces-production-2499.up.railway.app/api/v1/webhooks/{fid}"
    log.info("[golden] IMPORT_FLOW → %s", fid)

    verified = await engine.verify_flow(fid)
    ttype = verified.get("version", {}).get("trigger", {}).get("type", "?")
    log.info("[golden] Verified %s → trigger=%s", fid, ttype)

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

    diagnosis = None
    if self_test:
        diagnosis = _walk_flow_tree(verified)

    return {"flow_id": fid, "trigger_type": ttype,
            "publish": pub, "diagnosis": diagnosis,
            "webhook_url": webhook_url}


# ═══════════════════════════════════════════════════════════════
# DIAGNOSE — walk the flow tree
# ═══════════════════════════════════════════════════════════════
def _walk_flow_tree(flow_data: dict) -> dict:
    """Walk the version→trigger tree and enumerate all steps."""
    version = flow_data.get("version", {})
    trigger = version.get("trigger", {})
    steps: List[dict] = []

    def walk(node, depth=0):
        if not node:
            return
        info: Dict[str, Any] = {
            "name": node.get("name"), "type": node.get("type"),
            "displayName": node.get("displayName"), "depth": depth,
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
    log.info("Siyadah Orchestrator v%s starting", VERSION)
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
    yield
    if _engine:
        await _engine.close()


app = FastAPI(title="Siyadah Orchestrator", version=VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def api_key_check(request: Request, call_next):
    if ORCH_API_KEY and request.url.path.startswith("/v2/"):
        key = request.headers.get("X-API-Key", "")
        if key != ORCH_API_KEY:
            return JSONResponse(status_code=401,
                                content={"error": "Invalid or missing API key"})
    return await call_next(request)


# ═══════════════════════════════════════════════════════════════
# BACKWARD-COMPATIBLE ENDPOINTS
# ═══════════════════════════════════════════════════════════════
@app.get("/")
async def root():
    return {
        "service": "Siyadah Orchestrator", "version": VERSION,
        "protocol": "Golden v4: IMPORT→VERIFY→LOCK→ENABLE",
        "templates": len(TEMPLATES), "presets": list(PRESETS.keys()),
        "capabilities": ["ROUTER", "LOOP", "CODE", "PIECE", "PRESETS",
                         "SMART_SCHEMA", "MULTI_TENANT", "MCP_EXECUTE"],
        "project_id": DEFAULT_PID,
        "mcp_proxy": bool(AP_MCP_URL),
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
async def v2_build(body: BuildBody):
    e = E()
    t = body.template
    if t not in TEMPLATES:
        raise HTTPException(400, detail=f"Unknown template: {t}. Available: {list(TEMPLATES.keys())}")
    tdef = TEMPLATES[t]
    missing = [k for k in tdef["req"] if k not in body.config]
    if missing:
        raise HTTPException(422, detail=f"Missing config: {missing}")

    pid = body.project_id or DEFAULT_PID
    cn = resolve_conns(body.connection_ids)

    await guard_connections(e, pid, ["gmail", "google-sheets"], cn, strict=True)

    log.info("BUILD template=%s name=%s pid=%s", t, body.display_name or t, pid)
    trigger = tdef["fn"](body.config, cn)
    result = await golden_build(e, pid, body.display_name or tdef["desc"], trigger)

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
async def v2_build_dynamic(body: DynamicBuildBody):
    e = E()
    pid = body.project_id or DEFAULT_PID
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

    specs_for_chain: List[dict] = []
    for a in body.actions:
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
        inp = dict(cleaned_in)
        conn_id = a.get("connection_id", cn.get(short, ""))
        if conn_id:
            inp["auth"] = C(conn_id)
        specs_for_chain.append({
            "type": "PIECE", "piece": full_ap,
            "action_name": a.get("action_name", ""),
            "version": a_ver, "input": inp,
            "display_name": a.get("display_name", f"Action"),
            "property_settings": ps,
        })

    counter = [1]
    first_action = await _build_action_chain(specs_for_chain, counter, e)
    trigger = build_trigger(
        full_t,
        trigger_ver, trigger_name, trigger_input,
        body.display_name + " — Trigger", first_action)

    result = await golden_build(e, pid, body.display_name, trigger)

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
async def v2_build_router(body: RouterBuildBody):
    e = E()
    pid = body.project_id or DEFAULT_PID
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

    result = await golden_build(e, pid, body.display_name, trigger)
    return {"status": "deployed", "flow_id": result["flow_id"],
            "type": "ROUTER", "branches_count": len(branch_defs),
            "webhook_url": result.get("webhook_url"),
            "publish": result["publish"],
            "diagnosis": result.get("diagnosis")}


# ═══════════════════════════════════════════════════════════════
# V2 — LOOP BUILDER
# ═══════════════════════════════════════════════════════════════
@app.post("/v2/build-loop")
async def v2_build_loop(body: LoopBuildBody):
    e = E()
    pid = body.project_id or DEFAULT_PID
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
    result = await golden_build(e, pid, body.display_name, trigger)
    return {"status": "deployed", "flow_id": result["flow_id"],
            "type": "LOOP", "items_expression": body.items_expression,
            "webhook_url": result.get("webhook_url"),
            "publish": result["publish"],
            "diagnosis": result.get("diagnosis")}


# ═══════════════════════════════════════════════════════════════
# V2 — COMPLEX BUILDER (any mix)
# ═══════════════════════════════════════════════════════════════
@app.post("/v2/build-complex")
async def v2_build_complex(body: ComplexBuildBody):
    e = E()
    pid = body.project_id or DEFAULT_PID
    cn = resolve_conns(body.connection_ids)
    validate_complex_steps(body.steps)

    required = _extract_pieces_from_steps(body.steps)
    await guard_connections(e, pid, required, cn, strict=True)

    counter = [1]
    steps_info: List[dict] = []
    try:
        first_action = await _build_action_chain(
            body.steps, counter, e, steps_info)
        trigger = wh_trigger("استقبال بيانات", first_action)
        result = await golden_build(e, pid, body.display_name, trigger)
        if not isinstance(result, dict) or not result.get("flow_id"):
            raise HTTPException(
                500,
                detail={
                    "message": "golden_build returned an invalid payload",
                    "diagnosis": repr(result),
                },
            )
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
    ordered = _sort_steps_info_chronological(steps_info)
    return {"status": "deployed", "flow_id": result["flow_id"],
            "type": "COMPLEX", "steps": ordered,
            "webhook_url": result.get("webhook_url"),
            "publish": result["publish"],
            "diagnosis": result.get("diagnosis")}


# ═══════════════════════════════════════════════════════════════
# V2 — PRESET BUILDER
# ═══════════════════════════════════════════════════════════════
@app.get("/v2/presets")
async def v2_presets():
    return {"presets": {k: v["desc"] for k, v in PRESETS.items()}}


@app.post("/v2/build-preset")
async def v2_build_preset(body: PresetBuildBody):
    e = E()
    if body.preset not in PRESETS:
        raise HTTPException(400, detail=f"Unknown preset: {body.preset}. Available: {list(PRESETS.keys())}")
    pid = body.project_id or DEFAULT_PID
    cn = resolve_conns(body.connection_ids)
    pdef = PRESETS[body.preset]
    default_name, trigger = pdef["fn"](body.params, cn)
    name = body.display_name or default_name
    result = await golden_build(e, pid, name, trigger)
    return {"status": "deployed", "flow_id": result["flow_id"],
            "preset": body.preset, "display_name": name,
            "webhook_url": result.get("webhook_url"),
            "publish": result["publish"],
            "diagnosis": result.get("diagnosis")}


# ═══════════════════════════════════════════════════════════════
# V2 — SMART BUILDER (schema-validated propertySettings)
# ═══════════════════════════════════════════════════════════════
@app.post("/v2/build-smart")
async def v2_build_smart(body: SmartBuildBody):
    e = E()
    pid = body.project_id or DEFAULT_PID
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

        sname = f"step_{counter[0]}"
        counter[0] += 1
        inp = dict(cleaned_cfg)
        short = resolved_piece.replace("@activepieces/piece-", "")
        conn_id = cn.get(short, "")
        if conn_id and "auth" not in inp:
            inp["auth"] = C(conn_id)

        full = (resolved_piece if resolved_piece.startswith("@")
                else f"@activepieces/piece-{resolved_piece}")
        step = build_action(sname, full, ver, resolved_action, inp,
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
    result = await golden_build(e, pid, body.display_name, trigger)
    return {"status": "deployed", "flow_id": result["flow_id"],
            "type": "SMART", "steps": steps_info,
            "webhook_url": result.get("webhook_url"),
            "publish": result["publish"],
            "diagnosis": result.get("diagnosis")}


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

    return {"valid": len(errors) == 0, "errors": errors,
            "steps_count": 1 + len(body.actions), "pieces_used": pieces_used}


# ═══════════════════════════════════════════════════════════════
# V2 — CLIENT STATUS
# ═══════════════════════════════════════════════════════════════
@app.get("/v2/client-status")
async def v2_status():
    e = E()
    flows = await e.list_flows(DEFAULT_PID)
    conns = await e.list_connections(DEFAULT_PID)
    runs = await e.list_runs(DEFAULT_PID)
    active = [f for f in flows if f.get("status") == "ENABLED"]
    failed = [r for r in runs if r.get("status") == "FAILED"]
    recent = sorted(runs, key=lambda r: r.get("created", ""), reverse=True)[:10]
    return {
        "project_id": DEFAULT_PID,
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
async def v2_flow_patch(flow_id: str, body: FlowPatchBody):
    e = E()
    if body.action == "enable":
        await e._fop(flow_id, "CHANGE_STATUS", {"status": "ENABLED"})
        return {"flow_id": flow_id, "status": "ENABLED"}
    elif body.action == "disable":
        await e._fop(flow_id, "CHANGE_STATUS", {"status": "DISABLED"})
        return {"flow_id": flow_id, "status": "DISABLED"}
    elif body.action == "delete":
        await e.delete_flow(flow_id)
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
async def v2_reimport(flow_id: str, body: ReimportBody):
    """Re-import flow with new structure. Golden Protocol on existing flow."""
    e = E()
    pid = body.project_id or DEFAULT_PID
    cn = resolve_conns(body.connection_ids)

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
                inp = dict(a.get("input", {}))
                conn_id = a.get("connection_id", cn.get(short, ""))
                if conn_id and "auth" not in inp:
                    inp["auth"] = C(conn_id)
                specs.append({
                    "type": "PIECE", "piece": a_piece,
                    "action_name": a.get("action_name", ""),
                    "version": a.get("version", PIECE_VERSIONS.get(short, "~0.1.0")),
                    "input": inp,
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

        diagnosis = _walk_flow_tree(final)
        return {"status": "updated", "flow_id": flow_id,
                "display_name": name, "publish": pub,
                "diagnosis": diagnosis}
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
                "diagnosis": diag,
            },
        ) from ex


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
    e = E()
    body = await request.json()
    name = body.get("client_name", "New Client")
    return {"status": "created",
            "project": await e._r("POST", "/v1/projects/",
                                  {"displayName": name})}


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
    ]}


@app.post("/v2/mcp/execute")
async def v2_mcp_execute(body: MCPExecuteBody):
    """Live MCP Tool Dispatcher — Claude calls tools here."""
    e = E()
    tool = body.tool
    p = body.parameters
    pid = body.project_id or p.get("project_id") or DEFAULT_PID
    cn = resolve_conns(body.connection_ids or p.get("connection_ids"))

    try:
        result = await _mcp_dispatch(e, tool, p, pid, cn)
        return {"tool": tool, "success": True, "result": result}
    except HTTPException as he:
        return {"tool": tool, "success": False,
                "error": he.detail, "status_code": he.status_code}
    except Exception as ex:
        return {"tool": tool, "success": False, "error": str(ex)}


async def _mcp_dispatch(e: SiyadahEngine, tool: str, p: dict,
                        pid: str, cn: Dict[str, str]) -> Any:
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
        trigger = tdef["fn"](config, cn)
        name = p.get("display_name", tdef["desc"])
        result = await golden_build(e, pid, name, trigger)
        wh = (f"{AP_BASE}/api/v1/webhooks/{result['flow_id']}"
              if not tpl.startswith("scheduled") else None)
        return {**result, "webhook_url": wh, "template": tpl}

    if tool == "build_dynamic_flow":
        t = p.get("trigger", {})
        piece_name = t.get("piece", "@activepieces/piece-webhook")
        resolved_t, t_schema = await auto_resolve_piece(e, piece_name)
        full = resolved_t if resolved_t.startswith("@") else f"@activepieces/piece-{resolved_t}"
        ver = resolve_piece_version(t_schema, resolved_t)
        specs: List[dict] = []
        for a in p.get("actions", []):
            ap = a.get("piece", "")
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
            inp = dict(cleaned_in)
            cid = a.get("connection_id", cn.get(short, ""))
            if cid and "auth" not in inp:
                inp["auth"] = C(cid)
            specs.append({"type": "PIECE", "piece": full_ap,
                          "action_name": a.get("action_name", ""),
                          "version": a_ver, "input": inp,
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
        result = await golden_build(e, pid, name, trigger)
        return {**result}

    if tool == "build_from_preset":
        preset = p.get("preset", "")
        if preset not in PRESETS:
            raise HTTPException(400, f"Unknown preset: {preset}")
        default_name, trigger = PRESETS[preset]["fn"](p.get("params", {}), cn)
        name = p.get("display_name", default_name)
        return await golden_build(e, pid, name, trigger)

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
                inp = dict(a.get("input", {}))
                cid = a.get("connection_id", cn.get(short, ""))
                if cid and "auth" not in inp:
                    inp["auth"] = C(cid)
                specs_u.append({"type": "PIECE", "piece": ap,
                                "action_name": a.get("action_name", ""),
                                "input": inp,
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
                "publish": pub, "diagnosis": _walk_flow_tree(verified)}

    if tool == "list_operators":
        return {"operators": OPERATORS}

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
async def v2_connect(body: ConnectBody):
    """Create a new AP connection for a piece."""
    e = E()
    pid = body.project_id or DEFAULT_PID
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
async def v2_connections_health(project_id: Optional[str] = None):
    """Health overview of all connections — split by healthy/unhealthy."""
    e = E()
    pid = project_id or DEFAULT_PID
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
async def v2_connection_test(connection_id: str, project_id: Optional[str] = None):
    """Check stored status of a connection (not a live connectivity test)."""
    e = E()
    pid = project_id or DEFAULT_PID
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
async def v2_connection_delete(connection_id: str, project_id: Optional[str] = None):
    """Delete a connection — refuses if any flow uses it."""
    e = E()
    pid = project_id or DEFAULT_PID
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
