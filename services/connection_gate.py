from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List


def extract_pieces_from_steps(steps: List[dict]) -> List[str]:
    """Collect PIECE names from normal actions, router branches, and loops."""
    pieces: List[str] = []

    for step in steps or []:
        if not isinstance(step, dict):
            continue

        stype = step.get("type", "PIECE")
        raw = (step.get("piece") or step.get("piece_name") or "").strip()

        if stype == "PIECE" and raw:
            pieces.append(raw)

        for key in ("actions", "loop_actions", "before_loop", "after_loop"):
            sub = step.get(key)
            if isinstance(sub, list):
                pieces.extend(extract_pieces_from_steps(sub))

        for branch in step.get("branches") or []:
            if isinstance(branch, dict):
                pieces.extend(extract_pieces_from_steps(branch.get("actions", [])))

    return pieces


def schema_requires_auth(schema: dict) -> bool:
    auth = schema.get("auth")

    if not auth:
        return False

    if isinstance(auth, list):
        return any(bool(x.get("required", True)) for x in auth if isinstance(x, dict))

    if isinstance(auth, dict):
        return bool(auth.get("required", True))

    return True


def schema_auth_type(schema: dict) -> str | None:
    auth = schema.get("auth")

    if not auth:
        return None

    if isinstance(auth, list):
        for item in auth:
            if isinstance(item, dict) and item.get("type"):
                return item.get("type")

    if isinstance(auth, dict):
        return auth.get("type")

    return "UNKNOWN"


async def classify_connection_requirements(
    *,
    steps: List[dict],
    live_connections: List[dict],
    fetch_schema: Callable[[str], Awaitable[dict]],
    connection_overrides: Dict[str, str] | None = None,
) -> dict:
    """Classify pieces as runnable or blocked by missing/broken connections.

    Activepieces remains source of truth:
    - schema.auth says if a piece needs auth
    - app-connections says if tenant has ACTIVE connection
    """

    connection_overrides = connection_overrides or {}

    raw_pieces = extract_pieces_from_steps(steps)

    ordered: List[str] = []
    seen = set()

    for piece in raw_pieces:
        short = piece.replace("@activepieces/piece-", "")
        full = piece if piece.startswith("@activepieces/") else f"@activepieces/piece-{short}"

        if full not in seen:
            ordered.append(full)
            seen.add(full)

    active_by_piece: Dict[str, dict] = {}
    errored_by_piece: Dict[str, list] = {}

    for conn in live_connections or []:
        piece_name = conn.get("pieceName") or ""
        status = conn.get("status") or ""

        if not piece_name:
            continue

        if status == "ACTIVE" and piece_name not in active_by_piece:
            active_by_piece[piece_name] = conn
        elif status and status != "ACTIVE":
            errored_by_piece.setdefault(piece_name, []).append({
                "id": conn.get("id"),
                "externalId": conn.get("externalId"),
                "displayName": conn.get("displayName"),
                "status": status,
                "type": conn.get("type"),
            })

    runnable_pieces = []
    blocked_pieces = []
    connection_ids: Dict[str, str] = {}

    for full in ordered:
        short = full.replace("@activepieces/piece-", "")
        schema = await fetch_schema(full)
        requires_auth = schema_requires_auth(schema)

        if not requires_auth:
            runnable_pieces.append({
                "piece": full,
                "short": short,
                "requires_auth": False,
                "status": "RUNNABLE",
            })
            continue

        override_id = connection_overrides.get(short, "")
        active = active_by_piece.get(full)

        if override_id:
            connection_ids[short] = override_id
            runnable_pieces.append({
                "piece": full,
                "short": short,
                "requires_auth": True,
                "auth_type": schema_auth_type(schema),
                "status": "RUNNABLE_WITH_OVERRIDE",
                "connection_external_id": override_id,
            })
            continue

        if active:
            ext = active.get("externalId") or active.get("id")
            connection_ids[short] = ext
            runnable_pieces.append({
                "piece": full,
                "short": short,
                "requires_auth": True,
                "auth_type": schema_auth_type(schema),
                "status": "RUNNABLE_CONNECTED",
                "connection_external_id": ext,
                "connection_type": active.get("type"),
                "connection_display_name": active.get("displayName"),
            })
            continue

        blocked_pieces.append({
            "piece": full,
            "short": short,
            "requires_auth": True,
            "auth_type": schema_auth_type(schema),
            "status": "BLOCKED_CONNECTION_REQUIRED",
            "errored_connections": errored_by_piece.get(full, []),
            "reason": "missing_or_inactive_connection",
        })

    return {
        "status": "READY" if not blocked_pieces else "PENDING_CONNECTIONS",
        "runnable_pieces": runnable_pieces,
        "blocked_pieces": blocked_pieces,
        "connection_ids": connection_ids,
        "total_pieces": len(ordered),
        "blocked_count": len(blocked_pieces),
        "runnable_count": len(runnable_pieces),
    }
