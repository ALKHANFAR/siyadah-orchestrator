#!/usr/bin/env python3
"""
Phase-9 migration runner with full forensic snapshot.

  1. Capture the As-Is state — every Siyadah-owned table + columns.
  2. Run init_db() (which now applies create_all + additive ALTERs).
  3. Capture the To-Be state.
  4. Print a structured diff so the operator can verify exactly
     which tables were created and which columns were added.

Idempotent — running this twice produces identical output on the second
run (no diff). That's the contract.

    DATABASE_URL=... SIYADAH_SKIP_PG_SSL=1 \
        python -m scripts.apply_phase9_migrations
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text   # noqa: E402

from database import engine, init_db   # noqa: E402


# Tables we own / care about. Excludes Activepieces' own tables which
# happen to share the same Postgres database (flow, flow_version, ...).
SIYADAH_TABLES = {
    "projects",
    "project_identities",
    "knowledge_assets",
    "autonomous_settings",
    "tenant_api_keys",
    "tenant_audit_log",
    "flow_registry",
    "piece_registry",
    "encrypted_tokens",       # Phase-9 NEW
    "oauth_sagas",            # Phase-9 NEW
}


async def snapshot() -> dict:
    """Return {table: [(column_name, data_type, is_nullable), ...]}
    for every Siyadah-owned table that exists right now."""
    if engine is None:
        raise SystemExit("DATABASE_URL not set")
    out: dict[str, list[tuple]] = {}
    async with engine.connect() as conn:
        existing = {r[0] for r in (await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ))).all()}
        for t in sorted(SIYADAH_TABLES):
            if t not in existing:
                out[t] = []   # marker: doesn't exist yet
                continue
            cols = (await conn.execute(text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = :t ORDER BY ordinal_position"
            ), {"t": t})).all()
            out[t] = [(c[0], c[1], c[2]) for c in cols]
    return out


def fmt_table_block(name: str, cols: list[tuple]) -> str:
    if not cols:
        return f"  {name}: ⨯ DOES NOT EXIST"
    lines = [f"  {name}: ({len(cols)} cols)"]
    for c, dt, nul in cols:
        lines.append(f"     • {c:30s} {dt:25s} {'NULL' if nul == 'YES' else 'NOT NULL'}")
    return "\n".join(lines)


def diff_snapshots(before: dict, after: dict) -> list[str]:
    """Compute the structural diff. Returns list of human-readable lines."""
    notes = []
    for t in sorted(SIYADAH_TABLES):
        b_cols = before.get(t, [])
        a_cols = after.get(t, [])
        b_names = {c[0] for c in b_cols}
        a_names = {c[0] for c in a_cols}

        if not b_cols and a_cols:
            notes.append(f"  + CREATED TABLE {t} ({len(a_cols)} columns)")
        elif b_cols and not a_cols:
            notes.append(f"  - DROPPED TABLE {t}  ⚠️")
        else:
            added = a_names - b_names
            dropped = b_names - a_names
            for col in sorted(added):
                col_info = next((c for c in a_cols if c[0] == col), None)
                if col_info:
                    notes.append(
                        f"  + ALTER TABLE {t} ADD COLUMN {col_info[0]} {col_info[1]}"
                    )
            for col in sorted(dropped):
                notes.append(f"  - ALTER TABLE {t} DROP COLUMN {col}  ⚠️")
    if not notes:
        notes.append("  (no changes — schema is already up-to-date, idempotent re-run)")
    return notes


async def main():
    print("┌─────────────────────────────────────────────────────────────────────")
    print("│ Phase-9 Migration Runner — Sovereign-Grade OAuth Schema")
    print("└─────────────────────────────────────────────────────────────────────")

    print("\n[1/4] Capturing As-Is snapshot from production Postgres …")
    before = await snapshot()

    print("\n══════════ AS-IS (before migration) ══════════")
    for t in sorted(SIYADAH_TABLES):
        print(fmt_table_block(t, before[t]))

    print("\n[2/4] Running init_db() — create_all + additive ALTERs …")
    await init_db()
    print("       ✓ init_db() returned cleanly")

    print("\n[3/4] Capturing To-Be snapshot …")
    after = await snapshot()

    print("\n══════════ TO-BE (after migration) ══════════")
    for t in sorted(SIYADAH_TABLES):
        print(fmt_table_block(t, after[t]))

    print("\n══════════ DIFF (As-Is → To-Be) ══════════")
    for line in diff_snapshots(before, after):
        print(line)

    print("\n[4/4] Verifying expected Phase-9 surface …")
    expected_new_tables = {"encrypted_tokens", "oauth_sagas"}
    expected_audit_cols = {"event_type", "event_meta"}

    after_tables = {t for t, cols in after.items() if cols}
    after_audit_cols = {c[0] for c in after.get("tenant_audit_log", [])}

    missing_tables = expected_new_tables - after_tables
    missing_cols = expected_audit_cols - after_audit_cols

    if not missing_tables and not missing_cols:
        print("       ✓ encrypted_tokens table present")
        print("       ✓ oauth_sagas table present")
        print("       ✓ tenant_audit_log.event_type column present")
        print("       ✓ tenant_audit_log.event_meta column present")
        print("\n  Phase-9 migrations PASSED — schema is Sovereign-Grade ready.")
        rc = 0
    else:
        if missing_tables:
            print(f"       ✗ MISSING tables: {missing_tables}")
        if missing_cols:
            print(f"       ✗ MISSING audit columns: {missing_cols}")
        rc = 1

    await engine.dispose()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
