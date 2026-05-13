from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

ORCH_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_ROOT = ORCH_ROOT / "registry"
PIECES_DIR = REGISTRY_ROOT / "pieces"

_registry_cache: Optional[Dict[str, Any]] = None


def canonical_piece_name(piece: str) -> str:
    token = (piece or "").strip()
    if not token:
        return token
    if token.startswith("@activepieces/"):
        return token
    return f"@activepieces/piece-{token.replace('@activepieces/piece-', '')}"


def piece_slug(piece: str) -> str:
    canonical = canonical_piece_name(piece)
    if canonical.startswith("@activepieces/piece-"):
        return canonical[len("@activepieces/piece-"):]
    return canonical.replace("@", "").replace("/", "_")


def load_registry() -> Dict[str, Any]:
    global _registry_cache

    if _registry_cache is not None:
        return _registry_cache

    if not PIECES_DIR.is_dir():
        raise HTTPException(
            status_code=500,
            detail={
                "error": "REGISTRY_NOT_AVAILABLE",
                "piece": "",
                "action_name": "",
                "missing_required_fields": [],
                "missing_dropdown_fields": [],
                "missing_dynamic_fields": [],
                "message": f"Registry pieces dir not found: {PIECES_DIR}",
            },
        )

    pieces_by_name: Dict[str, Dict[str, Any]] = {}
    pieces_by_slug: Dict[str, Dict[str, Any]] = {}

    for path in sorted(PIECES_DIR.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            continue

        piece_name = record.get("pieceName")
        if not piece_name:
            continue

        canonical = canonical_piece_name(str(piece_name))
        slug = piece_slug(canonical)
        pieces_by_name[canonical] = record
        pieces_by_slug[slug] = record

    _registry_cache = {
        "pieces_by_name": pieces_by_name,
        "pieces_by_slug": pieces_by_slug,
    }
    return _registry_cache


def _error(
    code: str,
    piece: str,
    action_name: str,
    missing_required_fields: List[str] | None = None,
    missing_dropdown_fields: List[str] | None = None,
    missing_dynamic_fields: List[str] | None = None,
    message: str = "",
) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": code,
            "piece": canonical_piece_name(piece),
            "action_name": action_name,
            "missing_required_fields": missing_required_fields or [],
            "missing_dropdown_fields": missing_dropdown_fields or [],
            "missing_dynamic_fields": missing_dynamic_fields or [],
            "message": message,
        },
    )


def get_piece_action_schema(piece: str, action_name: str) -> Dict[str, Any]:
    registry = load_registry()
    canonical = canonical_piece_name(piece)
    slug = piece_slug(canonical)

    piece_record = (
        registry["pieces_by_name"].get(canonical)
        or registry["pieces_by_slug"].get(slug)
    )

    if not piece_record:
        raise _error(
            "REGISTRY_PIECE_NOT_FOUND",
            canonical,
            action_name,
            message=f"Piece not found in generated registry: {canonical}",
        )

    actions = piece_record.get("actions") or {}
    action_schema = actions.get(action_name)

    if not action_schema:
        raise _error(
            "REGISTRY_ACTION_NOT_FOUND",
            canonical,
            action_name,
            message=f"Action not found in generated registry: {canonical}.{action_name}",
        )

    return action_schema


def _missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if value == {} or value == []:
        return True
    return False


def _action_input(action: Dict[str, Any]) -> Dict[str, Any]:
    settings = action.get("settings") if isinstance(action.get("settings"), dict) else {}
    direct_input = action.get("input") if isinstance(action.get("input"), dict) else {}
    nested_input = settings.get("input") if isinstance(settings.get("input"), dict) else {}

    merged: Dict[str, Any] = {}
    merged.update(settings)
    merged.update(nested_input)
    merged.update(direct_input)
    return merged


def _piece_and_action(action: Dict[str, Any]) -> tuple[str, str]:
    settings = action.get("settings") if isinstance(action.get("settings"), dict) else {}

    piece = (
        action.get("piece")
        or action.get("piece_name")
        or action.get("pieceName")
        or settings.get("piece")
        or settings.get("piece_name")
        or settings.get("pieceName")
        or ""
    )

    action_name = (
        action.get("action_name")
        or action.get("actionName")
        or settings.get("action_name")
        or settings.get("actionName")
        or ""
    )

    return str(piece).strip(), str(action_name).strip()


def validate_action_against_registry(action: Dict[str, Any]) -> None:
    if not isinstance(action, dict):
        return

    if action.get("type", "PIECE") != "PIECE":
        return

    piece, action_name = _piece_and_action(action)

    if not piece or not action_name:
        raise _error(
            "REGISTRY_REQUIRED_FIELD_MISSING",
            piece,
            action_name,
            missing_required_fields=["piece", "action_name"],
            message="PIECE action requires piece and action_name.",
        )

    schema = get_piece_action_schema(piece, action_name)
    payload = _action_input(action)

    required = list(schema.get("required_fields") or [])
    dropdown = list(schema.get("dropdown_fields") or [])
    dynamic = list(schema.get("dynamic_fields") or [])

    missing_required = [f for f in required if _missing(payload.get(f))]
    missing_dropdown = [f for f in dropdown if _missing(payload.get(f))]
    missing_dynamic = [f for f in dynamic if _missing(payload.get(f))]

    if missing_required:
        raise _error(
            "REGISTRY_REQUIRED_FIELD_MISSING",
            piece,
            action_name,
            missing_required_fields=missing_required,
            missing_dropdown_fields=missing_dropdown,
            missing_dynamic_fields=missing_dynamic,
            message="Required Activepieces fields are missing according to generated registry.",
        )

    if missing_dropdown:
        raise _error(
            "PENDING_CONFIGURATION_DROPDOWN",
            piece,
            action_name,
            missing_dropdown_fields=missing_dropdown,
            missing_dynamic_fields=missing_dynamic,
            message="Dropdown fields need resolver/configuration before build.",
        )

    if missing_dynamic:
        raise _error(
            "PENDING_CONFIGURATION_DYNAMIC",
            piece,
            action_name,
            missing_dynamic_fields=missing_dynamic,
            message="Dynamic fields need resolver/configuration before build.",
        )


def validate_actions_against_registry(actions: Any) -> None:
    if not isinstance(actions, list):
        return

    for action in actions:
        if not isinstance(action, dict):
            continue

        validate_action_against_registry(action)

        for key in ("actions", "loop_actions", "before_loop", "after_loop"):
            if isinstance(action.get(key), list):
                validate_actions_against_registry(action[key])

        branches = action.get("branches")
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, dict):
                    validate_actions_against_registry(branch.get("actions"))
