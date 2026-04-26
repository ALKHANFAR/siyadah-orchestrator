"""
Siyadah Piece Validator — hard-stop guard against the AP-blind-import trap.

Activepieces accepts IMPORT_FLOW with a non-existent pieceName and marks the
version `valid: true` — see the Deep System Forensics probe (2026-04-24).
Without a pre-flight check we discover typos only at runtime, which is what
created the orphan-flow problem Wave-1 had to clean up.

Contract:
  validate_trigger(session, trigger_dict) -> list[ValidationError]
  assert_trigger(session, trigger_dict)  -> raises HTTPException(422) on any error

Handlebars awareness: any leaf value that contains `{{…}}` is treated as a
dynamic reference. The validator checks PRESENCE (required field present)
but NOT TYPE (because we can't evaluate `{{trigger['body']['amount']}}` to
a concrete value at build time).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import PieceRegistry

_DYNAMIC_RE = re.compile(r"\{\{.*?\}\}")

# Sentinel values we never want reaching Activepieces — either stale
# auto-fill leftovers or obvious placeholders from incomplete clients.
# If any of these appear as a leaf value, we hard-stop.
_BANNED_VALUES = frozenset({
    "Siyadah Auto-Fill",
    "TODO_REPLACE_WITH_SPREADSHEET_ID",
    "your_id_here",
    "<replace_me>",
})


@dataclass
class ValidationError:
    """Structured error the BFF can switch on — no regex-parsing strings."""
    error_code: str        # stable enum: PIECE_NOT_IN_REGISTRY, ACTION_NOT_FOUND, ...
    message: str           # human-readable, English (logs) or mirrored Arabic in hint
    field: str             # JSONPath-ish, e.g. "step_2.input.receiver"
    piece: str | None = None
    piece_version: str | None = None
    hint: str | None = None


def _strip_version(raw: str) -> str:
    """'~0.12.1' → '0.12.1'. Leaves bare semver untouched."""
    if not raw:
        return ""
    return raw.lstrip("~^").strip()


def _contains_handlebars(value: Any) -> bool:
    """Recursively: does this value (str/list/dict) contain `{{…}}`?"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(_DYNAMIC_RE.search(value))
    if isinstance(value, dict):
        return any(_contains_handlebars(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_handlebars(v) for v in value)
    return False


def _is_banned(value: Any) -> bool:
    if isinstance(value, str) and value.strip() in _BANNED_VALUES:
        return True
    if isinstance(value, dict):
        return any(_is_banned(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_is_banned(v) for v in value)
    return False


def _walk_steps(trigger: dict):
    """Yield (path, step_dict) for trigger + every action/router-child/loop-child.

    `path` is a breadcrumb like "trigger" → "trigger.nextAction" →
    "trigger.nextAction.children[2]" used to point at the failing step.
    """
    if not trigger:
        return
    yield ("trigger", trigger)

    def recurse(node: dict, path: str):
        nxt = node.get("nextAction")
        if isinstance(nxt, dict):
            npath = f"{path}.nextAction"
            yield (npath, nxt)
            yield from recurse(nxt, npath)
        # Router children
        for idx, child in enumerate(node.get("children") or []):
            if isinstance(child, dict):
                cpath = f"{path}.children[{idx}]"
                yield (cpath, child)
                yield from recurse(child, cpath)
        # Loop body
        floop = node.get("firstLoopAction")
        if isinstance(floop, dict):
            lpath = f"{path}.firstLoopAction"
            yield (lpath, floop)
            yield from recurse(floop, lpath)

    yield from recurse(trigger, "trigger")


async def _fetch_piece(
    session: AsyncSession, name: str, version: str,
) -> PieceRegistry | None:
    """Exact (name, version) lookup. Callers that want fuzzy 'latest'
    resolution should pre-resolve the version before validation."""
    res = await session.execute(
        select(PieceRegistry).where(
            PieceRegistry.name == name,
            PieceRegistry.piece_version == version,
        )
    )
    return res.scalar_one_or_none()


async def validate_trigger(
    session: AsyncSession, trigger: dict,
) -> list[ValidationError]:
    """Walk the trigger tree and collect ALL errors (not just the first).

    The BFF wants to show the user every broken field at once. Returning
    a list instead of raising lets the caller decide whether to batch
    or stop.
    """
    errors: list[ValidationError] = []

    for path, step in _walk_steps(trigger):
        # ── Structural step types (ROUTER, LOOP, CODE) don't reference pieces ──
        stype = step.get("type", "")
        if stype not in ("PIECE", "PIECE_TRIGGER"):
            # CODE steps and routers don't use pieces; banned-value scan only
            settings = step.get("settings") or {}
            if _is_banned(settings.get("input")):
                errors.append(ValidationError(
                    error_code="BANNED_PLACEHOLDER_VALUE",
                    message=f"{path}: placeholder/auto-fill value detected in input",
                    field=f"{path}.settings.input",
                    hint="Replace TODO_* placeholders with real values before building.",
                ))
            continue

        settings = step.get("settings") or {}
        piece_name = settings.get("pieceName", "")
        raw_version = settings.get("pieceVersion", "")
        version = _strip_version(raw_version)

        if not piece_name:
            errors.append(ValidationError(
                error_code="PIECE_NAME_MISSING",
                message=f"{path}: settings.pieceName is required",
                field=f"{path}.settings.pieceName",
            ))
            continue
        if not version:
            errors.append(ValidationError(
                error_code="PIECE_VERSION_MISSING",
                message=f"{path}: settings.pieceVersion is required",
                field=f"{path}.settings.pieceVersion",
                piece=piece_name,
                hint="Include pieceVersion like '~0.12.1' — orchestrator strips the '~' on lookup.",
            ))
            continue

        piece = await _fetch_piece(session, piece_name, version)
        if piece is None:
            # Check if a different version exists — better error message
            any_ver = await session.execute(
                select(PieceRegistry.piece_version).where(
                    PieceRegistry.name == piece_name,
                )
            )
            known = [v for (v,) in any_ver.all()]
            if known:
                errors.append(ValidationError(
                    error_code="PIECE_VERSION_NOT_IN_REGISTRY",
                    message=f"{path}: {piece_name} version {version!r} not in registry",
                    field=f"{path}.settings.pieceVersion",
                    piece=piece_name,
                    piece_version=version,
                    hint=f"Known versions: {sorted(known)[-3:]}. "
                         f"Run: python -m scripts.sync_pieces --piece {piece_name}",
                ))
            else:
                errors.append(ValidationError(
                    error_code="PIECE_NOT_IN_REGISTRY",
                    message=f"{path}: unknown piece {piece_name!r}",
                    field=f"{path}.settings.pieceName",
                    piece=piece_name,
                    piece_version=version,
                    hint=f"Run: python -m scripts.sync_pieces --piece {piece_name}",
                ))
            continue

        # ── Action/trigger name exists? ──
        if stype == "PIECE_TRIGGER":
            name_key = settings.get("triggerName", "")
            index = piece.triggers_index or {}
            kind = "trigger"
            field_path = f"{path}.settings.triggerName"
        else:
            name_key = settings.get("actionName", "")
            index = piece.actions_index or {}
            kind = "action"
            field_path = f"{path}.settings.actionName"

        if not name_key:
            errors.append(ValidationError(
                error_code=f"{kind.upper()}_NAME_MISSING",
                message=f"{path}: settings.{kind}Name is required",
                field=field_path,
                piece=piece_name,
                piece_version=version,
            ))
            continue

        if name_key not in index:
            errors.append(ValidationError(
                error_code=f"{kind.upper()}_NOT_FOUND",
                message=f"{path}: {kind} {name_key!r} not found in {piece_name}",
                field=field_path,
                piece=piece_name,
                piece_version=version,
                hint=f"Available {kind}s: {sorted(index.keys())[:10]}",
            ))
            continue

        step_input = settings.get("input") or {}

        # ── Auth-Type Compatibility (Multi-Auth Gauntlet hook) ──
        # If the piece declares an auth type (OAUTH2 / CUSTOM_AUTH / BASIC_AUTH /
        # SECRET_TEXT etc.), the step's input MUST carry an `auth` key — usually
        # a connection ref like `{{connections['xyz']}}`. We don't validate the
        # connection identity itself; that's guard_connections' job (live AP).
        if piece.auth_type and piece.auth_type not in ("NONE", ""):
            auth_val = step_input.get("auth")
            if auth_val in (None, "", [], {}):
                errors.append(ValidationError(
                    error_code="AUTH_REQUIRED_BUT_MISSING",
                    message=f"{path}: {piece_name} requires {piece.auth_type} "
                            f"auth but input.auth is missing",
                    field=f"{path}.settings.input.auth",
                    piece=piece_name,
                    piece_version=version,
                    hint="Pass connection_ids[\"<piece-short>\"] in the build "
                         "request, or include input.auth='{{connections[\"...\"]}}'.",
                ))

        # ── Required-field presence (Handlebars-aware) ──
        required_props: list[str] = (index.get(name_key) or {}).get(
            "required_props", []
        )
        for req in required_props:
            if req == "auth":
                # auth is injected by the build pipeline via C(conn_id) —
                # if it's missing the request goes to guard_connections, not
                # here. Skip to avoid a duplicate failure mode.
                continue
            if req not in step_input:
                errors.append(ValidationError(
                    error_code="REQUIRED_FIELD_MISSING",
                    message=f"{path}: required field {req!r} missing from input",
                    field=f"{path}.settings.input.{req}",
                    piece=piece_name,
                    piece_version=version,
                    hint="Provide the field, OR a handlebars ref like "
                         "'{{trigger['body']['" + req + "']}}' if the value comes "
                         "from an upstream step.",
                ))
                continue
            val = step_input[req]
            # Empty string / None / [] / {} — handlebars refs are OK (they resolve later)
            if _contains_handlebars(val):
                continue
            if val is None or val == "" or val == [] or val == {}:
                errors.append(ValidationError(
                    error_code="REQUIRED_FIELD_EMPTY",
                    message=f"{path}: required field {req!r} is empty",
                    field=f"{path}.settings.input.{req}",
                    piece=piece_name,
                    piece_version=version,
                ))

        # ── Banned placeholder scan on this step's input ──
        if _is_banned(step_input):
            errors.append(ValidationError(
                error_code="BANNED_PLACEHOLDER_VALUE",
                message=f"{path}: placeholder/auto-fill value detected — "
                        "the 'Siyadah Auto-Fill' era is over",
                field=f"{path}.settings.input",
                piece=piece_name,
                piece_version=version,
                hint="Replace the placeholder with a real value or a handlebars ref.",
            ))

    return errors


async def assert_trigger(session: AsyncSession, trigger: dict) -> None:
    """Sniper-mode: walk the tree, raise 422 with ALL errors if any found.

    Returns silently if the tree is clean. Intended to be called from
    golden_build() right before engine.create_flow().
    """
    errs = await validate_trigger(session, trigger)
    if not errs:
        return
    raise HTTPException(
        status_code=422,
        detail={
            "message": "Flow failed piece-registry validation. "
                       "Fix the errors below and retry — no flow was created on Activepieces.",
            "errors": [asdict(e) for e in errs],
            "total_errors": len(errs),
        },
    )
