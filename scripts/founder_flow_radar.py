#!/usr/bin/env python3
"""
Founder Flow Radar — READ-ONLY classifier for the 10 golden + 141 legacy flows.

Goal
----
Identify which Activepieces flows belong to the founder, separate "golden"
(Siyadah-stamped via Sovereign Tightening) from "legacy" (pre-stamp / orphan),
and emit a deterministic report — without touching a single byte upstream.

Hard guarantees (do not weaken):
  • HTTP: only GET against AP + DB SELECT. No POST/PATCH/PUT/DELETE.
  • DB:   read-only session; no commits, no writes to flow_registry or any other table.
  • AP:   never invokes engine.update_metadata / delete_flow / publish_and_enable.
  • CLI:  no --apply, no --fix, no destructive flags. By design.

Classification (per flow, in this order — first match wins):
  1. FOUNDER_GOLDEN — metadata.stampedBy == "siyadah:golden_build" AND
                      (metadata.tenantId == --founder-tenant OR
                       metadata.ownerEmail == --founder-email)
  2. FOUNDER_LEGACY — flow lives in flow_registry under --founder-tenant
                      but lacks the sovereign stamp on AP metadata.
  3. GOLDEN          — sovereign-stamped, owner ≠ founder.
  4. LEGACY          — no sovereign stamp, no flow_registry row.
  5. ORPHAN          — sovereign-stamped, but flow_registry has no matching row
                      (mirror missed; AP is still ground truth).

Usage
-----
  # quick scan (founder identity from env)
  python scripts/founder_flow_radar.py \
      --founder-tenant <pid> \
      --founder-email <email>

  # write a JSON report alongside the human table
  python scripts/founder_flow_radar.py \
      --founder-tenant <pid> --founder-email <email> \
      --json-out reports/founder_radar_$(date +%Y%m%d).json

Exit codes
----------
  0 — completed successfully (regardless of how many founder flows found)
  2 — AP authentication failed
  3 — DB unavailable (we still emit AP-only rows)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class FlowRow:
    flow_id: str
    display_name: str
    status: str
    ap_tenant_id: Optional[str]              # from AP metadata.tenantId
    ap_owner_email: Optional[str]            # from AP metadata.ownerEmail
    ap_stamped_at: Optional[str]             # from AP metadata.stampedAt
    ap_stamped_by: Optional[str]             # from AP metadata.stampedBy
    registry_tenant_id: Optional[str] = None # from siyadah.flow_registry
    registry_display_name: Optional[str] = None
    registry_trigger_type: Optional[str] = None
    classification: str = "UNKNOWN"


def _classify(row: FlowRow, founder_tenant: Optional[str], founder_email: Optional[str]) -> str:
    sovereign_stamped = (
        row.ap_stamped_by == "siyadah:golden_build"
        and bool(row.ap_tenant_id)
    )
    in_founder_registry = (
        founder_tenant is not None
        and row.registry_tenant_id == founder_tenant
    )
    matches_founder = (
        (founder_tenant and row.ap_tenant_id == founder_tenant)
        or (founder_email and row.ap_owner_email == founder_email)
    )

    if sovereign_stamped and matches_founder:
        return "FOUNDER_GOLDEN"
    if in_founder_registry and not sovereign_stamped:
        return "FOUNDER_LEGACY"
    if sovereign_stamped and row.registry_tenant_id is None:
        return "ORPHAN"
    if sovereign_stamped:
        return "GOLDEN"
    return "LEGACY"


async def _fetch_ap_flows(project_id: str) -> list[dict[str, Any]]:
    # Lazy imports — match the orchestrator runtime so env (AP_BASE,
    # AP_EMAIL, AP_PASSWORD) is loaded the same way main.py loads it.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from main import SiyadahEngine, AP_BASE, AP_EMAIL, AP_PASSWORD  # noqa: E402

    if not (AP_BASE and AP_EMAIL and AP_PASSWORD):
        raise SystemExit("AP_BASE_URL/AP_EMAIL/AP_PASSWORD missing — cannot scan AP")

    token = await SiyadahEngine.sign_in(AP_EMAIL, AP_PASSWORD, AP_BASE)
    engine = SiyadahEngine(AP_BASE, token, email=AP_EMAIL, password=AP_PASSWORD)
    flows = await engine.list_flows(project_id)
    enriched: list[dict[str, Any]] = []
    for f in flows:
        try:
            full = await engine.get_flow(f["id"])
        except Exception as exc:
            full = {**f, "_radar_warning": f"get_flow failed: {exc}"}
        enriched.append(full)
    return enriched


async def _fetch_registry_index() -> dict[str, dict[str, Any]]:
    """Build {flow_id: {tenant_id, display_name, trigger_type}} from flow_registry. Read-only."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.environ.setdefault("SIYADAH_SKIP_PG_SSL", "1")
    try:
        from database import async_session
        from models import FlowRegistry
        from sqlalchemy import select
    except Exception as exc:
        print(f"⚠️  flow_registry unreachable ({exc}) — proceeding with AP-only data", file=sys.stderr)
        return {}

    if async_session is None:
        return {}

    out: dict[str, dict[str, Any]] = {}
    async with async_session() as s:
        # Read-only: bare SELECT, no commit, no flush.
        rows = (await s.execute(select(FlowRegistry))).scalars().all()
        for r in rows:
            out[r.flow_id] = {
                "tenant_id": r.tenant_id,
                "display_name": r.display_name,
                "trigger_type": r.trigger_type,
            }
    return out


def _extract_meta(flow: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    meta = flow.get("metadata") or {}
    if not isinstance(meta, dict):
        return None, None, None, None
    return (
        meta.get("tenantId"),
        meta.get("ownerEmail"),
        meta.get("stampedAt"),
        meta.get("stampedBy"),
    )


async def _run(args: argparse.Namespace) -> int:
    project_id = args.project_id or os.getenv("AP_PROJECT_ID", "")
    if not project_id:
        print("ERROR: --project-id missing and AP_PROJECT_ID env not set", file=sys.stderr)
        return 2

    try:
        ap_flows = await _fetch_ap_flows(project_id)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: AP scan failed: {exc}", file=sys.stderr)
        return 2

    registry = await _fetch_registry_index()

    rows: list[FlowRow] = []
    for f in ap_flows:
        fid = f.get("id", "")
        version = f.get("version") or {}
        display = version.get("displayName") or f.get("displayName") or "(no name)"
        status = f.get("status", "UNKNOWN")
        t_id, owner, stamped_at, stamped_by = _extract_meta(f)
        reg = registry.get(fid, {})
        row = FlowRow(
            flow_id=fid,
            display_name=display,
            status=status,
            ap_tenant_id=t_id,
            ap_owner_email=owner,
            ap_stamped_at=stamped_at,
            ap_stamped_by=stamped_by,
            registry_tenant_id=reg.get("tenant_id"),
            registry_display_name=reg.get("display_name"),
            registry_trigger_type=reg.get("trigger_type"),
        )
        row.classification = _classify(row, args.founder_tenant, args.founder_email)
        rows.append(row)

    # ── Print human table ────────────────────────────────────
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.classification] = counts.get(r.classification, 0) + 1

    print(f"\n🛡️  Founder Flow Radar — project={project_id}")
    print(f"    Scanned at: {datetime.now(timezone.utc).isoformat()}")
    print(f"    Total flows in AP: {len(rows)}")
    print(f"    Classification breakdown:")
    for label in ("FOUNDER_GOLDEN", "FOUNDER_LEGACY", "GOLDEN", "ORPHAN", "LEGACY"):
        print(f"      • {label:<16} {counts.get(label, 0):>4}")
    print()

    if not args.summary_only:
        # Show founder flows first, sorted by classification then name
        ordered = sorted(
            rows,
            key=lambda r: (
                0 if r.classification.startswith("FOUNDER") else
                1 if r.classification == "ORPHAN" else
                2 if r.classification == "GOLDEN" else 3,
                r.display_name.lower(),
            ),
        )
        print(f"{'Classification':<17}{'Flow ID':<40}{'Status':<10}{'Owner Email':<32}Display Name")
        print("─" * 130)
        for r in ordered:
            print(
                f"{r.classification:<17}"
                f"{(r.flow_id or '-'):<40}"
                f"{r.status:<10}"
                f"{(r.ap_owner_email or '-'):<32}"
                f"{r.display_name}"
            )
        print()

    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)) or ".", exist_ok=True)
        payload = {
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "project_id": project_id,
            "founder_tenant": args.founder_tenant,
            "founder_email": args.founder_email,
            "counts": counts,
            "flows": [asdict(r) for r in rows],
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"📄 JSON report → {args.json_out}")

    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="READ-ONLY radar: classify AP flows by founder ownership and Sovereign-stamp status.",
    )
    p.add_argument("--project-id", help="AP project id (defaults to AP_PROJECT_ID env)")
    p.add_argument("--founder-tenant", help="Founder's Siyadah project_id (matches AP metadata.tenantId)")
    p.add_argument("--founder-email", help="Founder's owner email (matches AP metadata.ownerEmail)")
    p.add_argument("--json-out", help="Optional path to write a JSON dump (best for diffs over time)")
    p.add_argument("--summary-only", action="store_true", help="Print counts only, suppress per-flow table")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
