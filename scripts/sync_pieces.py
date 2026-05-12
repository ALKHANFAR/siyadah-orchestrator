#!/usr/bin/env python3
"""
Piece Vault harvester — populate piece_registry from Activepieces.

CLI-ONLY. Deliberately NOT wired into startup or a HTTP endpoint — a
688-piece fetch takes ~2–3 minutes and would fail Railway's health
check on every deploy. Operator runs this once after first deploy and
whenever AP ships new pieces:

    # First-time full sync
    python -m scripts.sync_pieces --full

    # Incremental (skip pieces already up-to-date, default)
    python -m scripts.sync_pieces

    # Single piece (debugging / after a hotfix)
    python -m scripts.sync_pieces --piece @activepieces/piece-gmail

    # Dry run — print what would change, touch nothing
    python -m scripts.sync_pieces --dry-run

Env required: AP_BASE_URL, AP_EMAIL, AP_PASSWORD, DATABASE_URL.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
load_dotenv()


import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

# Make `orchestrator/` importable whether invoked as `python -m scripts...`
# from the repo root or directly.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from database import async_session, engine, init_db  # noqa: E402
from models import PieceRegistry  # noqa: E402

log = logging.getLogger("sync_pieces")

BATCH_SIZE = 50
CONCURRENCY = 10
INCREMENTAL_SKIP_HOURS = 24 * 7  # skip re-fetch if synced within the last week


# ═══════════════════════════════════════════════════════════════
# AP client
# ═══════════════════════════════════════════════════════════════

async def _sign_in(client: httpx.AsyncClient, base: str, email: str, password: str) -> str:
    r = await client.post(
        f"{base}/api/v1/authentication/sign-in",
        json={"email": email, "password": password},
    )
    r.raise_for_status()
    token = r.json().get("token", "")
    if not token:
        raise RuntimeError("AP sign-in returned no token")
    return token


async def _list_pieces(client: httpx.AsyncClient, base: str, token: str) -> list[dict]:
    r = await client.get(
        f"{base}/api/v1/pieces/",
        params={"includeHidden": "false"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else data.get("data", [])
    return items


async def _get_piece(
    client: httpx.AsyncClient, base: str, token: str, name: str, version: str | None = None,
) -> dict | None:
    params: dict[str, str] = {}
    if version:
        params["version"] = version
    r = await client.get(
        f"{base}/api/v1/pieces/{name}",
        params=params or None,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


# ═══════════════════════════════════════════════════════════════
# Schema → registry row
# ═══════════════════════════════════════════════════════════════

def _build_index(bucket: Any) -> dict[str, dict]:
    """Derive {name: {required_props, prop_types}} from AP's actions/triggers dict."""
    if not isinstance(bucket, dict):
        return {}
    out: dict[str, dict] = {}
    for key, body in bucket.items():
        if not isinstance(body, dict):
            continue
        props = body.get("props") or body.get("properties") or {}
        required: list[str] = []
        ptypes: dict[str, str] = {}
        for pname, pinfo in props.items() if isinstance(props, dict) else []:
            if not isinstance(pinfo, dict):
                continue
            ptypes[pname] = pinfo.get("type", "")
            if pinfo.get("required"):
                required.append(pname)
        out[key] = {"required_props": required, "prop_types": ptypes}
    return out


def _extract_auth_type(auth: Any) -> str | None:
    """AP returns auth as None / dict / list-of-dicts (multi-auth pieces).

    For our presence check we only need to know whether the piece requires
    *some* auth. Returns the first non-empty type string found, or None.
    """
    if not auth:
        return None
    if isinstance(auth, dict):
        return auth.get("type") or None
    if isinstance(auth, list):
        for entry in auth:
            if isinstance(entry, dict) and entry.get("type"):
                return entry["type"]
    return None


def _row_from_schema(schema: dict) -> dict:
    return {
        "name": schema.get("name", ""),
        "piece_version": schema.get("version", ""),
        "display_name": schema.get("displayName") or schema.get("name"),
        "description": schema.get("description"),
        "logo_url": schema.get("logoUrl"),
        "categories": schema.get("categories") or [],
        "auth_type": _extract_auth_type(schema.get("auth")),
        "full_schema": schema,
        "actions_index": _build_index(schema.get("actions")),
        "triggers_index": _build_index(schema.get("triggers")),
    }


# ═══════════════════════════════════════════════════════════════
# Orchestration
# ═══════════════════════════════════════════════════════════════

async def _sync_one(
    client: httpx.AsyncClient, base: str, token: str, name: str,
    dry_run: bool, stats: dict,
) -> None:
    try:
        schema = await _get_piece(client, base, token, name)
    except Exception as e:
        log.warning("[miss] %s: fetch failed (%s)", name, e)
        stats["failed"] += 1
        return
    if not schema:
        log.warning("[miss] %s: not found", name)
        stats["failed"] += 1
        return
    row = _row_from_schema(schema)
    # AP's action dict can come back as an int ("count-only") for big pieces —
    # re-fetch with explicit version to force full payload.
    if not row["actions_index"] and not row["triggers_index"]:
        ver = row["piece_version"]
        if ver:
            schema2 = await _get_piece(client, base, token, name, ver)
            if schema2:
                row = _row_from_schema(schema2)
    if not row["name"] or not row["piece_version"]:
        log.warning("[skip] %s: schema missing name/version", name)
        stats["failed"] += 1
        return

    if dry_run:
        log.info("[dry] would upsert %s v%s (%d actions, %d triggers)",
                 row["name"], row["piece_version"],
                 len(row["actions_index"]), len(row["triggers_index"]))
        stats["would_upsert"] += 1
        return

    if async_session is None:
        raise RuntimeError("DATABASE_URL not configured")
    async with async_session() as s:
        stmt = pg_insert(PieceRegistry).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["name", "piece_version"],
            set_={
                "display_name": stmt.excluded.display_name,
                "description": stmt.excluded.description,
                "logo_url": stmt.excluded.logo_url,
                "categories": stmt.excluded.categories,
                "auth_type": stmt.excluded.auth_type,
                "full_schema": stmt.excluded.full_schema,
                "actions_index": stmt.excluded.actions_index,
                "triggers_index": stmt.excluded.triggers_index,
            },
        )
        await s.execute(stmt)
        await s.commit()
    log.info("[ok] %s v%s (%d actions, %d triggers)",
             row["name"], row["piece_version"],
             len(row["actions_index"]), len(row["triggers_index"]))
    stats["upserted"] += 1


async def _already_fresh(name_versions: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """Return (name, version) pairs synced within INCREMENTAL_SKIP_HOURS."""
    if async_session is None or not name_versions:
        return set()
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=INCREMENTAL_SKIP_HOURS)
    async with async_session() as s:
        rows = (await s.execute(
            select(PieceRegistry.name, PieceRegistry.piece_version)
            .where(PieceRegistry.last_synced >= cutoff)
        )).all()
    return {(n, v) for (n, v) in rows}


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    base = os.getenv("AP_BASE_URL", "").rstrip("/")
    email = os.getenv("AP_EMAIL", "")
    password = os.getenv("AP_PASSWORD", "")
    if not (base and email and password):
        log.error("AP_BASE_URL / AP_EMAIL / AP_PASSWORD must be set")
        return 2

    if not args.dry_run and engine is None:
        log.error("DATABASE_URL not set — refusing to run non-dry sync")
        return 2

    if not args.dry_run:
        await init_db()

    t0 = time.time()
    stats = {"upserted": 0, "would_upsert": 0, "skipped": 0, "failed": 0}

    async with httpx.AsyncClient(
        timeout=60.0,
        limits=httpx.Limits(max_connections=CONCURRENCY * 2,
                            max_keepalive_connections=CONCURRENCY),
    ) as client:
        log.info("Signing in to %s …", base)
        token = await _sign_in(client, base, email, password)

        if args.piece:
            targets: list[tuple[str, str]] = [(args.piece, "")]
            log.info("Targeting single piece: %s", args.piece)
        else:
            log.info("Listing pieces from AP …")
            pieces = await _list_pieces(client, base, token)
            log.info("Discovered %d pieces", len(pieces))
            targets = [
                (p.get("name", ""), p.get("version", ""))
                for p in pieces
                if p.get("name")
            ]

        fresh: set[tuple[str, str]] = set()
        if not args.full and not args.piece:
            fresh = await _already_fresh(targets)
            if fresh:
                log.info("Incremental: skipping %d pieces synced within last %dh",
                         len(fresh), INCREMENTAL_SKIP_HOURS)

        work = [n for (n, v) in targets if (n, v) not in fresh and n]
        stats["skipped"] = len(targets) - len(work)

        sem = asyncio.Semaphore(CONCURRENCY)

        async def guarded(name: str) -> None:
            async with sem:
                await _sync_one(client, base, token, name, args.dry_run, stats)

        # Process in batches of BATCH_SIZE to bound memory and surface
        # mid-run progress cleanly.
        for batch_start in range(0, len(work), BATCH_SIZE):
            batch = work[batch_start:batch_start + BATCH_SIZE]
            log.info("--- Batch %d/%d (%d pieces) ---",
                     batch_start // BATCH_SIZE + 1,
                     (len(work) + BATCH_SIZE - 1) // BATCH_SIZE,
                     len(batch))
            await asyncio.gather(*(guarded(n) for n in batch))

    elapsed = time.time() - t0
    log.info(
        "Done in %.1fs — upserted=%d would_upsert=%d skipped=%d failed=%d",
        elapsed, stats["upserted"], stats["would_upsert"],
        stats["skipped"], stats["failed"],
    )
    if engine is not None:
        await engine.dispose()
    return 0 if stats["failed"] == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument(
        "--full", action="store_true",
        help="Refresh every piece, ignoring the 7-day freshness window.",
    )
    ap.add_argument(
        "--piece", type=str, default="",
        help="Sync a single piece by canonical name (e.g. @activepieces/piece-gmail).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change; touch neither DB nor anything else.",
    )
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
