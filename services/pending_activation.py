from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict


async def save_pending_activation_plan(
    *,
    async_session,
    PendingActivationPlan,
    tenant_id: str,
    display_name: str,
    graph_plan: Dict[str, Any],
    connection_gate: Dict[str, Any],
    next_reminder_hours: int = 24,
) -> dict:
    """Persist a workflow plan that is ready but blocked by missing connections.

    No secrets are stored here. Only graph structure + blocked/runnable pieces.
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
        "next_reminder_at": row.next_reminder_at.isoformat() if row.next_reminder_at else None,
    }
