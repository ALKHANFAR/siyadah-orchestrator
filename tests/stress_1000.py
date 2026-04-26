#!/usr/bin/env python3
"""
Sovereign 1000-Concurrent Stress Test
======================================

Fires 1000 authenticated requests at the production orchestrator's
`/v2/logic/suggest` endpoint (the live Dahae ranker we shipped in Phase
12a, which exercises the Postgres pool: ProjectIdentity + KnowledgeAsset
read + the Dahae query against piece_registry).

What this proves
----------------
- Latency distribution under burst load (p50 / p95 / p99 / max)
- Error rate under contention
- Postgres pool behaviour: pool_size=5 + the orchestrator's connection
  reuse pattern. If the SELECT FOR UPDATE SKIP LOCKED + processing_until
  lease infrastructure is honest, we should see clean queueing — not
  cascading failures.
- Whether the freshly-shipped /v2/logic/suggest correctly returns
  recommendations on every successful call.

Usage
-----
  STRESS_API_KEY=<raw>  STRESS_PROJECT_ID=ou4jOTA4KMnDrzOVsKWvd \\
      python -m tests.stress_1000

Optional env:
  STRESS_TARGET_URL  — default: production
  STRESS_TOTAL       — default: 1000
  STRESS_CONCURRENCY — default: 100
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


TARGET_URL = os.environ.get(
    "STRESS_TARGET_URL",
    "https://siyadah-orchestrator-production.up.railway.app",
)
API_KEY = os.environ.get("STRESS_API_KEY", "")
PROJECT_ID = os.environ.get("STRESS_PROJECT_ID", "ou4jOTA4KMnDrzOVsKWvd")
TOTAL = int(os.environ.get("STRESS_TOTAL", "1000"))
CONCURRENCY = int(os.environ.get("STRESS_CONCURRENCY", "100"))


ENDPOINT = os.environ.get("STRESS_ENDPOINT", "/v2/logic/suggest")
METHOD = os.environ.get("STRESS_METHOD", "POST")


async def _one(client: httpx.AsyncClient, sem: asyncio.Semaphore, idx: int):
    async with sem:
        t0 = time.perf_counter()
        try:
            if METHOD == "GET":
                r = await client.get(
                    ENDPOINT,
                    headers={
                        "X-API-Key": API_KEY,
                        "X-Siyadah-Tenant": PROJECT_ID,
                    },
                    timeout=60.0,
                )
            else:
                r = await client.post(
                    ENDPOINT,
                    json={"project_id": PROJECT_ID},
                    headers={
                        "X-API-Key": API_KEY,
                        "X-Siyadah-Tenant": PROJECT_ID,
                        "Content-Type": "application/json",
                    },
                    timeout=60.0,
                )
            dur_ms = (time.perf_counter() - t0) * 1000.0
            body_ok = False
            n_pieces = 0
            if r.status_code == 200:
                try:
                    j = r.json()
                    body_ok = isinstance(j.get("suggestions"), list)
                    n_pieces = len(j.get("recommended_pieces") or [])
                except Exception:
                    pass
            return {
                "idx": idx,
                "status": r.status_code,
                "dur_ms": dur_ms,
                "body_ok": body_ok,
                "n_pieces": n_pieces,
                "error": None,
            }
        except Exception as e:
            return {
                "idx": idx,
                "status": -1,
                "dur_ms": (time.perf_counter() - t0) * 1000.0,
                "body_ok": False,
                "n_pieces": 0,
                "error": f"{type(e).__name__}: {str(e)[:120]}",
            }


async def main() -> int:
    # API key only required for /v2/* endpoints
    if ENDPOINT.startswith("/v2/") and not API_KEY:
        print("ERROR: STRESS_API_KEY env not set (required for /v2/*).", file=sys.stderr)
        return 2

    print("=" * 70)
    print(f" 1000-CONCURRENT STRESS TEST")
    print("=" * 70)
    print(f"  Target:      {TARGET_URL}")
    print(f"  Endpoint:    {METHOD} {ENDPOINT}")
    print(f"  Total:       {TOTAL}")
    print(f"  Concurrency: {CONCURRENCY}  (max in-flight)")
    print(f"  Tenant:      {PROJECT_ID}")
    print("=" * 70)

    sem = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(
        max_connections=CONCURRENCY,
        max_keepalive_connections=CONCURRENCY,
    )

    t_start = time.perf_counter()
    async with httpx.AsyncClient(
        base_url=TARGET_URL, limits=limits, timeout=30.0,
    ) as client:
        results = await asyncio.gather(
            *[_one(client, sem, i) for i in range(TOTAL)]
        )
    wall = time.perf_counter() - t_start

    # Stats
    statuses = Counter(r["status"] for r in results)
    # body_ok only matters when we expect a suggest-shaped JSON response
    require_body = ENDPOINT.startswith("/v2/logic/suggest")
    if require_body:
        successes = [r for r in results if r["status"] == 200 and r["body_ok"]]
    else:
        successes = [r for r in results if r["status"] == 200]
    failures = [r for r in results if r not in successes]

    durations_success = sorted(r["dur_ms"] for r in successes)
    durations_all = sorted(r["dur_ms"] for r in results)
    err_codes = Counter(
        r["error"].split(":")[0] if r["error"] else f"HTTP_{r['status']}"
        for r in failures
    )

    def pct(vals, p):
        if not vals:
            return 0
        i = max(0, min(len(vals) - 1, int(round(p / 100.0 * (len(vals) - 1)))))
        return vals[i]

    rps = TOTAL / wall

    print(f"\n  Wall-clock:        {wall:7.2f} s")
    print(f"  Throughput:        {rps:7.1f} req/s")
    print(f"  Successful 2xx:    {len(successes):>4d} / {TOTAL}  ({100*len(successes)/TOTAL:.1f}%)")
    print(f"  Failures:          {len(failures):>4d}")

    if successes:
        print(f"\n  Latency (success-only, ms):")
        print(f"    min  = {min(durations_success):8.1f}")
        print(f"    p50  = {pct(durations_success, 50):8.1f}")
        print(f"    p75  = {pct(durations_success, 75):8.1f}")
        print(f"    p90  = {pct(durations_success, 90):8.1f}")
        print(f"    p95  = {pct(durations_success, 95):8.1f}")
        print(f"    p99  = {pct(durations_success, 99):8.1f}")
        print(f"    max  = {max(durations_success):8.1f}")
        print(f"    mean = {statistics.fmean(durations_success):8.1f}")

    print(f"\n  Status code histogram:")
    for code, n in sorted(statuses.items(), key=lambda x: (-x[1], str(x[0]))):
        bar = "█" * int(60 * n / TOTAL)
        print(f"    {code!s:<6}  {n:>4d}  {bar}")

    if err_codes:
        print(f"\n  Failure breakdown:")
        for code, n in err_codes.most_common(10):
            print(f"    {code:<25s}  {n:>4d}")

    # Sanity: did the Dahae ranker return pieces?
    if successes:
        n_pieces_dist = Counter(r["n_pieces"] for r in successes)
        print(f"\n  recommended_pieces count distribution (success):")
        for k, v in sorted(n_pieces_dist.items()):
            print(f"    n={k:>2d}  count={v}")

    print("\n" + "=" * 70)
    # Pass criteria: ≥99% success, p95 ≤ 5000ms, no auth/connection storms
    pass_ok = (
        len(successes) >= int(TOTAL * 0.99)
        and (not durations_success or pct(durations_success, 95) <= 5000)
    )
    print(f"  RESULT: {'PASS' if pass_ok else 'FAIL'}")
    print("=" * 70)
    return 0 if pass_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
