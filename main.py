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
import asyncio, json, logging, os, re, time as _time, traceback
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

            # ── Smart Auto-Fill: populate missing required fields ──
            if props:
                for _fn, _fi in props.items():
                    if _fn == "auth":
                        continue
                    if (isinstance(_fi, dict) and _fi.get("required", False)
                            and (_fn not in cleaned_cfg
                                 or cleaned_cfg[_fn] in (None, "", []))):
                        _ptype = _fi.get("type", "")
                        if _ptype == "BOOLEAN":
                            cleaned_cfg[_fn] = False
                        elif _ptype == "NUMBER":
                            cleaned_cfg[_fn] = 0
                        elif _ptype == "ARRAY":
                            cleaned_cfg[_fn] = []
                        elif _ptype in ("STATIC_DROPDOWN", "DROPDOWN"):
                            _opts = _fi.get("options", [])
                            if isinstance(_opts, list) and _opts:
                                _first = _opts[0]
                                cleaned_cfg[_fn] = _first.get("value", _first) if isinstance(_first, dict) else _first
                            else:
                                cleaned_cfg[_fn] = ""
                        else:
                            cleaned_cfg[_fn] = "Siyadah Auto-Fill"
                        log.info("[auto-fill] %s.%s → %r (type=%s)",
                                 sname, _fn, cleaned_cfg[_fn], _ptype)

            # ── Draft Guard: inject missing boolean fields ──
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
                       trigger: dict, *, self_test: bool = True) -> dict:
    """Full Golden Protocol: IMPORT_FLOW → GET-verify → LOCK_AND_PUBLISH → ENABLE."""
    # ── Visibility Guard: unique timestamp + fallback project ──
    name = f"{name} ({datetime.now().strftime('%H:%M:%S')})"
    pid = pid or DEFAULT_PID

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

    return {"flow_id": fid, "trigger_type": ttype,
            "publish": pub, "diagnosis": diagnosis,
            "webhook_url": webhook_url,
            "resource_link": resource_link,
            "pulse_sent": pulse_sent,
            "client_email": client_email}


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


# ═══════════════════════════════════════════════════════════════
# SAICO INSURANCE COLLECTION — Sarah voice agent pipeline
# ═══════════════════════════════════════════════════════════════
# T_SAICO_TRIGGER: CRM/campaign webhook → enrich lead → log attempt →
#   send WhatsApp warm-up → fire Sondos outbound call with Sarah skill.
# T_SAICO_OUTCOME: Sondos post-call webhook → parse outcome → log →
#   ROUTER(5): paid_full | plan_or_promise | callback_scheduled |
#   transferred | fallback(no_contact/refused).
# Arabic collection skill (system prompt, KB, objection matrix) lives
# in skills/saico_collection_sarah.md and is loaded into Sondos, not AP.

_SAICO_ENRICH_CODE = """
export const code = async (inputs) => {
  const b = inputs.body || {};
  const amount = Number(b.outstanding_amount || 0);
  const days = Number(b.days_overdue || 0);
  const attempt = Number(b.contact_attempt || 1);
  let urgency = 'NORMAL';
  if (amount >= 20000 || days >= 60 || attempt >= 3) urgency = 'HIGH';
  if (amount >= 50000 || days >= 120) urgency = 'CRITICAL';
  const min_down = Number(inputs.min_down_payment_pct || 30);
  const max_inst = Number(inputs.max_installments || 6);
  const disc_pct = Number(inputs.discount_authority_pct || 10);
  const down_payment = Math.round(amount * (min_down / 100));
  const monthly = max_inst > 0 ? Math.round((amount - down_payment) / max_inst) : 0;
  const discounted = Math.round(amount * (1 - disc_pct / 100));
  return {
    ok: true,
    customer_name: b.customer_name || '',
    customer_phone: b.customer_phone || '',
    id_last4: b.id_last4 || '',
    debt_type: b.debt_type || 'recourse',
    outstanding_amount: amount,
    policy_number: b.policy_number || '',
    claim_number: b.claim_number || 'N/A',
    incident_date: b.incident_date || '',
    vehicle_plate: b.vehicle_plate || '',
    days_overdue: days,
    contact_attempt: attempt,
    previous_promise: b.previous_promise || 'none',
    iban: inputs.iban || b.iban || '',
    bank_name: inputs.bank_name || b.bank_name || '',
    max_installments: max_inst,
    min_down_payment_pct: min_down,
    discount_authority_pct: disc_pct,
    down_payment_amount: down_payment,
    monthly_installment: monthly,
    discounted_amount: discounted,
    whatsapp_link: inputs.whatsapp_link || '',
    payment_link: inputs.payment_link || '',
    agent_transfer_number: inputs.agent_transfer_number || '',
    complaint_channel: inputs.complaint_channel || '8001242002',
    call_time_window: inputs.call_time_window || '09:00-21:00',
    call_datetime: new Date().toISOString(),
    language: 'ar-SA',
    urgency,
    lead_id: b.lead_id || (b.policy_number + '-' + Date.now())
  };
};
"""

_SAICO_WHATSAPP_WARMUP_CODE = """
export const code = async (inputs) => {
  if (!inputs.whatsapp_api_url || !inputs.whatsapp_token) {
    return { skipped: true, reason: 'no_whatsapp_config' };
  }
  const msg = 'أستاذ/ة ' + inputs.customer_name + '، سيتم التواصل معك خلال دقائق من سايكو ' +
              'بخصوص الوثيقة ' + inputs.policy_number + '. نتطلع لمساعدتك على إغلاق الموضوع بيسر. — سايكو 8001242002';
  try {
    const r = await fetch(inputs.whatsapp_api_url, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + inputs.whatsapp_token,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        to: inputs.customer_phone,
        type: 'template',
        template: {
          name: inputs.template_name || 'saico_precall_warmup',
          language: { code: 'ar' },
          components: [{ type: 'body', parameters: [
            { type: 'text', text: inputs.customer_name },
            { type: 'text', text: inputs.policy_number }
          ]}]
        },
        fallback_text: msg
      })
    });
    return { sent: r.ok, status: r.status, to: inputs.customer_phone };
  } catch (e) {
    return { sent: false, error: String(e) };
  }
};
"""

_SAICO_SONDOS_CALL_CODE = """
export const code = async (inputs) => {
  if (!inputs.sondos_base_url || !inputs.sondos_api_key || !inputs.sondos_assistant_id) {
    return { queued: false, error: 'missing_sondos_config' };
  }
  const payload = {
    assistant_id: inputs.sondos_assistant_id,
    phone_number: inputs.customer_phone,
    language: 'ar-SA',
    max_duration_sec: 420,
    metadata: { lead_id: inputs.lead_id, urgency: inputs.urgency },
    variables: {
      customer_name: inputs.customer_name,
      customer_phone: inputs.customer_phone,
      id_last4: inputs.id_last4,
      debt_type: inputs.debt_type,
      outstanding_amount: String(inputs.outstanding_amount),
      policy_number: inputs.policy_number,
      claim_number: inputs.claim_number,
      incident_date: inputs.incident_date,
      vehicle_plate: inputs.vehicle_plate,
      days_overdue: String(inputs.days_overdue),
      contact_attempt: String(inputs.contact_attempt),
      previous_promise: inputs.previous_promise,
      iban: inputs.iban,
      bank_name: inputs.bank_name,
      max_installments: String(inputs.max_installments),
      min_down_payment_pct: String(inputs.min_down_payment_pct),
      discount_authority_pct: String(inputs.discount_authority_pct),
      whatsapp_link: inputs.whatsapp_link,
      payment_link: inputs.payment_link,
      agent_transfer_number: inputs.agent_transfer_number,
      complaint_channel: inputs.complaint_channel,
      call_time_window: inputs.call_time_window,
      call_datetime: inputs.call_datetime,
      language: inputs.language
    }
  };
  try {
    const r = await fetch(inputs.sondos_base_url + '/v1/calls/outbound', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + inputs.sondos_api_key,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(payload)
    });
    const data = await r.json().catch(() => ({}));
    return {
      queued: r.ok,
      status: r.status,
      call_id: data.call_id || data.id || null,
      lead_id: inputs.lead_id,
      phone: inputs.customer_phone,
      urgency: inputs.urgency
    };
  } catch (e) {
    return { queued: false, error: String(e) };
  }
};
"""

_SAICO_OUTCOME_PARSE_CODE = """
export const code = async (inputs) => {
  const b = inputs.body || {};
  const o = (b.outcome || '').toLowerCase();
  const normalized = ['paid_full','paid_partial','plan_agreed','promise_to_pay',
                      'callback_scheduled','transferred','no_contact','refused']
                     .includes(o) ? o : 'no_contact';
  return {
    call_id: b.call_id || '',
    lead_id: b.customer_id || b.lead_id || '',
    policy_number: b.policy_number || '',
    customer_name: b.customer_name || '',
    customer_phone: b.customer_phone || '',
    outcome: normalized,
    committed_amount: Number(b.committed_amount || 0),
    committed_date: b.committed_date || '',
    ladder_reached: Number(b.commitment_ladder_reached || 0),
    objections: Array.isArray(b.objections_raised) ? b.objections_raised.join(' | ') : '',
    duration_sec: Number(b.call_duration_sec || 0),
    transcript_url: b.transcript_url || '',
    iban: inputs.iban || '',
    bank_name: inputs.bank_name || '',
    payment_link: inputs.payment_link || '',
    recorded_at: new Date().toISOString()
  };
};
"""

_SAICO_WHATSAPP_PAYMENT_CODE = """
export const code = async (inputs) => {
  if (!inputs.whatsapp_api_url || !inputs.whatsapp_token) {
    return { skipped: true, reason: 'no_whatsapp_config' };
  }
  const body = 'أستاذ/ة ' + inputs.customer_name + '، شكراً لوقتك. ' +
               'الالتزام: ' + inputs.outcome + ' بمبلغ ' + inputs.committed_amount + ' ريال' +
               (inputs.committed_date ? ' بتاريخ ' + inputs.committed_date : '') + '.\\n' +
               'للسداد: آيبان ' + inputs.iban + ' — ' + inputs.bank_name +
               (inputs.payment_link ? '\\nرابط سداد مباشر: ' + inputs.payment_link : '') +
               '\\nالمرجع: ' + inputs.policy_number + ' — سايكو 8001242002';
  try {
    const r = await fetch(inputs.whatsapp_api_url, {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + inputs.whatsapp_token,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        to: inputs.customer_phone,
        type: 'text',
        text: { body }
      })
    });
    return { sent: r.ok, status: r.status };
  } catch (e) {
    return { sent: false, error: String(e) };
  }
};
"""


def T_SAICO_TRIGGER(c, cn):
    """Outbound collection: webhook lead → enrich → log → warm-up WhatsApp → Sondos call."""
    sid = c.get("spreadsheet_id", "")

    sondos_input = {
        "body": "{{trigger['body']}}",
        "sondos_base_url": c.get("sondos_base_url", ""),
        "sondos_api_key": c.get("sondos_api_key", ""),
        "sondos_assistant_id": c.get("sondos_assistant_id", ""),
        "lead_id": "{{step_1['lead_id']}}",
        "urgency": "{{step_1['urgency']}}",
        "customer_name": "{{step_1['customer_name']}}",
        "customer_phone": "{{step_1['customer_phone']}}",
        "id_last4": "{{step_1['id_last4']}}",
        "debt_type": "{{step_1['debt_type']}}",
        "outstanding_amount": "{{step_1['outstanding_amount']}}",
        "policy_number": "{{step_1['policy_number']}}",
        "claim_number": "{{step_1['claim_number']}}",
        "incident_date": "{{step_1['incident_date']}}",
        "vehicle_plate": "{{step_1['vehicle_plate']}}",
        "days_overdue": "{{step_1['days_overdue']}}",
        "contact_attempt": "{{step_1['contact_attempt']}}",
        "previous_promise": "{{step_1['previous_promise']}}",
        "iban": "{{step_1['iban']}}",
        "bank_name": "{{step_1['bank_name']}}",
        "max_installments": "{{step_1['max_installments']}}",
        "min_down_payment_pct": "{{step_1['min_down_payment_pct']}}",
        "discount_authority_pct": "{{step_1['discount_authority_pct']}}",
        "whatsapp_link": "{{step_1['whatsapp_link']}}",
        "payment_link": "{{step_1['payment_link']}}",
        "agent_transfer_number": "{{step_1['agent_transfer_number']}}",
        "complaint_channel": "{{step_1['complaint_channel']}}",
        "call_time_window": "{{step_1['call_time_window']}}",
        "call_datetime": "{{step_1['call_datetime']}}",
        "language": "{{step_1['language']}}",
    }
    s4 = build_code_step("step_4", "إطلاق مكالمة سندس", _SAICO_SONDOS_CALL_CODE, sondos_input)

    warmup_input = {
        "whatsapp_api_url": c.get("whatsapp_api_url", ""),
        "whatsapp_token": c.get("whatsapp_token", ""),
        "template_name": c.get("whatsapp_warmup_template", "saico_precall_warmup"),
        "customer_name": "{{step_1['customer_name']}}",
        "customer_phone": "{{step_1['customer_phone']}}",
        "policy_number": "{{step_1['policy_number']}}",
    }
    s3 = build_code_step("step_3", "وتساب تمهيدي", _SAICO_WHATSAPP_WARMUP_CODE,
                         warmup_input, next_action=s4)

    if sid:
        s2 = sheet_add("step_2", cn["google-sheets"], sid, {
            "A": "{{step_1['lead_id']}}",
            "B": "{{step_1['customer_name']}}",
            "C": "{{step_1['customer_phone']}}",
            "D": "{{step_1['policy_number']}}",
            "E": "{{step_1['outstanding_amount']}}",
            "F": "{{step_1['days_overdue']}}",
            "G": "{{step_1['contact_attempt']}}",
            "H": "{{step_1['urgency']}}",
            "I": "{{step_1['call_datetime']}}",
            "J": "queued",
        }, "تسجيل محاولة التحصيل", next_action=s3)
    else:
        s2 = build_code_step("step_2", "تسجيل داخلي",
            'export const code = async (i) => ({ logged: true, lead_id: i.lead_id });',
            {"lead_id": "{{step_1['lead_id']}}"}, next_action=s3)

    enrich_input = {
        "body": "{{trigger['body']}}",
        "iban": c.get("iban", ""),
        "bank_name": c.get("bank_name", ""),
        "max_installments": c.get("max_installments", 6),
        "min_down_payment_pct": c.get("min_down_payment_pct", 30),
        "discount_authority_pct": c.get("discount_authority_pct", 10),
        "whatsapp_link": c.get("whatsapp_link", ""),
        "payment_link": c.get("payment_link", ""),
        "agent_transfer_number": c.get("agent_transfer_number", ""),
        "complaint_channel": c.get("complaint_channel", "8001242002"),
        "call_time_window": c.get("call_time_window", "09:00-21:00"),
    }
    s1 = build_code_step("step_1", "إثراء الليد + حساب الضغط",
                         _SAICO_ENRICH_CODE, enrich_input, next_action=s2)

    return wh_trigger("استقبال ليد تحصيل سايكو", s1)


def T_SAICO_OUTCOME(c, cn):
    """Post-call router: parse Sondos outcome → log → branch to follow-up action."""
    sid = c.get("spreadsheet_id", "")
    supervisor = c.get("supervisor_email", "collections@saico.com.sa")
    paid_email = c.get("paid_confirmation_email", supervisor)

    # --- Branch A: paid_full — thank-you email (closed) ---
    paid = gmail_send("step_4", cn["gmail"], [paid_email],
        "سداد كامل: {{step_1['customer_name']}} — {{step_1['policy_number']}}",
        "تم السداد الكامل عبر المكالمة.\n"
        "العميل: {{step_1['customer_name']}}\n"
        "الوثيقة: {{step_1['policy_number']}}\n"
        "المبلغ: {{step_1['committed_amount']}} ريال\n"
        "معرف المكالمة: {{step_1['call_id']}}\n"
        "التسجيل: {{step_1['transcript_url']}}\n\n— سايكو",
        "تأكيد سداد للمشرف")

    # --- Branch B: plan_agreed / promise_to_pay — WhatsApp with IBAN + link ---
    plan_wh_input = {
        "whatsapp_api_url": c.get("whatsapp_api_url", ""),
        "whatsapp_token": c.get("whatsapp_token", ""),
        "customer_name": "{{step_1['customer_name']}}",
        "customer_phone": "{{step_1['customer_phone']}}",
        "policy_number": "{{step_1['policy_number']}}",
        "outcome": "{{step_1['outcome']}}",
        "committed_amount": "{{step_1['committed_amount']}}",
        "committed_date": "{{step_1['committed_date']}}",
        "iban": "{{step_1['iban']}}",
        "bank_name": "{{step_1['bank_name']}}",
        "payment_link": "{{step_1['payment_link']}}",
    }
    plan_wh = build_code_step("step_5", "وتساب تفاصيل السداد",
                              _SAICO_WHATSAPP_PAYMENT_CODE, plan_wh_input)

    # --- Branch C: callback_scheduled — log callback for scheduled retry ---
    if sid:
        cb = sheet_add("step_6", cn["google-sheets"], sid, {
            "A": "{{step_1['lead_id']}}",
            "B": "{{step_1['customer_name']}}",
            "C": "{{step_1['customer_phone']}}",
            "D": "{{step_1['policy_number']}}",
            "E": "{{step_1['committed_date']}}",
            "F": "callback_scheduled",
            "G": "{{step_1['call_id']}}",
        }, "حفظ موعد الاتصال")
    else:
        cb = build_code_step("step_6", "حفظ موعد (داخلي)",
            'export const code = async (i) => ({ scheduled: true, when: i.when });',
            {"when": "{{step_1['committed_date']}}"})

    # --- Branch D: transferred — alert supervisor for human handoff ---
    xfer = gmail_send("step_7", cn["gmail"], [supervisor],
        "تحويل للبشري: {{step_1['customer_name']}} — {{step_1['policy_number']}}",
        "تم تحويل المكالمة لمختص بشري.\n"
        "العميل: {{step_1['customer_name']}}\n"
        "الجوال: {{step_1['customer_phone']}}\n"
        "الوثيقة: {{step_1['policy_number']}}\n"
        "الاعتراضات: {{step_1['objections']}}\n"
        "التسجيل: {{step_1['transcript_url']}}\n\nيرجى الاتصال خلال ساعة.",
        "تنبيه تحويل عاجل")

    # --- Branch E (FALLBACK): no_contact / refused — pressure WhatsApp + supervisor email ---
    pressure_input = dict(plan_wh_input)
    pressure_input["committed_amount"] = "{{step_1['committed_amount'] || 'المستحق كاملاً'}}"
    pressure_wh = build_code_step("step_8", "وتساب ضغط الإغلاق",
                                  _SAICO_WHATSAPP_PAYMENT_CODE, pressure_input)
    alert = gmail_send("step_9", cn["gmail"], [supervisor],
        "بلا التزام: {{step_1['customer_name']}} — إعادة محاولة مطلوبة",
        "المكالمة انتهت بلا التزام واضح.\n"
        "العميل: {{step_1['customer_name']}}\n"
        "الجوال: {{step_1['customer_phone']}}\n"
        "الوثيقة: {{step_1['policy_number']}}\n"
        "النتيجة: {{step_1['outcome']}}\n"
        "الاعتراضات: {{step_1['objections']}}\n"
        "التسجيل: {{step_1['transcript_url']}}\n\nيرجى جدولة محاولة ثانية.",
        "بلا التزام — تنبيه",
        next_action=pressure_wh)

    router = build_router_step("step_3", "توجيه حسب نتيجة المكالمة", [
        condition_branch("سداد كامل",
            [[cond("TEXT_EXACTLY_MATCHES", "{{step_1['outcome']}}", "paid_full")]]),
        condition_branch("خطة أو وعد",
            [[cond("TEXT_CONTAINS", "{{step_1['outcome']}}", "plan_agreed")],
             [cond("TEXT_CONTAINS", "{{step_1['outcome']}}", "promise_to_pay")],
             [cond("TEXT_CONTAINS", "{{step_1['outcome']}}", "paid_partial")]]),
        condition_branch("رد اتصال مجدول",
            [[cond("TEXT_EXACTLY_MATCHES", "{{step_1['outcome']}}", "callback_scheduled")]]),
        condition_branch("تحويل بشري",
            [[cond("TEXT_EXACTLY_MATCHES", "{{step_1['outcome']}}", "transferred")]]),
        fallback_branch("بلا التزام"),
    ], [paid, plan_wh, cb, xfer, alert])

    if sid:
        s2 = sheet_add("step_2", cn["google-sheets"], sid, {
            "A": "{{step_1['call_id']}}",
            "B": "{{step_1['lead_id']}}",
            "C": "{{step_1['customer_name']}}",
            "D": "{{step_1['policy_number']}}",
            "E": "{{step_1['outcome']}}",
            "F": "{{step_1['committed_amount']}}",
            "G": "{{step_1['committed_date']}}",
            "H": "{{step_1['ladder_reached']}}",
            "I": "{{step_1['duration_sec']}}",
            "J": "{{step_1['objections']}}",
            "K": "{{step_1['transcript_url']}}",
            "L": "{{step_1['recorded_at']}}",
        }, "تسجيل نتيجة المكالمة", next_action=router)
    else:
        s2 = build_code_step("step_2", "تسجيل داخلي",
            'export const code = async (i) => ({ logged: true, outcome: i.outcome });',
            {"outcome": "{{step_1['outcome']}}"}, next_action=router)

    parse_input = {
        "body": "{{trigger['body']}}",
        "iban": c.get("iban", ""),
        "bank_name": c.get("bank_name", ""),
        "payment_link": c.get("payment_link", ""),
    }
    s1 = build_code_step("step_1", "قراءة نتيجة سندس",
                         _SAICO_OUTCOME_PARSE_CODE, parse_input, next_action=s2)

    return wh_trigger("استقبال نتيجة مكالمة سايكو", s1)


TEMPLATES = {
    "webhook_to_email":           {"fn": T1, "req": ["recipient_email"],                   "desc": "تنبيه إيميل فوري"},
    "webhook_to_sheet":           {"fn": T2, "req": ["spreadsheet_id"],                    "desc": "حفظ بيانات في جدول"},
    "webhook_to_sheet_and_email": {"fn": T3, "req": ["recipient_email"],                   "desc": "حفظ + تنبيه"},
    "support_auto_reply":         {"fn": T4, "req": ["spreadsheet_id"],                    "desc": "رد تلقائي + تذكرة دعم"},
    "marketing_welcome":          {"fn": T5, "req": ["spreadsheet_id"],                    "desc": "ترحيب مشترك جديد"},
    "ops_log_report":             {"fn": T6, "req": ["recipient_email", "spreadsheet_id"], "desc": "تسجيل عملية + تقرير"},
    "lead_notify_and_confirm":    {"fn": T7, "req": ["recipient_email", "spreadsheet_id"], "desc": "نظام ليدات كامل"},
    "scheduled_report":           {"fn": T8, "req": ["recipient_email", "spreadsheet_id"], "desc": "تقرير يومي"},
    "saico_collection_trigger":   {"fn": T_SAICO_TRIGGER,
                                   "req": ["sondos_base_url", "sondos_api_key", "sondos_assistant_id"],
                                   "desc": "تحصيل سايكو: webhook → إثراء → وتساب تمهيدي → مكالمة سندس"},
    "saico_collection_outcome":   {"fn": T_SAICO_OUTCOME,
                                   "req": ["supervisor_email"],
                                   "desc": "تحصيل سايكو: نتيجة سندس → ROUTER(5) → متابعة حسب النتيجة"},
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

    yield

    # Shutdown
    if _engine:
        await _engine.close()
    try:
        from mcp_sse import close_redis
        await close_redis()
    except Exception:
        pass


app = FastAPI(title="Siyadah Orchestrator", version=VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

from mcp_sse import router as sse_router  # noqa: E402
app.include_router(sse_router)


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
        result = await golden_build(e, pid, body.display_name, trigger)
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

            # ── Smart Auto-Fill: populate missing required fields ──
            if props:
                for _fn, _fi in props.items():
                    if _fn == "auth":
                        continue
                    if (isinstance(_fi, dict) and _fi.get("required", False)
                            and (_fn not in cleaned_cfg
                                 or cleaned_cfg[_fn] in (None, "", []))):
                        _ptype = _fi.get("type", "")
                        if _ptype == "BOOLEAN":
                            cleaned_cfg[_fn] = False
                        elif _ptype == "NUMBER":
                            cleaned_cfg[_fn] = 0
                        elif _ptype == "ARRAY":
                            cleaned_cfg[_fn] = []
                        elif _ptype in ("STATIC_DROPDOWN", "DROPDOWN"):
                            _opts = _fi.get("options", [])
                            if isinstance(_opts, list) and _opts:
                                _first = _opts[0]
                                cleaned_cfg[_fn] = _first.get("value", _first) if isinstance(_first, dict) else _first
                            else:
                                cleaned_cfg[_fn] = ""
                        else:
                            cleaned_cfg[_fn] = "Siyadah Auto-Fill"
                        log.info("[auto-fill] smart-build %s → %r (type=%s)",
                                 _fn, cleaned_cfg[_fn], _ptype)

            # ── Draft Guard: inject missing boolean fields ──
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
    result = await golden_build(e, pid, body.display_name, trigger)
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
async def v2_project_register(body: ProjectRegisterBody):
    """Register or update a project in the Siyadah memory layer."""
    try:
        from database import async_session
        from models import Project, ProjectIdentity
        from sqlalchemy import select
        if not async_session:
            raise HTTPException(503, detail="Database not configured")

        pid = body.project_id or DEFAULT_PID
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
async def v2_identity_ingest(body: IngestBody):
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

        pid = body.project_id or DEFAULT_PID
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
async def v2_logic_suggest(body: SuggestBody):
    """Sector-aware suggestion engine — returns 3 personalized flow recommendations.

    Analyzes the project's identity and proposes automation flows that
    match its sector, language, and business goals.
    """
    from database import async_session
    from models import ProjectIdentity, KnowledgeAsset
    from sqlalchemy import select

    pid = body.project_id or DEFAULT_PID

    sector = "default"
    lang = "en"
    tone = "professional"
    goals: List[str] = []

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

    if lang == "ar":
        hint = (
            f"بناءً على قطاع «{sector}»، نقترح {len(suggestions)} فلوهات أتمتة. "
            "اختر أحدها أو اطلب فلو مخصص."
        )
    else:
        hint = (
            f"Based on sector «{sector}», we suggest {len(suggestions)} automation flows. "
            "Pick one or request a custom flow."
        )

    return {
        "project_id": pid,
        "sector": sector,
        "language": lang,
        "tone_of_voice": tone,
        "suggestions": suggestions,
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
    project_id: Optional[str] = Query(
        default=None,
        description="Tenant project id (defaults to AP_PROJECT_ID).",
    ),
):
    """Compare ProjectIdentity + knowledge to success patterns in Mem; add per-flow health WARNING hints."""
    pid = project_id or DEFAULT_PID
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
async def v2_mcp_execute(body: MCPExecuteBody):
    """Live MCP Tool Dispatcher — Claude calls tools here.
    Also used by mcp_sse._execute_mcp_tool for SSE transport.
    """
    tool = body.tool
    p = body.parameters
    pid = body.project_id or p.get("project_id") or DEFAULT_PID
    cn = resolve_conns(body.connection_ids or p.get("connection_ids"))

    if tool in _DB_ONLY_TOOLS:
        e = _engine  # may be None — these tools don't need AP
    else:
        e = E()

    try:
        result = await _mcp_dispatch(e, tool, p, pid, cn)
        hint = await _generate_smart_hint(pid)
        return {"tool": tool, "success": True,
                "result": compress_response(result), "_hint": hint}
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
