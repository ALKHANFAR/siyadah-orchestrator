#!/usr/bin/env python3
"""
Operator monitoring CLI — summarise ``tenant_audit_log`` since a cutoff.

Designed to be the first thing you run after flipping
REQUIRE_TENANT_ENFORCE=true, and periodically during the dry-run
window. Prints three tables:

1. Violation breakdown (by kind × tenant).
2. Top 10 tenants by write volume.
3. Top 5 paths by 4xx/5xx rate (potential regressions).

Usage
-----
  # last 24h
  python scripts/audit_stats.py

  # last 7 days
  python scripts/audit_stats.py --hours 168

  # only violations
  python scripts/audit_stats.py --violations-only
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone


async def _run(args: argparse.Namespace) -> int:
    os.environ.setdefault("SIYADAH_SKIP_PG_SSL", "1")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from database import async_session
        from models import TenantAuditLog
        from sqlalchemy import func, select
    except Exception as exc:
        print(f"[audit] import failed: {exc}", file=sys.stderr)
        return 2

    if async_session is None:
        print("[audit] DATABASE_URL not configured", file=sys.stderr)
        return 2

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    async with async_session() as s:
        total = (await s.execute(
            select(func.count()).select_from(TenantAuditLog)
            .where(TenantAuditLog.occurred_at >= cutoff)
        )).scalar_one()

        # 1. violation breakdown
        vio_rows = (await s.execute(
            select(
                TenantAuditLog.violation,
                TenantAuditLog.project_id,
                func.count().label("n"),
            )
            .where(TenantAuditLog.occurred_at >= cutoff)
            .where(TenantAuditLog.violation.isnot(None))
            .group_by(TenantAuditLog.violation, TenantAuditLog.project_id)
            .order_by(func.count().desc())
        )).all()

        # 2. top tenants by write volume (clean writes only)
        if not args.violations_only:
            tenant_rows = (await s.execute(
                select(
                    TenantAuditLog.project_id,
                    func.count().label("n"),
                )
                .where(TenantAuditLog.occurred_at >= cutoff)
                .where(TenantAuditLog.violation.is_(None))
                .where(TenantAuditLog.project_id.isnot(None))
                .group_by(TenantAuditLog.project_id)
                .order_by(func.count().desc())
                .limit(10)
            )).all()

            # 3. hot error paths (status >= 400)
            error_rows = (await s.execute(
                select(
                    TenantAuditLog.endpoint,
                    func.count().label("n"),
                    func.avg(TenantAuditLog.http_status).label("avg_status"),
                )
                .where(TenantAuditLog.occurred_at >= cutoff)
                .where(TenantAuditLog.http_status >= 400)
                .group_by(TenantAuditLog.endpoint)
                .order_by(func.count().desc())
                .limit(5)
            )).all()
        else:
            tenant_rows = []
            error_rows = []

    # ── render
    print(f"\n━━━ audit stats — last {args.hours}h "
          f"(since {cutoff.isoformat(timespec='seconds')}) ━━━")
    print(f"  total rows: {total}")
    print()

    print(f"[violations]  total kinds: {len(vio_rows)}")
    if not vio_rows:
        print("  (clean — zero violations in window)")
    else:
        print(f"  {'violation':<26} {'tenant':<32} {'count':>6}")
        print(f"  {'-' * 26} {'-' * 32} {'-' * 6}")
        for v, pid, n in vio_rows[:25]:
            print(f"  {v or '<none>':<26} {str(pid or '<null>'):<32} {n:>6}")

    if not args.violations_only:
        print()
        print(f"[top tenants by clean writes]")
        if not tenant_rows:
            print("  (no clean writes in window)")
        else:
            print(f"  {'tenant':<40} {'count':>6}")
            print(f"  {'-' * 40} {'-' * 6}")
            for pid, n in tenant_rows:
                print(f"  {pid:<40} {n:>6}")

        print()
        print(f"[top error paths (4xx/5xx)]")
        if not error_rows:
            print("  (zero 4xx/5xx in window — clean)")
        else:
            print(f"  {'endpoint':<50} {'count':>6} {'avg_status':>12}")
            print(f"  {'-' * 50} {'-' * 6} {'-' * 12}")
            for ep, n, avg in error_rows:
                print(f"  {ep:<50} {n:>6} {float(avg):>12.1f}")

    # Exit non-zero if violations exist AND user asked us to gate a CI step.
    if args.fail_on_violations and vio_rows:
        print(f"\n[audit] FAIL: {len(vio_rows)} violation kinds found", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hours", type=int, default=24,
                   help="Window in hours (default: 24).")
    p.add_argument("--violations-only", action="store_true",
                   help="Only show the violations table.")
    p.add_argument("--fail-on-violations", action="store_true",
                   help="Exit non-zero if any violation rows exist. "
                        "For use in CI gating the flip to ENFORCE=true.")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
