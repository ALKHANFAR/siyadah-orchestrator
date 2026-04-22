"""
Siyadah MCP SSE — Server-Sent Events transport for MCP protocol
=================================================================
Endpoints:
  GET  /v2/mcp/sse                     → open SSE stream, receive session_id
  POST /v2/mcp/messages/{session_id}   → send tool calls, responses pushed via SSE

Sessions are stored in Redis (survives Railway restarts) with in-memory fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

log = logging.getLogger("siyadah.sse")

router = APIRouter(prefix="/v2/mcp", tags=["MCP-SSE"])

REDIS_URL = os.getenv("REDIS_URL", "")
# Guard: refuse unsubstituted Railway template placeholders (prevents silent
# DNS failure like `<RAILWAY_PRIVATE_DOMAIN>:6379`).
if REDIS_URL and (
    "<" in REDIS_URL and ">" in REDIS_URL
    or "${{" in REDIS_URL
    or "RAILWAY_PRIVATE_DOMAIN" in REDIS_URL
):
    log.error(
        "REDIS_URL contains an unsubstituted template placeholder: %r. "
        "Fix in Railway → Variables using ${{Redis.REDIS_URL}} or paste the "
        "real hostname. Falling back to in-memory sessions.", REDIS_URL
    )
    REDIS_URL = ""
SESSION_TTL = 3600  # 1 hour

_redis: Optional[aioredis.Redis] = None
_sessions_mem: Dict[str, Dict[str, Any]] = {}


# ── Redis lifecycle ──────────────────────────────────────────────

async def init_redis() -> None:
    global _redis
    if not REDIS_URL:
        log.warning("REDIS_URL not set — SSE sessions will be in-memory only")
        return
    try:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=5)
        await _redis.ping()
        log.info("Redis connected for SSE sessions")
    except Exception as exc:
        log.error("Redis connection failed: %s — falling back to in-memory", exc)
        _redis = None


async def close_redis() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


# ── Session store (Redis-first, memory-fallback) ────────────────

async def _save_session(sid: str, data: dict) -> None:
    payload = json.dumps(data, default=str)
    if _redis:
        try:
            await _redis.setex(f"mcp:sess:{sid}", SESSION_TTL, payload)
            return
        except Exception as exc:
            log.warning("Redis save failed for session %s: %s", sid, exc)
    _sessions_mem[sid] = data


async def _load_session(sid: str) -> Optional[dict]:
    if _redis:
        try:
            raw = await _redis.get(f"mcp:sess:{sid}")
            if raw:
                return json.loads(raw)
        except Exception as exc:
            log.warning("Redis load failed for session %s: %s", sid, exc)
    return _sessions_mem.get(sid)


async def _delete_session(sid: str) -> None:
    if _redis:
        try:
            await _redis.delete(f"mcp:sess:{sid}")
        except Exception:
            pass
    _sessions_mem.pop(sid, None)


# ── Per-session event queue ──────────────────────────────────────

_queues: Dict[str, asyncio.Queue] = {}


def _get_queue(sid: str) -> asyncio.Queue:
    if sid not in _queues:
        _queues[sid] = asyncio.Queue()
    return _queues[sid]


# ── SSE stream endpoint ─────────────────────────────────────────

@router.get("/sse")
async def mcp_sse_connect(request: Request):
    """Open an SSE stream. Returns session_id in the first event."""
    sid = str(uuid.uuid4())
    session_data = {
        "session_id": sid,
        "created_at": time.time(),
        "status": "connected",
    }
    await _save_session(sid, session_data)
    queue = _get_queue(sid)
    log.info("SSE session opened: %s", sid)

    async def event_generator():
        yield {
            "event": "endpoint",
            "data": json.dumps({
                "session_id": sid,
                "messages_url": f"/v2/mcp/messages/{sid}",
            }),
        }
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield {"event": "message", "data": json.dumps(msg)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            await _delete_session(sid)
            _queues.pop(sid, None)
            log.info("SSE session closed: %s", sid)

    return EventSourceResponse(event_generator())


# ── Message endpoint ─────────────────────────────────────────────

class MCPMessage(BaseModel):
    jsonrpc: str = "2.0"
    method: str = ""
    id: Optional[int | str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


@router.post("/messages/{session_id}")
async def mcp_sse_message(session_id: str, body: MCPMessage):
    """Receive a JSON-RPC message and push the response through the SSE stream."""
    sess = await _load_session(session_id)
    if not sess:
        raise HTTPException(404, detail=f"Session '{session_id}' not found or expired")

    queue = _queues.get(session_id)
    if not queue:
        raise HTTPException(410, detail=f"Session '{session_id}' stream disconnected")

    response = await _handle_mcp_message(body)

    await queue.put(response)

    return {"status": "accepted", "session_id": session_id}


async def _handle_mcp_message(msg: MCPMessage) -> dict:
    """Route JSON-RPC methods to internal handlers."""

    if msg.method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg.id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "siyadah-orchestrator", "version": "7.1.0"},
                "capabilities": {"tools": {"listChanged": False}},
            },
        }

    if msg.method == "tools/list":
        from main import v2_mcp_tools
        tools_resp = await v2_mcp_tools()
        mcp_tools = []
        for t in tools_resp.get("tools", []):
            mcp_tools.append({
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("parameters", {"type": "object", "properties": {}}),
            })
        return {
            "jsonrpc": "2.0",
            "id": msg.id,
            "result": {"tools": mcp_tools},
        }

    if msg.method == "tools/call":
        tool_name = msg.params.get("name", "")
        arguments = msg.params.get("arguments", {})
        try:
            result = await _execute_mcp_tool(tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": msg.id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                    "isError": False,
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg.id,
                "result": {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": msg.id,
        "error": {"code": -32601, "message": f"Method not found: {msg.method}"},
    }


async def _load_project_context(project_id: str) -> dict | None:
    """Load ProjectIdentity + KnowledgeAsset as system context for AI tools."""
    try:
        from database import async_session
        from models import ProjectIdentity, KnowledgeAsset
        from sqlalchemy import select

        if not async_session:
            return None

        async with async_session() as session:
            identity = (await session.execute(
                select(ProjectIdentity).where(
                    ProjectIdentity.project_id == project_id)
            )).scalar_one_or_none()

            knowledge = (await session.execute(
                select(KnowledgeAsset).where(
                    KnowledgeAsset.project_id == project_id)
            )).scalar_one_or_none()

        if not identity:
            return None

        return {
            "sector": identity.sector,
            "language": identity.language,
            "description": identity.business_description,
            "tone_of_voice": knowledge.tone_of_voice if knowledge else None,
            "faqs": knowledge.faqs if knowledge else [],
            "brand_keywords": knowledge.brand_keywords if knowledge else [],
        }
    except Exception as exc:
        log.warning("Failed to load project context: %s", exc)
        return None


async def _execute_mcp_tool(tool_name: str, arguments: dict) -> Any:
    """Shared tool executor — used by both SSE and REST `/v2/mcp/execute`.

    Injects ProjectIdentity as _system_context so the AI has full
    business awareness when processing tool calls.
    """
    from main import E, DEFAULT_PID, resolve_conns, _mcp_dispatch

    e = E()
    pid = arguments.pop("project_id", None) or DEFAULT_PID
    cn = resolve_conns(arguments.pop("connection_ids", None))

    context = await _load_project_context(pid)
    if context:
        arguments["_system_context"] = context

    return await _mcp_dispatch(e, tool_name, arguments, pid, cn)
