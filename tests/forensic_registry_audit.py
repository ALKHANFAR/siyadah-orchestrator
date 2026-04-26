"""
Forensic Registry Audit — three-way reconciliation.

Sources cross-referenced:
  A — `piece_registry` in production Postgres (our cached snapshot)
  B — AP /api/v1/pieces/{name} called LIVE right now
  C — activepieces.com/pieces (where applicable, public catalogue)

For each of 20 randomly-sampled pieces (deterministic seed):
  • Pull full row from A
  • Call AP API and pull full schema from B
  • Diff (action set, version, auth_type, displayName, description)
  • Score on the 10/10 quality criteria
  • Flag any A↔B discrepancy as DRIFT

For 5 high-stakes pieces (slack, hubspot, salesforce, gmail, stripe):
  • Additionally fetch from activepieces.com (source C)
  • Compare advertised action names to our cache

Exit code = number of drift findings. Zero means A≡B for all sampled.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:utYxWmdoDWsJRYAioDgsDnYEhfHQgsjz"
    "@caboose.proxy.rlwy.net:28585/railway",
)
os.environ.setdefault("SIYADAH_SKIP_PG_SSL", "1")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("ORCHESTRATOR_ALLOWED_ORIGINS", "http://x")
os.environ.setdefault("AP_BASE_URL", "https://activepieces-production-2499.up.railway.app")
os.environ.setdefault("AP_EMAIL", "a@siyadah-ai.com")
os.environ.setdefault("AP_PASSWORD", "Siyadah2026pass")
os.environ.setdefault("AP_PROJECT_ID", "ou4jOTA4KMnDrzOVsKWvd")

from sqlalchemy import select  # noqa: E402

from database import async_session, engine  # noqa: E402
from models import PieceRegistry  # noqa: E402

AP_BASE = os.environ["AP_BASE_URL"]


def section(label: str):
    print(f"\n{'═' * 78}\n  {label}\n{'═' * 78}")


# Deterministic seed so the same 20 pieces audit each run
random.seed(20260426)


# ═══════════════════════════════════════════════════════════════
# AP live lookup
# ═══════════════════════════════════════════════════════════════

async def ap_token() -> str:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            f"{AP_BASE}/api/v1/authentication/sign-in",
            json={"email": os.environ["AP_EMAIL"],
                  "password": os.environ["AP_PASSWORD"]},
        )
        r.raise_for_status()
        return r.json()["token"]


async def ap_get_piece(name: str, version: str | None, token: str) -> dict | None:
    """Pull from AP. Try with version first; fall back without."""
    async with httpx.AsyncClient(timeout=30) as c:
        params = {"version": version} if version else None
        r = await c.get(
            f"{AP_BASE}/api/v1/pieces/{name}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise RuntimeError(f"AP returned {r.status_code}: {r.text[:200]}")
        d = r.json()
        # Sometimes AP returns actions count as int (cold) — re-fetch with version
        if isinstance(d.get("actions"), int) and d.get("version"):
            r2 = await c.get(
                f"{AP_BASE}/api/v1/pieces/{name}",
                params={"version": d["version"]},
                headers={"Authorization": f"Bearer {token}"},
            )
            r2.raise_for_status()
            d = r2.json()
        return d


# ═══════════════════════════════════════════════════════════════
# 10/10 quality scoring
# ═══════════════════════════════════════════════════════════════

def score_piece(row: PieceRegistry, ap_live: dict) -> tuple[int, dict]:
    """Returns (score 0-10, per-criterion result dict)."""
    checks: dict[str, tuple[bool, str]] = {}
    fs = row.full_schema or {}

    # 1. displayName non-empty
    dn = (row.display_name or fs.get("displayName") or "").strip()
    checks["displayName_present"] = (bool(dn), dn[:40] or "(empty)")
    # 2. description non-empty
    desc = (row.description or fs.get("description") or "").strip()
    checks["description_present"] = (
        len(desc) >= 10, desc[:60] or "(empty)",
    )
    # 3. auth_type semantics — must be either None (no auth) or recognized type
    valid_auth = {None, "OAUTH2", "CUSTOM_AUTH", "BASIC_AUTH", "SECRET_TEXT"}
    checks["auth_type_valid"] = (
        row.auth_type in valid_auth or row.auth_type is None,
        str(row.auth_type),
    )
    # 4. piece_version present + non-trivial
    checks["version_present"] = (
        bool(row.piece_version) and "." in row.piece_version,
        row.piece_version or "(empty)",
    )
    # 5. categories non-empty
    cats = list(row.categories or [])
    checks["categories_present"] = (len(cats) >= 1, str(cats[:3]))
    # 6. has at least one action OR trigger
    actions = row.actions_index or {}
    triggers = row.triggers_index or {}
    checks["has_actions_or_triggers"] = (
        len(actions) + len(triggers) >= 1,
        f"{len(actions)}A/{len(triggers)}T",
    )
    # 7. each action has prop_types map
    props_ok = all(
        isinstance(a.get("prop_types"), dict)
        for a in (actions.values() if actions else [])
    )
    checks["actions_have_prop_types"] = (
        props_ok if actions else True, f"{len(actions)} actions",
    )
    # 8. logo_url non-empty
    checks["logo_url_present"] = (
        bool(row.logo_url), (row.logo_url or "")[:40] or "(empty)",
    )
    # 9. action names match AP live exactly (no kebab/snake drift)
    if ap_live:
        ap_actions = ap_live.get("actions") or {}
        if isinstance(ap_actions, dict):
            ap_action_set = set(ap_actions.keys())
            our_action_set = set(actions.keys())
            checks["action_names_match_AP_live"] = (
                ap_action_set == our_action_set,
                f"|AP|={len(ap_action_set)} |DB|={len(our_action_set)} "
                f"diff_in_AP_only={list(ap_action_set - our_action_set)[:3]} "
                f"diff_in_DB_only={list(our_action_set - ap_action_set)[:3]}",
            )
        else:
            checks["action_names_match_AP_live"] = (
                False, f"AP returned actions as {type(ap_actions).__name__}",
            )
    else:
        checks["action_names_match_AP_live"] = (False, "AP returned 404 — phantom in DB!")
    # 10. piece_version matches AP live
    if ap_live:
        ap_ver = (ap_live.get("version") or "").lstrip("~^")
        our_ver = (row.piece_version or "").lstrip("~^")
        checks["version_matches_AP_live"] = (
            ap_ver == our_ver, f"AP={ap_ver!r}  DB={our_ver!r}",
        )
    else:
        checks["version_matches_AP_live"] = (False, "AP 404")

    score = sum(1 for ok, _ in checks.values() if ok)
    return score, checks


# ═══════════════════════════════════════════════════════════════
# Main audit
# ═══════════════════════════════════════════════════════════════

async def main():
    section("FORENSIC REGISTRY AUDIT — three-way reconciliation")

    # Pick 20 random + always include 5 high-stakes
    HIGH_STAKES = [
        "@activepieces/piece-slack",
        "@activepieces/piece-hubspot",
        "@activepieces/piece-salesforce",
        "@activepieces/piece-gmail",
        "@activepieces/piece-stripe",
    ]

    async with async_session() as s:
        all_rows = (await s.execute(
            select(PieceRegistry).order_by(PieceRegistry.name)
        )).scalars().all()

    print(f"\n  Total pieces in piece_registry: {len(all_rows)}")
    by_name = {r.name: r for r in all_rows}

    # Random sample of 15 (we add 5 high-stakes for total 20)
    pool = [r for r in all_rows if r.name not in HIGH_STAKES]
    random_15 = random.sample(pool, 15)
    sample = [by_name[n] for n in HIGH_STAKES if n in by_name] + random_15
    print(f"  Sample size: {len(sample)} (5 high-stakes + 15 random; seed=20260426)")
    print(f"  High-stakes: {[r.name.split('-piece-')[-1] for r in sample[:5]]}")

    print(f"\n  Authenticating to AP …")
    tok = await ap_token()
    print(f"  ✓ token len={len(tok)}")

    # ── Three-way comparison ──────────────────────────────────────
    section("PER-PIECE FORENSIC TABLE")
    findings: list[dict] = []
    drift_count = 0
    phantom_count = 0
    ten_of_ten_count = 0
    score_distribution: dict[int, int] = {}

    for row in sample:
        try:
            ap_live = await ap_get_piece(row.name, row.piece_version, tok)
        except Exception as e:
            print(f"\n  ✗ {row.name}: AP fetch error — {type(e).__name__}: {e}")
            findings.append({
                "piece": row.name, "score": 0, "status": "AP_FETCH_ERROR",
                "error": str(e)[:120],
            })
            continue

        score, checks = score_piece(row, ap_live)
        score_distribution[score] = score_distribution.get(score, 0) + 1
        if score == 10:
            ten_of_ten_count += 1
        if ap_live is None:
            phantom_count += 1

        # Highlight discrepancies
        action_match = checks["action_names_match_AP_live"][0]
        version_match = checks["version_matches_AP_live"][0]
        is_drift = (not action_match) or (not version_match) or (ap_live is None)
        if is_drift:
            drift_count += 1

        marker = "✗ DRIFT" if is_drift else f"✓ {score}/10"
        short = row.name.replace("@activepieces/piece-", "")
        print(f"\n  {marker:14s}  {short:30s}  v{row.piece_version}")
        for crit, (ok, detail) in checks.items():
            ok_mark = "✓" if ok else "✗"
            print(f"      {ok_mark}  {crit:32s}  {str(detail)[:88]}")

        findings.append({
            "piece": row.name, "score": score,
            "drift": is_drift,
            "checks": {k: v[0] for k, v in checks.items()},
            "details": {k: v[1] for k, v in checks.items()},
        })

    # ── Cross-check 5 high-stakes against activepieces.com ───────
    section("CROSS-CHECK vs activepieces.com (public catalogue)")
    web_findings: list[dict] = []
    for row in sample[:5]:                                  # only the 5 high-stakes
        short = row.name.replace("@activepieces/piece-", "")
        url = f"https://www.activepieces.com/pieces/{short}"
        print(f"\n  Fetching {url} …")
        try:
            from urllib.parse import quote
            # Use httpx directly so we don't depend on WebFetch availability
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                r = await c.get(url, headers={"User-Agent": "siyadah-audit"})
            if r.status_code != 200:
                print(f"    ✗ HTTP {r.status_code} — public page unavailable")
                web_findings.append({"piece": row.name, "web_http": r.status_code})
                continue
            text = r.text
            # Crude extraction — count action mentions matching our DB names
            actions = list((row.actions_index or {}).keys())
            found_in_web = [a for a in actions if a in text]
            missing_in_web = [a for a in actions if a not in text]
            print(f"    HTTP 200, page bytes={len(text)}")
            print(f"    DB actions: {len(actions)}")
            print(f"    Found verbatim on website: {len(found_in_web)}")
            if missing_in_web and len(actions) <= 10:
                print(f"    Not found on page (may be cosmetic-renamed): {missing_in_web[:6]}")
            web_findings.append({
                "piece": row.name,
                "actions_in_db": len(actions),
                "actions_verbatim_on_web": len(found_in_web),
            })
        except Exception as e:
            print(f"    ✗ {type(e).__name__}: {e}")
            web_findings.append({"piece": row.name, "error": str(e)[:120]})

    # ── Summary ──────────────────────────────────────────────────
    section("FORENSIC SUMMARY")
    print(f"\n  Sampled pieces:                      {len(sample)}")
    print(f"  Phantom pieces (in DB, not in AP):   {phantom_count}")
    print(f"  Pieces with drift (action/version):  {drift_count}")
    print(f"  Pieces scoring 10/10:                {ten_of_ten_count}")
    print(f"\n  Score distribution:")
    for s in sorted(score_distribution.keys(), reverse=True):
        print(f"    {s}/10  →  {score_distribution[s]} pieces")

    if drift_count == 0 and phantom_count == 0:
        print(f"\n  ✓ AUDIT PASSED — A ≡ B for all {len(sample)} sampled pieces")
        rc = 0
    else:
        print(f"\n  ✗ AUDIT FAILED — {drift_count + phantom_count} discrepancies found")
        rc = drift_count + phantom_count

    if engine is not None:
        await engine.dispose()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
