from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)


GATE6_FLAG_ENV = "GATE6_AP_VISIBLE_DRAFT"

# Whitelist of fields safe to expose in API responses / AP metadata.
# Blocked entries carry `reason`; runnable entries don't — both share the
# same projection so secrets (errored_connections, externalId, displayName,
# connection_type, ownerEmail, ids) never leak.
_SAFE_PIECE_KEYS = ("piece", "short", "status", "reason", "auth_type", "requires_auth")


def gate6_ap_visible_draft_enabled() -> bool:
    """Gate-6 feature flag.

    True only when GATE6_AP_VISIBLE_DRAFT == "1". Any other value
    (unset, "0", "true", "yes", "True", whitespace) keeps the legacy
    DB-only path. Strict equality is intentional — flips must be explicit.
    """
    return os.getenv(GATE6_FLAG_ENV, "") == "1"


def sanitize_pieces(pieces: Iterable[dict] | None) -> List[dict]:
    """Project pieces to the safe whitelist.

    Used for both blocked and runnable entries — the structure is the
    same and the secrets to strip are the same (errored_connections,
    connection_external_id, connection_display_name, connection_type,
    any id/externalId/displayName/ownerEmail).
    """
    cleaned: List[dict] = []
    for raw in pieces or []:
        if not isinstance(raw, dict):
            continue
        cleaned.append({k: raw[k] for k in _SAFE_PIECE_KEYS if k in raw})
    return cleaned


def sanitize_missing_connections(blocked_pieces: Iterable[dict] | None) -> List[dict]:
    """Sanitize blocked pieces for API responses."""
    return sanitize_pieces(blocked_pieces)


def build_sanitized_metadata(*, tenant_id: str) -> dict:
    """AP `metadata` payload for an AP_VISIBLE_DRAFT flow.

    `tenantId` is the only identifier kept because the ownership gate
    `_flow_belongs_to` (main.py) falls back to AP `metadata.tenantId`
    when the flow is absent from `flow_registry` — draft flows are not
    registered, so dropping `tenantId` would make every subsequent
    ownership check on the draft fail closed (403).

    Explicitly excluded: ownerEmail, flowId, platformId, projectIds
    array, raw AP connection objects, connection ids/externalIds.
    """
    return {
        "tenantId": tenant_id,
        "mode": "AP_VISIBLE_DRAFT",
        "stampedAt": datetime.now(timezone.utc).isoformat(),
        "stampedBy": "siyadah:gate6_ap_visible_draft",
        "skipPublish": True,
    }


def build_pending_activation_payload(
    saved_plan: Dict[str, Any],
    sanitized_missing: List[dict],
) -> dict:
    """Project the persisted plan to safe API fields only."""
    return {
        "id": saved_plan.get("id"),
        "flow_id": saved_plan.get("flow_id"),
        "status": saved_plan.get("status"),
        "display_name": saved_plan.get("display_name"),
        "missing_connections": sanitized_missing,
        "next_reminder_at": saved_plan.get("next_reminder_at"),
    }


def build_connection_gate_payload(
    connection_gate: Dict[str, Any],
    sanitized_blocked: List[dict],
    sanitized_runnable: List[dict],
) -> dict:
    """Project the in-memory gate result to safe API fields only.

    Counts, status string, and sanitized piece lists — no raw
    `errored_connections`, no `connection_ids` map, no externalIds.
    """
    return {
        "status": connection_gate.get("status"),
        "blocked_count": connection_gate.get("blocked_count"),
        "runnable_count": connection_gate.get("runnable_count"),
        "total_pieces": connection_gate.get("total_pieces"),
        "blocked_pieces": sanitized_blocked,
        "runnable_pieces": sanitized_runnable,
    }


async def create_ap_visible_draft_flow(
    *,
    engine,
    pid: str,
    display_name: str,
    trigger: dict,
) -> str:
    """Create an AP-visible DISABLED flow with the imported trigger tree.

    Strict call sequence: create_flow → update_metadata → import_flow → get_flow.
    The flow stays DISABLED because publish/enable are NEVER called from
    this function. Forbidden ops not invoked: publish_and_enable,
    LOCK_AND_PUBLISH, CHANGE_STATUS, test_webhook.

    Post-import GET-verify mirrors `SiyadahEngine.verify_flow`: AP can
    answer 200 on IMPORT_FLOW and still leave `version.trigger.type` as
    `"EMPTY"` — persisting that flow_id would record a draft with no
    usable graph. The EMPTY check is non-negotiable per AGENTS.md.
    """
    if engine is None:
        raise RuntimeError("activepieces_engine_not_configured")

    flow = await engine.create_flow(pid, display_name)
    fid = flow.get("id") if isinstance(flow, dict) else None
    if not fid:
        raise RuntimeError("ap_create_flow_returned_no_id")

    await engine.update_metadata(fid, build_sanitized_metadata(tenant_id=pid))
    await engine.import_flow(fid, display_name, trigger)

    flow_after = await engine.get_flow(fid)
    if not isinstance(flow_after, dict):
        raise RuntimeError("ap_visible_draft_get_flow_invalid_response")

    if flow_after.get("status") == "ENABLED":
        # Defensive: AP_VISIBLE_DRAFT must never end up ENABLED.
        raise RuntimeError("ap_visible_draft_unexpectedly_enabled")

    trigger_type = (
        flow_after.get("version", {})
        .get("trigger", {})
        .get("type", "UNKNOWN")
    )
    if trigger_type == "EMPTY":
        raise RuntimeError("ap_visible_draft_trigger_empty_after_import")

    return fid


async def save_pending_activation_plan(
    *,
    async_session,
    PendingActivationPlan,
    tenant_id: str,
    display_name: str,
    graph_plan: Dict[str, Any],
    connection_gate: Dict[str, Any],
    next_reminder_hours: int = 24,
    flow_id: Optional[str] = None,
) -> dict:
    """Persist a workflow plan that is ready but blocked by missing connections.

    No secrets are stored here. Raw blocked_pieces (with errored_connections)
    persist to DB only — callers MUST sanitize before returning to clients.
    `flow_id` is set when Gate-6 AP_VISIBLE_DRAFT created an AP shell.
    """
    if async_session is None:
        raise RuntimeError("database_not_configured")

    now = datetime.now(timezone.utc)
    next_reminder_at = now + timedelta(hours=next_reminder_hours)

    row = PendingActivationPlan(
        tenant_id=tenant_id,
        display_name=display_name,
        status=connection_gate.get("status", "PENDING_CONNECTIONS"),
        graph_plan=graph_plan,
        missing_connections=connection_gate.get("blocked_pieces", []),
        runnable_pieces=connection_gate.get("runnable_pieces", []),
        blocked_pieces=connection_gate.get("blocked_pieces", []),
        next_reminder_at=next_reminder_at,
        flow_id=flow_id,
    )

    async with async_session() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)

    return {
        "id": row.id,
        "status": row.status,
        "display_name": row.display_name,
        "missing_connections": row.missing_connections,
        "runnable_pieces": row.runnable_pieces,
        "blocked_pieces": row.blocked_pieces,
        "flow_id": row.flow_id,
        "next_reminder_at": row.next_reminder_at.isoformat() if row.next_reminder_at else None,
    }
