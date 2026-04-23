#!/usr/bin/env python3
"""
Seed a raw API key into ``tenant_api_keys`` for a given project_id.

Safe to run against a live Postgres: hashes the raw key with sha256
before insert, never writes the raw value, and fails loudly on
duplicate (idempotent behaviour requires --rotate).

Usage
-----
  # Read DATABASE_URL from env, seed a new key pair interactively:
  python scripts/seed_tenant_key.py \\
      --project-id ou4jOTA4KMnDrzOVsKWvd \\
      --label siyadah65-bff-prod

  # With an explicit raw key (e.g. existing ORCHESTRATOR_API_KEY):
  ORCHESTRATOR_API_KEY=pasted-raw-key python scripts/seed_tenant_key.py \\
      --project-id <pid> --label siyadah65-bff-prod --from-env

  # Rotate: mark all active keys for this project as revoked then seed new:
  python scripts/seed_tenant_key.py --project-id <pid> --label new-key --rotate

Exits non-zero on any failure. The raw key is printed ONCE — capture it
into Railway env immediately; there is no way to recover it later.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import secrets
import sys
from datetime import datetime, timezone


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_raw_key(nbytes: int = 48) -> str:
    """48 bytes → 64-char urlsafe b64, minus padding."""
    return secrets.token_urlsafe(nbytes)


async def _run(args: argparse.Namespace) -> int:
    # Inject test env so database.py import doesn't crash when called
    # locally against a dev Postgres.
    os.environ.setdefault("SIYADAH_SKIP_PG_SSL", "1")

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from database import async_session
        from models import TenantApiKey, Project
        from sqlalchemy import select, update
    except Exception as exc:
        print(f"[seed] import failed: {exc}", file=sys.stderr)
        return 2

    if async_session is None:
        print("[seed] DATABASE_URL not configured", file=sys.stderr)
        return 2

    # Resolve the raw key.
    if args.from_env:
        raw = os.environ.get("ORCHESTRATOR_API_KEY", "").strip()
        if not raw:
            print("[seed] --from-env set but ORCHESTRATOR_API_KEY is empty",
                  file=sys.stderr)
            return 2
        source = "ORCHESTRATOR_API_KEY env"
    elif args.raw_key:
        raw = args.raw_key
        source = "--raw-key"
    else:
        raw = _generate_raw_key()
        source = "generated"

    key_hash = _sha256(raw)

    async with async_session() as s:
        # Sanity: project must exist (otherwise FK blows up).
        existing_proj = (await s.execute(
            select(Project).where(Project.project_id == args.project_id)
        )).scalar_one_or_none()
        if not existing_proj:
            print(f"[seed] project_id={args.project_id!r} not found in projects table. "
                  "Create the project row first, or run with --autocreate-project.",
                  file=sys.stderr)
            if not args.autocreate_project:
                return 3
            s.add(Project(project_id=args.project_id, name=f"auto-{args.project_id}"))
            await s.commit()
            print(f"[seed] auto-created project row: {args.project_id}")

        # Collision check on hash (UNIQUE constraint).
        dup = (await s.execute(
            select(TenantApiKey).where(TenantApiKey.key_hash == key_hash)
        )).scalar_one_or_none()
        if dup and dup.revoked_at is None:
            print(f"[seed] a live key row with this hash already exists "
                  f"(project={dup.project_id}, label={dup.label!r}). "
                  "Refusing to insert a duplicate — rotate instead.",
                  file=sys.stderr)
            return 4

        if args.rotate:
            revoked = await s.execute(
                update(TenantApiKey)
                .where(TenantApiKey.project_id == args.project_id)
                .where(TenantApiKey.revoked_at.is_(None))
                .values(revoked_at=datetime.now(timezone.utc))
            )
            n = getattr(revoked, "rowcount", None) or 0
            print(f"[seed] rotated: marked {n} active key(s) as revoked "
                  f"for project {args.project_id}")

        row = TenantApiKey(
            project_id=args.project_id,
            key_hash=key_hash,
            label=args.label,
            scopes=(args.scopes.split(",") if args.scopes else ["read", "write"]),
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)

    print()
    print("━" * 64)
    print("  KEY SEEDED".center(64))
    print("━" * 64)
    print(f"  project_id : {args.project_id}")
    print(f"  label      : {args.label}")
    print(f"  scopes     : {row.scopes}")
    print(f"  registry id: {row.id}")
    print(f"  key source : {source}")
    print()
    if source == "generated":
        print("  ⚠  Raw key (copy to Railway env NOW — not recoverable):")
        print(f"     {raw}")
    else:
        print(f"  key_hash (for verification): {key_hash[:16]}...")
    print("━" * 64)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-id", required=True)
    p.add_argument("--label", required=True,
                   help="Human-readable label (e.g. siyadah65-bff-prod)")
    p.add_argument("--raw-key", help="Supply a pre-existing raw key (for rotation "
                                     "from a previously-shared value).")
    p.add_argument("--from-env", action="store_true",
                   help="Use ORCHESTRATOR_API_KEY env as the raw key source.")
    p.add_argument("--rotate", action="store_true",
                   help="Revoke any active keys for this project first.")
    p.add_argument("--scopes", default="read,write",
                   help="Comma-separated scope list (default: read,write)")
    p.add_argument("--autocreate-project", action="store_true",
                   help="If the projects table lacks a row for project_id, "
                        "insert a stub row instead of erroring.")
    args = p.parse_args(argv)

    if args.raw_key and args.from_env:
        p.error("--raw-key and --from-env are mutually exclusive")

    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
