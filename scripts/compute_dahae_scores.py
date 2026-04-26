#!/usr/bin/env python3
"""
Compute Dahae + Laziness + effective_dahae for every piece in the registry.

Implements §15 + §17 + §18.1 of the Sovereign Constitution.

  python -m scripts.compute_dahae_scores              # compute + write all
  python -m scripts.compute_dahae_scores --dry-run    # compute + print, don't write
  python -m scripts.compute_dahae_scores --verify     # spot-check known anchors

The math is purely from `piece_registry.actions_index`,
`piece_registry.triggers_index`, and `piece_registry.full_schema.projectUsage`.
Zero LLM cost. Zero network calls. ~2s for 688 pieces.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import math
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from sqlalchemy import select, update  # noqa: E402

log = logging.getLogger("dahae")


# ═══════════════════════════════════════════════════════════════
# Algorithm — pure functions, no I/O
# ═══════════════════════════════════════════════════════════════

KNOWN_VERBS = {
    # CRUD
    "create", "add", "new", "insert", "make",
    "read", "get", "fetch", "retrieve",
    "update", "patch", "edit", "modify", "set",
    "delete", "remove", "destroy",
    # Query
    "find", "search", "lookup", "query",
    "list", "enumerate", "index",
    # Communication
    "send", "post", "publish", "share",
    # Sync / merge
    "sync", "merge", "upsert",
    # Enrichment / AI
    "enrich", "analyze", "classify", "extract", "generate",
    "summarize", "translate", "transcribe", "embed",
    # Subscriptions
    "subscribe", "unsubscribe", "follow", "unfollow",
    # Approval / workflow
    "approve", "reject", "review", "submit",
    # IO
    "export", "import", "upload", "download",
    "archive", "restore",
    # Scheduling
    "schedule", "trigger", "invoke", "run", "execute",
    # Custom
    "custom", "call",
}


COMPOUND_PATTERN = re.compile(
    r"\b(and|with|or|then|after|before|"
    r"bulk|batch|many|multiple|all|each|every|"
    r"upsert|sync|merge|find_or_create|create_or_update|"
    r"associate|attach|link|connect)\b",
    re.IGNORECASE,
)


def tokenize(name: str) -> list[str]:
    return [t for t in name.replace("-", "_").lower().split("_") if t]


def extract_verb(name: str) -> str | None:
    for tok in tokenize(name):
        if tok in KNOWN_VERBS:
            return tok
    return None


def is_compound(name: str) -> bool:
    return bool(COMPOUND_PATTERN.search(name.replace("-", "_")))


# ──────────────────────── Dahae components ─────────────────────────

def compute_breadth(actions: dict) -> float:
    """How many distinct verbs the piece's actions cover."""
    if not actions:
        return 0.0
    verbs = {v for n in actions if (v := extract_verb(n)) is not None}
    return min(len(verbs) / 8.0, 1.0)


def compute_richness(actions: dict) -> float:
    """Average required_props per action (capped at 6)."""
    counts = [
        len((a or {}).get("required_props") or [])
        for a in actions.values()
    ] if actions else []
    if not counts:
        return 0.0
    avg = sum(counts) / len(counts)
    return min(avg / 6.0, 1.0)


def compute_compression(actions: dict) -> float:
    """Fraction of actions whose name implies multi-step semantics."""
    if not actions:
        return 0.0
    return sum(1 for n in actions if is_compound(n)) / len(actions)


def compute_symmetry(actions: dict, triggers: dict) -> float:
    """1.0 = both, 0.5 = one, 0 = neither."""
    has_a, has_t = bool(actions), bool(triggers)
    if has_a and has_t:
        return 1.0
    if has_a or has_t:
        return 0.5
    return 0.0


def compute_adoption(full_schema: dict) -> float:
    """log10-scaled projectUsage signal, capped at 10K projects."""
    pu = full_schema.get("projectUsage") if full_schema else None
    if not pu or pu <= 0:
        return 0.0
    return min(math.log10(1 + pu) / 4.0, 1.0)


# ──────────────────────── Laziness components ──────────────────────

def compute_empty_props_ratio(actions: dict) -> float:
    if not actions:
        return 0.0
    empty = sum(
        1 for a in actions.values()
        if not (a or {}).get("prop_types")
    )
    return empty / len(actions)


def compute_name_entropy(action_names: list[str]) -> float:
    """Shannon entropy of the token distribution across action names."""
    if not action_names:
        return 0.0
    tokens: list[str] = []
    for n in action_names:
        tokens.extend(tokenize(n))
    if not tokens:
        return 0.0
    counts = collections.Counter(tokens)
    total = sum(counts.values())
    h = -sum((c / total) * math.log2(c / total) for c in counts.values())
    return h


def compute_richness_variance(actions: dict) -> float:
    """Coefficient of variation of required_props counts."""
    if not actions or len(actions) < 2:
        return 0.0
    counts = [
        len((a or {}).get("required_props") or []) for a in actions.values()
    ]
    m = sum(counts) / len(counts)
    if m == 0:
        return 0.0
    var = sum((c - m) ** 2 for c in counts) / len(counts)
    cv = math.sqrt(var) / m
    return min(cv, 1.0)


# ──────────────────────── Composite ────────────────────────────────

def compute_all(piece_row) -> dict:
    actions = piece_row.actions_index or {}
    triggers = piece_row.triggers_index or {}
    full_schema = piece_row.full_schema or {}

    # Dahae
    breadth = compute_breadth(actions)
    richness = compute_richness(actions)
    compression = compute_compression(actions)
    symmetry = compute_symmetry(actions, triggers)
    adoption = compute_adoption(full_schema)
    dahae_raw = (
        0.30 * breadth
        + 0.25 * richness
        + 0.20 * compression
        + 0.15 * symmetry
        + 0.10 * adoption
    )
    dahae_score = round(dahae_raw * 100)

    # Laziness
    empty_ratio = compute_empty_props_ratio(actions)
    entropy = compute_name_entropy(list(actions.keys()))
    norm_entropy = min(entropy / 5.0, 1.0)
    var = compute_richness_variance(actions)
    laziness_raw = (
        0.40 * empty_ratio
        + 0.35 * (1.0 - norm_entropy)
        + 0.25 * (1.0 - var)
    )
    # If a piece has 0 actions, laziness math degenerates — treat as
    # neutral (50) to neither penalize nor reward trigger-only pieces.
    if not actions:
        laziness_score = 50
    else:
        laziness_score = round(laziness_raw * 100)

    effective_dahae = round(dahae_score * (1.0 - laziness_score / 100.0))

    return {
        "dahae_score": dahae_score,
        "laziness_score": laziness_score,
        "effective_dahae": effective_dahae,
        "breakdown": {
            "breadth": round(breadth, 3),
            "richness": round(richness, 3),
            "compression": round(compression, 3),
            "symmetry": symmetry,
            "adoption": round(adoption, 3),
            "empty_props_ratio": round(empty_ratio, 3),
            "name_entropy": round(entropy, 3),
            "richness_variance": round(var, 3),
            "n_actions": len(actions),
            "n_triggers": len(triggers),
        },
    }


# ═══════════════════════════════════════════════════════════════
# Verify-mode anchors (sanity checks)
# ═══════════════════════════════════════════════════════════════

VERIFY_ANCHORS = {
    # piece_short:    expected_band ('high' | 'mid' | 'low'), reason
    "slack":          ("high", "26 actions + 14 triggers + verb diversity"),
    "hubspot":        ("high", "45 actions + many compound names + high adoption"),
    "salesforce":     ("high", "27 actions across CRUD + sales objects"),
    "gmail":          ("mid",  "7 actions, one major verb (send)"),
    "typeform":       ("low",  "1 action only — custom_api_call"),
    "webhook":        ("low",  "2 actions, ~no triggers"),
    # 'ai' has only 6 atomic actions and no triggers — algorithm rates it
    # low by structural surface, even though the brand is well-known.
    # We trust the math, not the brand prior.
    "ai":             ("low",  "6 atomic actions, no triggers — low structural surface"),
    "google-sheets":  ("high", "21 actions + triggers + bulk semantics"),
    "openai":         ("mid",  "9 actions, mostly atomic"),
}


# Bands calibrated to the empirical effective_dahae distribution
# observed against 688 production pieces:
#   min=4  median=20  p75=28  p95=39  max=53
# So:
#   high = top ~5% (effective ≥ 40)
#   mid  = above median, below top 5% (20 ≤ effective < 40)
#   low  = at-or-below median (< 20)
def band(score: int) -> str:
    if score >= 40:
        return "high"
    if score >= 20:
        return "mid"
    return "low"


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    from database import async_session, engine, init_db
    from models import PieceRegistry

    if engine is None:
        log.error("DATABASE_URL not set")
        return 2

    if not args.dry_run:
        await init_db()  # ensures the new columns are present

    async with async_session() as s:
        rows = (await s.execute(
            select(PieceRegistry).order_by(PieceRegistry.name)
        )).scalars().all()

    log.info("Loaded %d pieces from registry", len(rows))

    results: list[dict] = []
    for row in rows:
        scores = compute_all(row)
        results.append({
            "name": row.name,
            "version": row.piece_version,
            **scores,
        })

    # Distribution
    dahae_vals = [r["dahae_score"] for r in results]
    eff_vals = [r["effective_dahae"] for r in results]
    laz_vals = [r["laziness_score"] for r in results]
    log.info("Distribution (effective_dahae):")
    log.info("  min=%d  median=%d  p75=%d  p95=%d  max=%d",
             min(eff_vals), sorted(eff_vals)[len(eff_vals)//2],
             sorted(eff_vals)[int(len(eff_vals)*0.75)],
             sorted(eff_vals)[int(len(eff_vals)*0.95)],
             max(eff_vals))

    if args.verify:
        log.info("\nAnchor verification:")
        ok = True
        for short, (expected, reason) in VERIFY_ANCHORS.items():
            full = f"@activepieces/piece-{short}"
            row_score = next(
                (r for r in results if r["name"] == full), None,
            )
            if not row_score:
                log.warning("  ⚠  %s NOT in registry", short)
                continue
            actual = band(row_score["effective_dahae"])
            mark = "✓" if actual == expected else "✗"
            log.info("  %s  %-20s effective=%d band=%s  expected=%s  (%s)",
                     mark, short, row_score["effective_dahae"], actual,
                     expected, reason)
            if actual != expected:
                ok = False
        if not ok:
            log.warning("Some anchors failed — algorithm may need tuning")

    # Top 10 / Bottom 10
    log.info("\nTop 10 by effective_dahae:")
    top = sorted(results, key=lambda r: r["effective_dahae"], reverse=True)[:10]
    for r in top:
        log.info("  %3d  %-46s  D=%2d  L=%2d  actions=%d",
                 r["effective_dahae"], r["name"][:46],
                 r["dahae_score"], r["laziness_score"],
                 r["breakdown"]["n_actions"])

    log.info("\nBottom 10 by effective_dahae (excluding zero-action triggers-only):")
    nonzero = [r for r in results if r["breakdown"]["n_actions"] > 0]
    bot = sorted(nonzero, key=lambda r: r["effective_dahae"])[:10]
    for r in bot:
        log.info("  %3d  %-46s  D=%2d  L=%2d  actions=%d",
                 r["effective_dahae"], r["name"][:46],
                 r["dahae_score"], r["laziness_score"],
                 r["breakdown"]["n_actions"])

    if args.dry_run:
        log.info("\n[dry-run] Would have UPDATEd %d rows", len(results))
        await engine.dispose()
        return 0

    # Write back
    log.info("\nWriting scores to piece_registry …")
    n_updated = 0
    async with async_session() as s:
        for r in results:
            await s.execute(
                update(PieceRegistry)
                .where(PieceRegistry.name == r["name"])
                .values(
                    dahae_score=r["dahae_score"],
                    laziness_score=r["laziness_score"],
                    effective_dahae=r["effective_dahae"],
                    dahae_breakdown=r["breakdown"],
                )
            )
            n_updated += 1
        await s.commit()
    log.info("Updated %d rows", n_updated)
    await engine.dispose()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute and print stats; do not write to DB.")
    ap.add_argument("--verify", action="store_true",
                    help="Spot-check known anchor pieces against expected bands.")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
