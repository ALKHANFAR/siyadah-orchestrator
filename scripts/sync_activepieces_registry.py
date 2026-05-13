#!/usr/bin/env python3
"""Sync the file-based Activepieces piece registry under ``registry/``.

Activepieces is the only source of truth: this script fetches each piece
schema from the live AP API, normalises it into a deterministic JSON
shape, writes one file per piece under ``registry/pieces/`` and rebuilds
the cross-piece indexes under ``registry/indexes/``.

Usage
-----

    python -m scripts.sync_activepieces_registry --help

    # Live network, no disk writes
    python -m scripts.sync_activepieces_registry --dry-run

    # Sync the 10 default target pieces
    python -m scripts.sync_activepieces_registry

    # Sync a subset (comma-separated short names or full @activepieces/… names)
    python -m scripts.sync_activepieces_registry --pieces gmail,slack

    # Verify the on-disk registry without touching the network
    python -m scripts.sync_activepieces_registry --validate-only

Env vars
--------

    AP_BASE_URL                   (required; trailing /api/v1 is appended)
    AP_TOKEN | AP_MCP_TOKEN       (optional bearer; skips sign-in)
    AP_EMAIL + AP_PASSWORD        (optional; used to fetch a bearer)

Invariants
----------

* No secrets are written to any registry file or to logs.
* Schemas are recorded verbatim from AP; required/dropdown/dynamic field
  sets are derived from the AP ``type`` / ``required`` metadata only.
* Output is deterministic (`sort_keys=True`, atomic temp+rename).
* Non-zero exit on any fetch/parse/validate failure.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

log = logging.getLogger("sync_activepieces_registry")

# ── Constants ────────────────────────────────────────────────────────────

REGISTRY_FORMAT_VERSION = 1

DEFAULT_TARGETS: list[str] = [
    "@activepieces/piece-webhook",
    "@activepieces/piece-google-sheets",
    "@activepieces/piece-gmail",
    "@activepieces/piece-slack",
    "@activepieces/piece-hubspot",
    "@activepieces/piece-clickup",
    "@activepieces/piece-airtable",
    "@activepieces/piece-google-calendar",
    "@activepieces/piece-google-drive",
    "@activepieces/piece-stripe",
]

DROPDOWN_TYPES = {"DROPDOWN", "MULTI_SELECT_DROPDOWN"}
DYNAMIC_TYPES = {"DYNAMIC"}

# Where the registry lives, relative to this file's parent's parent
# (orchestrator/scripts/sync_activepieces_registry.py → orchestrator/).
_HERE = Path(__file__).resolve().parent
_ORCH_ROOT = _HERE.parent
REGISTRY_ROOT = _ORCH_ROOT / "registry"
PIECES_DIR = REGISTRY_ROOT / "pieces"
INDEXES_DIR = REGISTRY_ROOT / "indexes"

# ── Helpers ──────────────────────────────────────────────────────────────


def _short_name(piece_name: str) -> str:
    """Map ``@activepieces/piece-google-sheets`` → ``google-sheets``."""
    if piece_name.startswith("@activepieces/piece-"):
        return piece_name[len("@activepieces/piece-"):]
    return piece_name.replace("/", "_").replace("@", "")


def _canonical_piece_name(token: str) -> str:
    """Allow callers to pass either short slug or full piece name."""
    t = token.strip()
    if not t:
        return t
    if t.startswith("@activepieces/"):
        return t
    return f"@activepieces/piece-{t}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_json_bytes(payload: Any) -> bytes:
    """Bytes used for the schema hash. Sorted keys, no whitespace, UTF-8."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _schema_hash(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write ``data`` as deterministic JSON atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def _extract_auth(auth: Any) -> dict:
    """AP returns auth as None | dict | list[dict] (multi-auth pieces)."""
    if not auth:
        return {"type": None, "required": False}
    if isinstance(auth, dict):
        return {
            "type": auth.get("type"),
            "required": bool(auth.get("required", True)),
        }
    if isinstance(auth, list):
        for entry in auth:
            if isinstance(entry, dict) and entry.get("type"):
                return {
                    "type": entry.get("type"),
                    "required": bool(entry.get("required", True)),
                }
    return {"type": None, "required": False}


def _classify_props(props: Any) -> dict:
    """Split a props dict into required / dropdown / dynamic / type-map.

    Only the AP-reported ``type`` and ``required`` metadata is consulted.
    No name-based heuristics; no hand-rolled rules.
    """
    if not isinstance(props, dict):
        return {
            "required_fields": [],
            "dropdown_fields": [],
            "dynamic_fields": [],
            "property_types": {},
        }
    required: list[str] = []
    dropdown: list[str] = []
    dynamic: list[str] = []
    ptypes: dict[str, str] = {}
    for pname, pinfo in props.items():
        if not isinstance(pinfo, dict):
            continue
        ptype = pinfo.get("type", "") or ""
        ptypes[pname] = ptype
        if pinfo.get("required") is True:
            required.append(pname)
        if ptype in DROPDOWN_TYPES:
            dropdown.append(pname)
        if ptype in DYNAMIC_TYPES:
            dynamic.append(pname)
    return {
        "required_fields": sorted(required),
        "dropdown_fields": sorted(dropdown),
        "dynamic_fields": sorted(dynamic),
        "property_types": ptypes,
    }


def _build_action_or_trigger_entry(body: Any) -> dict:
    """Normalise one action / trigger entry from AP."""
    if not isinstance(body, dict):
        return {
            "displayName": None,
            "description": None,
            "required_fields": [],
            "dropdown_fields": [],
            "dynamic_fields": [],
            "property_types": {},
            "props": {},
        }
    props = body.get("props") or body.get("properties") or {}
    classified = _classify_props(props)
    return {
        "displayName": body.get("displayName"),
        "description": body.get("description"),
        "required_fields": classified["required_fields"],
        "dropdown_fields": classified["dropdown_fields"],
        "dynamic_fields": classified["dynamic_fields"],
        "property_types": classified["property_types"],
        "props": props,
    }


def _build_piece_record(schema: dict, source_url: str) -> dict:
    """Project an AP piece schema into the registry's on-disk shape."""
    actions_raw = schema.get("actions") if isinstance(schema.get("actions"), dict) else {}
    triggers_raw = schema.get("triggers") if isinstance(schema.get("triggers"), dict) else {}
    actions = {k: _build_action_or_trigger_entry(v) for k, v in actions_raw.items()}
    triggers = {k: _build_action_or_trigger_entry(v) for k, v in triggers_raw.items()}
    record = {
        "pieceName": schema.get("name", ""),
        "displayName": schema.get("displayName"),
        "version": schema.get("version"),
        "auth": _extract_auth(schema.get("auth")),
        "actions": actions,
        "triggers": triggers,
        "schema_hash": _schema_hash(schema),
        "synced_at": _now_iso(),
        "source_url": source_url,
        "registry_format_version": REGISTRY_FORMAT_VERSION,
    }
    return record


# ── AP client (no global state, no secret leakage) ───────────────────────


@dataclass
class APClientConfig:
    base_url: str
    token: str | None


async def _sign_in(client: httpx.AsyncClient, base: str, email: str, password: str) -> str:
    r = await client.post(
        f"{base}/api/v1/authentication/sign-in",
        json={"email": email, "password": password},
        timeout=30.0,
    )
    r.raise_for_status()
    token = r.json().get("token") or ""
    if not token:
        raise RuntimeError("AP sign-in returned no token")
    return token


async def _resolve_config(client: httpx.AsyncClient) -> APClientConfig:
    base = os.getenv("AP_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("AP_BASE_URL is not set")
    token = os.getenv("AP_TOKEN") or os.getenv("AP_MCP_TOKEN") or ""
    if not token:
        email = os.getenv("AP_EMAIL", "")
        password = os.getenv("AP_PASSWORD", "")
        if email and password:
            log.info("Signing in to %s …", base)
            token = await _sign_in(client, base, email, password)
        else:
            log.info(
                "No AP_TOKEN / AP_EMAIL+AP_PASSWORD set — fetching unauthenticated. "
                "This only works against instances that expose /api/v1/pieces/* publicly."
            )
    return APClientConfig(base_url=base, token=token or None)


def _auth_headers(cfg: APClientConfig) -> dict[str, str]:
    if cfg.token:
        return {"Authorization": f"Bearer {cfg.token}"}
    return {}


async def _get_piece(
    client: httpx.AsyncClient, cfg: APClientConfig, name: str, version: str | None = None,
) -> dict | None:
    params: dict[str, str] = {}
    if version:
        params["version"] = version
    url = f"{cfg.base_url}/api/v1/pieces/{name}"
    r = await client.get(url, params=params or None, headers=_auth_headers(cfg), timeout=30.0)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"AP returned non-object payload for {name}: {type(payload).__name__}")
    return payload


async def _fetch_full_schema(client: httpx.AsyncClient, cfg: APClientConfig, name: str) -> dict:
    """Two-pass fetch — AP collapses actions/triggers to an int for big pieces
    until version is supplied explicitly."""
    schema = await _get_piece(client, cfg, name)
    if schema is None:
        raise RuntimeError(f"AP returned 404 for {name}")
    actions = schema.get("actions")
    triggers = schema.get("triggers")
    if not isinstance(actions, dict) or not isinstance(triggers, dict):
        ver = schema.get("version") or ""
        if ver:
            schema2 = await _get_piece(client, cfg, name, ver)
            if schema2 is not None:
                schema = schema2
    actions = schema.get("actions")
    triggers = schema.get("triggers")
    if not isinstance(actions, dict) and not isinstance(triggers, dict):
        raise RuntimeError(
            f"AP returned a degenerate schema for {name}: actions and triggers are not dicts"
        )
    return schema


# ── Index builders (read-only over the on-disk registry) ─────────────────


def _load_piece_files() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not PIECES_DIR.is_dir():
        return out
    for p in sorted(PIECES_DIR.glob("*.json")):
        try:
            out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"Failed to parse {p}: {e}") from e
    return out


def _build_indexes(records: dict[str, dict]) -> dict[str, dict]:
    """Build all index dicts from on-disk piece records (keyed by short slug)."""
    actions_idx: dict[str, list[str]] = {}
    triggers_idx: dict[str, list[str]] = {}
    required_idx: dict[str, dict[str, list[str]]] = {}
    dropdown_idx: dict[str, dict[str, list[str]]] = {}
    dynamic_idx: dict[str, dict[str, list[str]]] = {}
    auth_idx: dict[str, dict] = {}
    hashes_idx: dict[str, dict] = {}

    for _slug, rec in sorted(records.items()):
        piece = rec.get("pieceName") or ""
        if not piece:
            continue
        actions: dict = rec.get("actions") or {}
        triggers: dict = rec.get("triggers") or {}
        actions_idx[piece] = sorted(actions.keys())
        triggers_idx[piece] = sorted(triggers.keys())
        required_idx[piece] = {
            a: list(actions[a].get("required_fields") or []) for a in sorted(actions.keys())
        }
        dropdown_idx[piece] = {
            a: list(actions[a].get("dropdown_fields") or []) for a in sorted(actions.keys())
        }
        dynamic_idx[piece] = {
            a: list(actions[a].get("dynamic_fields") or []) for a in sorted(actions.keys())
        }
        auth_idx[piece] = rec.get("auth") or {"type": None, "required": False}
        hashes_idx[piece] = {
            "hash": rec.get("schema_hash"),
            "version": rec.get("version"),
            "synced_at": rec.get("synced_at"),
            "source_url": rec.get("source_url"),
        }

    return {
        "actions_index.json": actions_idx,
        "triggers_index.json": triggers_idx,
        "required_fields_index.json": required_idx,
        "dropdown_fields_index.json": dropdown_idx,
        "dynamic_fields_index.json": dynamic_idx,
        "auth_index.json": auth_idx,
        "schema_hashes.json": hashes_idx,
    }


def _write_indexes(indexes: dict[str, dict]) -> list[Path]:
    written: list[Path] = []
    for fname, payload in indexes.items():
        out_path = INDEXES_DIR / fname
        _atomic_write_json(out_path, payload)
        written.append(out_path)
    return written


# ── Validate-only path ───────────────────────────────────────────────────


def _validate_existing(targets: Iterable[str]) -> int:
    """Verify the on-disk registry is internally consistent.

    Returns POSIX exit code: 0 ok, 1 inconsistent, 2 misconfig.
    """
    if not PIECES_DIR.is_dir():
        log.error("Registry pieces dir missing: %s", PIECES_DIR)
        return 1
    records = _load_piece_files()
    if not records:
        log.error("No piece files found under %s", PIECES_DIR)
        return 1
    errors: list[str] = []

    # 1. Each piece record must carry the same fields the schema specifies.
    for slug, rec in records.items():
        for key in ("pieceName", "version", "actions", "triggers", "auth",
                    "schema_hash", "synced_at", "registry_format_version"):
            if key not in rec:
                errors.append(f"{slug}: missing key '{key}'")
        if rec.get("registry_format_version") != REGISTRY_FORMAT_VERSION:
            errors.append(
                f"{slug}: registry_format_version mismatch "
                f"(file={rec.get('registry_format_version')} expected={REGISTRY_FORMAT_VERSION})"
            )

    # 2. Targets must all be present.
    target_slugs = {_short_name(_canonical_piece_name(t)) for t in targets}
    for slug in target_slugs:
        if slug not in records:
            errors.append(f"target piece missing on disk: {slug}.json")

    # 3. Indexes must be present and match a freshly-rebuilt set.
    expected = _build_indexes(records)
    for fname, expected_payload in expected.items():
        idx_path = INDEXES_DIR / fname
        if not idx_path.is_file():
            errors.append(f"index missing: {idx_path.name}")
            continue
        try:
            actual = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"{fname}: parse error {e}")
            continue
        if actual != expected_payload:
            errors.append(f"{fname}: drift between piece files and index")

    if errors:
        for e in errors:
            log.error("  - %s", e)
        log.error("Validation FAILED with %d issue(s)", len(errors))
        return 1
    log.info(
        "Validation OK: %d piece(s), %d index file(s), format v%d",
        len(records), len(expected), REGISTRY_FORMAT_VERSION,
    )
    return 0


# ── Main run loop ────────────────────────────────────────────────────────


async def _sync(args: argparse.Namespace) -> int:
    targets_raw: list[str] = (
        [t for t in args.pieces.split(",") if t.strip()] if args.pieces else list(DEFAULT_TARGETS)
    )
    targets = [_canonical_piece_name(t) for t in targets_raw]
    log.info("Targets: %s", ", ".join(targets))

    async with httpx.AsyncClient() as client:
        try:
            cfg = await _resolve_config(client)
        except Exception as e:
            log.error("AP client configuration failed: %s", e)
            return 2

        records_by_slug: dict[str, dict] = {}
        failures: list[tuple[str, str]] = []
        for piece in targets:
            slug = _short_name(piece)
            try:
                schema = await _fetch_full_schema(client, cfg, piece)
            except httpx.HTTPError as e:
                failures.append((piece, f"network: {e}"))
                continue
            except Exception as e:
                failures.append((piece, str(e)))
                continue
            record = _build_piece_record(
                schema, source_url=f"{cfg.base_url}/api/v1/pieces/{piece}"
            )
            records_by_slug[slug] = record
            log.info(
                "[fetched] %s v%s  actions=%d triggers=%d auth=%s",
                record["pieceName"], record["version"],
                len(record["actions"]), len(record["triggers"]),
                (record["auth"] or {}).get("type"),
            )

    if failures:
        for name, err in failures:
            log.error("[fail] %s — %s", name, err)
        log.error("%d/%d pieces failed to sync", len(failures), len(targets))
        return 1

    if args.dry_run:
        log.info(
            "[dry-run] would write %d piece file(s) and 7 index file(s) under %s",
            len(records_by_slug), REGISTRY_ROOT,
        )
        return 0

    # Write piece files first.
    PIECES_DIR.mkdir(parents=True, exist_ok=True)
    INDEXES_DIR.mkdir(parents=True, exist_ok=True)
    for slug, record in records_by_slug.items():
        _atomic_write_json(PIECES_DIR / f"{slug}.json", record)
        log.info("[wrote] pieces/%s.json", slug)

    # Then merge with any pre-existing piece files so partial syncs don't
    # destroy unrelated indexes.
    on_disk = _load_piece_files()
    merged = {**on_disk, **records_by_slug}
    indexes = _build_indexes(merged)
    written_idx = _write_indexes(indexes)
    for p in written_idx:
        log.info("[wrote] indexes/%s", p.name)

    log.info(
        "Done — %d piece(s) synced, %d index file(s) written", len(records_by_slug), len(written_idx),
    )
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--pieces", type=str, default="",
        help="Comma-separated short slugs or full @activepieces names. Default: 10 targets.",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Fetch live schemas but write nothing to disk.",
    )
    ap.add_argument(
        "--validate-only", action="store_true",
        help="Verify the on-disk registry is internally consistent. No network calls.",
    )
    return ap


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = _build_argparser().parse_args()
    if args.validate_only and args.dry_run:
        log.error("--validate-only and --dry-run are mutually exclusive")
        return 2
    if args.validate_only:
        targets = (
            [t for t in args.pieces.split(",") if t.strip()] if args.pieces else list(DEFAULT_TARGETS)
        )
        return _validate_existing(targets)
    return asyncio.run(_sync(args))


if __name__ == "__main__":
    raise SystemExit(main())
