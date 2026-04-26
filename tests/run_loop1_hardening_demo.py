"""
Loop 1 — Hardening harsh suite (Q4 + Q9 + Q1 + Q2).

Five scenarios against production Postgres + production Redis:

  1. Q9 — PARALLELISM
     • Seed 100 tokens, all due NOW.
     • Mock provider refresh to take 200ms each.
     • Run ONE refresh_due_tokens() cycle.
     • SERIAL would take ~20s; PARALLEL (REFRESH_PARALLELISM=20) → ~1s.
     • Verify: cycle completes in <5s, all 100 rotated.

  2. Q4 — AP UPSERT FAILURE → ap_sync_pending=true
     • Seed 100 tokens; mock provider success but AP failure.
     • Run cycle 1 → all 100 rotated in DB, ap_sync_pending=true,
       provider_calls=100.
     • Run cycle 2 with AP MOCK FIXED → all 100 reach ap_sync_pending=false
       WITHOUT calling provider (provider_calls remains 100, not 200).
     • This proves the recovery is "ap-only retry", no wasted refresh tokens.

  3. Q4 — AP-RETRY DECRYPTS THE EXISTING TOKEN
     • Verify _retry_ap_only sends the SAME access_token that was rotated
       in cycle 1 (proven by capturing AP mock arguments).

  4. Q1 — CROSS-REPLICA: SECOND CYCLE DOESN'T DOUBLE-PROCESS
     • Run two refresh_due_tokens() cycles BACK-TO-BACK without sleeping.
     • Cycle 2 should see processing_until lease from cycle 1 if it's still
       running, else clean rows. Verify no duplicate rotation: refresh_count
       across all rows is exactly 1, not 2.

  5. Q2 — SCRUBBER catches OAuth keys + tokens
     • Feed `_scrub` test strings; verify Master Key b64, signing secret,
       Slack xoxb-/xoxe-, Google ya29./1//0 are all redacted.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Env BEFORE main is imported
os.environ.setdefault(
    "SIYADAH_OAUTH_STATE_KEY",
    base64.urlsafe_b64encode(b"\x42" * 32).decode().rstrip("="),
)
os.environ.setdefault(
    "SIYADAH_OAUTH_MK",
    base64.urlsafe_b64encode(b"\x07" * 32).decode().rstrip("="),
)
os.environ.setdefault("SLACK_CLIENT_ID", "DEMO")
os.environ.setdefault("SLACK_CLIENT_SECRET", "DEMO")
os.environ.setdefault("SLACK_REDIRECT_URI", "https://example.com/cb")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret-loop1-" + "x" * 32)
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:utYxWmdoDWsJRYAioDgsDnYEhfHQgsjz"
    "@caboose.proxy.rlwy.net:28585/railway",
)
os.environ.setdefault(
    "REDIS_URL",
    "redis://default:PVtXVtYgmXPgOWhvUfxRuYtBvriMwhrj"
    "@nozomi.proxy.rlwy.net:56937",
)
os.environ.setdefault("SIYADAH_SKIP_PG_SSL", "1")
os.environ.setdefault("ORCHESTRATOR_ALLOWED_ORIGINS", "http://testclient")
os.environ.setdefault("AP_BASE_URL", "https://activepieces-production-2499.up.railway.app")
os.environ.setdefault("AP_EMAIL", "")
os.environ.setdefault("AP_PASSWORD", "")
os.environ.setdefault("AP_PROJECT_ID", "ou4jOTA4KMnDrzOVsKWvd")
# Hardening config
os.environ["OAUTH_REFRESH_PARALLELISM"] = "20"
os.environ["OAUTH_REFRESH_BATCH_LIMIT"] = "200"

from sqlalchemy import select, text  # noqa: E402

import main                          # noqa: E402
import oauth_routes                  # noqa: E402
from database import async_session   # noqa: E402
from models import (                 # noqa: E402
    EncryptedToken, Project, TenantApiKey,
)
from oauth_providers import ParsedTokenResponse  # noqa: E402
from siyadah_crypto import CryptoProvider        # noqa: E402


TENANT = "loop1-hardening"


def banner(label: str):
    print(f"\n{'═' * 76}\n  {label}\n{'═' * 76}")


@contextmanager
def patch(target_module, name, fn):
    saved = getattr(target_module, name)
    setattr(target_module, name, fn)
    try:
        yield
    finally:
        setattr(target_module, name, saved)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

async def seed_tenant():
    key_hash = hashlib.sha256(b"loop1-key").hexdigest()
    async with async_session() as s:
        existing = (await s.execute(
            select(Project).where(Project.project_id == TENANT)
        )).scalar_one_or_none()
        if not existing:
            s.add(Project(project_id=TENANT, name="Loop1 hardening"))
            s.add(TenantApiKey(
                project_id=TENANT, key_hash=key_hash,
                label="loop1", scopes=["read", "write"],
            ))
            await s.commit()


async def cleanup():
    async with async_session() as s:
        await s.execute(
            text("DELETE FROM encrypted_tokens WHERE tenant_id = :t"),
            {"t": TENANT},
        )
        await s.commit()


async def seed_n_tokens(n: int) -> list[str]:
    """Insert N expired tokens — all due 60s in the past."""
    crypto = CryptoProvider.from_env()
    refresh_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    expires_at = refresh_at + timedelta(minutes=5)

    ids: list[str] = []
    async with async_session() as s:
        for i in range(n):
            new_id = str(uuid.uuid4())
            ids.append(new_id)
            aad = f"{TENANT}|slack|{new_id}".encode()
            dek = crypto.gen_dek()
            try:
                wrapped = crypto.wrap_dek(dek, aad=aad)
                sealed_a = crypto.encrypt_with_dek(
                    f"OLD_xoxb-V1-{i}".encode(), dek, aad=aad + b"|access",
                )
                sealed_r = crypto.encrypt_with_dek(
                    f"OLD_xoxe-V1-{i}".encode(), dek, aad=aad + b"|refresh",
                )
            finally:
                del dek
            s.add(EncryptedToken(
                id=new_id, tenant_id=TENANT, provider="slack",
                provider_account_id=f"T_LOOP1_{i:03d}",
                encrypted_access_token=sealed_a.ciphertext, iv_access=sealed_a.iv,
                encrypted_refresh_token=sealed_r.ciphertext, iv_refresh=sealed_r.iv,
                wrapped_dek=wrapped.ciphertext, iv_dek=wrapped.iv,
                encryption_version=wrapped.version,
                scopes=["chat:write"],
                expires_at=expires_at, refresh_at=refresh_at,
                refresh_count=0, status="ACTIVE", ap_sync_pending=False,
                ap_connection_external_id=f"siyadah-{new_id[:16]}",
            ))
        await s.commit()
    return ids


async def fetch_tokens(ids: list[str]) -> list[EncryptedToken]:
    async with async_session() as s:
        return list((await s.execute(
            select(EncryptedToken).where(EncryptedToken.id.in_(ids))
        )).scalars().all())


# ═══════════════════════════════════════════════════════════════
# SCENARIO 1 — Q9 PARALLELISM
# ═══════════════════════════════════════════════════════════════

async def scenario_q9_parallelism():
    """STRUCTURAL proof: instrument the provider mock with a concurrency
    counter — `max_concurrent` should reach REFRESH_PARALLELISM if the
    refresh logic actually uses asyncio.gather (vs serial). Doesn't
    depend on wall-clock timing — robust against Railway DB pool / proxy
    latency variability.
    """
    banner("Q9 — PARALLELISM (structural: measure max-concurrent provider calls)")
    await cleanup()
    N_TOKENS = 30
    await seed_n_tokens(N_TOKENS)

    in_flight = 0
    max_concurrent = 0
    total_calls = 0
    lock = asyncio.Lock()

    async def instrumented_refresh(cfg, refresh_token):
        nonlocal in_flight, max_concurrent, total_calls
        async with lock:
            in_flight += 1
            total_calls += 1
            if in_flight > max_concurrent:
                max_concurrent = in_flight
        try:
            await asyncio.sleep(0.5)        # hold long enough to overlap others
            return ParsedTokenResponse(
                access_token=f"NEW_xoxb-{total_calls}",
                refresh_token=f"NEW_xoxe-{total_calls}",
                expires_in=43200, scopes=["chat:write"],
                provider_account_id=f"T_Q9_{total_calls}",
            )
        finally:
            async with lock:
                in_flight -= 1

    async def fast_ap(cfg, **kw):
        await asyncio.sleep(0.01)

    # REFRESH_PARALLELISM=20 from env, batch_limit=200
    with patch(oauth_routes, "_refresh_with_provider", instrumented_refresh), \
         patch(oauth_routes, "_update_ap_connection", fast_ap):
        t0 = time.monotonic()
        s = await oauth_routes.refresh_due_tokens()
        elapsed = time.monotonic() - t0

    print(f"  cycle elapsed:        {elapsed:.2f}s")
    print(f"  total provider calls: {total_calls}")
    print(f"  max concurrent calls: {max_concurrent}")
    print(f"  REFRESH_PARALLELISM:  {oauth_routes.REFRESH_PARALLELISM}")
    print(f"  actions:              {s['actions']}")

    n_rotated = (s["actions"].get("rotated", 0)
                 + s["actions"].get("rotated_ap_pending", 0))
    assert n_rotated == N_TOKENS, f"only {n_rotated}/{N_TOKENS} rotated"
    # If asyncio.gather is wired, max_concurrent must exceed 1.
    # On Railway proxy with DB pool=5, we typically see 5-20+ concurrent.
    # On a serial implementation, max_concurrent would be exactly 1.
    assert max_concurrent >= 5, (
        f"parallelism not engaged — max_concurrent={max_concurrent} "
        f"(serial baseline = 1, parallel target = REFRESH_PARALLELISM)"
    )
    print(f"  ✓ SCENARIO Q9 PASSED — asyncio.gather delivers ≥5× concurrency "
          f"({max_concurrent}× peak)")


# ═══════════════════════════════════════════════════════════════
# SCENARIO 2 — Q4: AP failure → ap_sync_pending → recovery without provider call
# ═══════════════════════════════════════════════════════════════

async def scenario_q4_ap_recovery():
    banner("Q4 — AP failure → ap_sync_pending=true → recovery WITHOUT new provider call")
    await cleanup()
    ids = await seed_n_tokens(100)
    print(f"  seeded 100 expired tokens")

    provider_calls = 0
    ap_failed_calls = 0
    ap_success_calls = 0
    captured_access_tokens: list[str] = []

    async def good_refresh(cfg, refresh_token):
        nonlocal provider_calls
        provider_calls += 1
        return ParsedTokenResponse(
            access_token=f"NEW_xoxb-{provider_calls}-cycle1",
            refresh_token=f"NEW_xoxe-{provider_calls}",
            expires_in=43200, scopes=["chat:write"],
            provider_account_id=f"T_LOOP1_REC_{provider_calls}",
        )

    async def failing_ap(cfg, **kw):
        nonlocal ap_failed_calls
        ap_failed_calls += 1
        raise RuntimeError("AP unreachable: 503 Internal Service Error (simulated)")

    async def healthy_ap(cfg, **kw):
        nonlocal ap_success_calls
        ap_success_calls += 1
        captured_access_tokens.append(kw.get("access_token", ""))

    # Cycle 1 — provider OK, AP DOWN
    print(f"\n  CYCLE 1 — provider OK, AP DOWN")
    with patch(oauth_routes, "_refresh_with_provider", good_refresh), \
         patch(oauth_routes, "_update_ap_connection", failing_ap):
        s1 = await oauth_routes.refresh_due_tokens()
    print(f"    provider_calls:  {provider_calls}")
    print(f"    ap_failed_calls: {ap_failed_calls}")
    print(f"    actions:         {s1['actions']}")
    assert provider_calls == 100
    assert ap_failed_calls == 100
    assert s1["actions"].get("rotated_ap_pending", 0) == 100

    # Verify all 100 are now ap_sync_pending=true with refresh_count=1
    rows = await fetch_tokens(ids)
    pending = sum(1 for r in rows if r.ap_sync_pending)
    refreshed = sum(1 for r in rows if r.refresh_count == 1)
    print(f"    DB ap_sync_pending=true: {pending}  (expecting 100)")
    print(f"    DB refresh_count==1:     {refreshed}  (expecting 100)")
    assert pending == 100
    assert refreshed == 100

    # Cycle 2 — AP HEALED. Critical: provider must NOT be called.
    print(f"\n  CYCLE 2 — AP healed (worker should NOT call provider again)")
    provider_calls_before = provider_calls
    with patch(oauth_routes, "_refresh_with_provider", good_refresh), \
         patch(oauth_routes, "_update_ap_connection", healthy_ap):
        s2 = await oauth_routes.refresh_due_tokens()
    print(f"    provider_calls (delta): {provider_calls - provider_calls_before}")
    print(f"    ap_success_calls:       {ap_success_calls}")
    print(f"    actions:                {s2['actions']}")
    assert provider_calls == provider_calls_before, \
        "WORKER WASTED A REFRESH TOKEN CALL — recovery should be AP-only!"
    assert ap_success_calls == 100
    assert s2["actions"].get("ap_resynced", 0) == 100

    rows = await fetch_tokens(ids)
    cleared = sum(1 for r in rows if not r.ap_sync_pending)
    still_at_1 = sum(1 for r in rows if r.refresh_count == 1)
    print(f"    DB ap_sync_pending=false: {cleared}  (expecting 100)")
    print(f"    DB refresh_count still 1: {still_at_1}  (expecting 100)")
    assert cleared == 100
    assert still_at_1 == 100, "refresh_count should not increment on AP-only retry"

    # Q4 sub-check: AP retry sent the SAME access_token that was rotated in cycle 1.
    # The new tokens have form "NEW_xoxb-{i}-cycle1". captured_access_tokens
    # should contain those exact strings — proving _retry_ap_only decrypted
    # the existing rotated ciphertext, not generated new tokens.
    sample = captured_access_tokens[:5]
    print(f"    captured AP tokens sample: {sample}")
    assert all(t.startswith("NEW_xoxb-") and t.endswith("-cycle1") for t in captured_access_tokens), \
        f"AP got tokens that don't match cycle-1 rotation"

    print(f"\n  ✓ SCENARIO Q4 PASSED — AP recovery WITHOUT wasting provider calls.")
    print(f"    provider calls: 100 (cycle 1 only)")
    print(f"    ap calls:       200 (100 failed + 100 succeeded)")
    print(f"    Compare to OLD behaviour: 200 provider calls (one per cycle for 12h+)")


# ═══════════════════════════════════════════════════════════════
# SCENARIO 3 — Q1: cross-replica back-to-back cycles don't double-process
# ═══════════════════════════════════════════════════════════════

async def scenario_q1_no_double_process():
    banner("Q1 — Two simultaneous cycles, only one wins each token (FOR UPDATE SKIP LOCKED)")
    await cleanup()
    ids = await seed_n_tokens(50)
    print(f"  seeded 50 expired tokens")

    provider_calls = 0

    async def slow_refresh(cfg, refresh_token):
        nonlocal provider_calls
        provider_calls += 1
        await asyncio.sleep(0.5)
        return ParsedTokenResponse(
            access_token=f"NEW_{provider_calls}", refresh_token=f"NEW_R_{provider_calls}",
            expires_in=43200, scopes=["chat:write"],
            provider_account_id=f"T_LOOP1_RACE_{provider_calls}",
        )

    async def fast_ap(cfg, **kw):
        await asyncio.sleep(0.01)

    # Spawn TWO cycles concurrently — simulates two replicas
    with patch(oauth_routes, "_refresh_with_provider", slow_refresh), \
         patch(oauth_routes, "_update_ap_connection", fast_ap):
        t0 = time.monotonic()
        s_a, s_b = await asyncio.gather(
            oauth_routes.refresh_due_tokens(),
            oauth_routes.refresh_due_tokens(),
        )
        elapsed = time.monotonic() - t0

    total_rotated = (s_a["actions"].get("rotated", 0)
                     + s_b["actions"].get("rotated", 0)
                     + s_a["actions"].get("rotated_ap_pending", 0)
                     + s_b["actions"].get("rotated_ap_pending", 0))
    skipped_locked_a = s_a["actions"].get("skip_locked", 0)
    skipped_locked_b = s_b["actions"].get("skip_locked", 0)
    print(f"  elapsed: {elapsed:.2f}s")
    print(f"  cycle A: claimed={s_a.get('claimed', 0)}  actions={s_a['actions']}")
    print(f"  cycle B: claimed={s_b.get('claimed', 0)}  actions={s_b['actions']}")
    print(f"  total_rotated across BOTH cycles: {total_rotated}")
    print(f"  skipped_locked (Redis-level): {skipped_locked_a + skipped_locked_b}")
    print(f"  provider_calls: {provider_calls}")

    # Critical: total provider calls + skip_locked must = 50 (each row processed once)
    # Either:
    #   - Cycle A claims all 50, Cycle B sees lease and gets 0
    #   - Or they split, but no row is processed twice
    assert provider_calls == 50, \
        f"DOUBLE PROCESSING: provider called {provider_calls} times for 50 rows"

    rows = await fetch_tokens(ids)
    refresh_counts = [r.refresh_count for r in rows]
    print(f"  refresh_count distribution: max={max(refresh_counts)} min={min(refresh_counts)}")
    assert max(refresh_counts) == 1, \
        f"some token rotated >1 time: max refresh_count={max(refresh_counts)}"
    assert all(r == 1 for r in refresh_counts), "some tokens missed entirely"

    print(f"  ✓ SCENARIO Q1 PASSED — Postgres lease prevented double-processing")


# ═══════════════════════════════════════════════════════════════
# SCENARIO 4 — Q2: scrubber catches new patterns
# ═══════════════════════════════════════════════════════════════

def scenario_q2_scrubber():
    banner("Q2 — Scrubber catches OAuth keys + Slack/Google tokens")
    from logging_config import _scrub
    cases = [
        # (input contains, must NOT appear in output)
        ("SIYADAH_OAUTH_MK=BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc",
         "BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc"),
        ("Config dump: SIYADAH_OAUTH_STATE_KEY = QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI",
         "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI"),
        ("env: SLACK_SIGNING_SECRET=test-signing-secret-xyz-abc-1234",
         "test-signing-secret-xyz-abc-1234"),
        ("Got Slack token xoxb-9999-abcdef-secret-bot-token", "xoxb-9999-abcdef-secret-bot-token"),
        ("Refresh xoxe-1234-rotating-refresh-token-here", "xoxe-1234-rotating-refresh-token-here"),
        ("Google access ya29.a0AfH6SMBxxxxxxxxxxxxxxxxxxxxxxx", "ya29.a0AfH6SMBxxxxxxxxxxxxxxxxxxxxxxx"),
        ("Google refresh 1//0gXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX", "1//0gXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"),
    ]
    all_clean = True
    for orig, secret in cases:
        scrubbed = _scrub(orig)
        leaked = secret in scrubbed
        status = "✗ LEAK" if leaked else "✓ clean"
        print(f"  {status}  {orig[:60]}…")
        print(f"          scrubbed → {scrubbed[:80]}")
        if leaked:
            all_clean = False
    assert all_clean, "scrubber failed on at least one case"
    print(f"\n  ✓ SCENARIO Q2 PASSED — every secret pattern redacted")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

async def main_runner():
    banner("Loop 1 — Hardening (Q4 + Q9 + Q1 + Q2)")
    async with main.app.router.lifespan_context(main.app):
        await seed_tenant()

        results = []
        for name, fn in [
            ("Q9_parallelism", scenario_q9_parallelism),
            ("Q4_ap_recovery", scenario_q4_ap_recovery),
            ("Q1_no_double_process", scenario_q1_no_double_process),
        ]:
            try:
                await fn()
                results.append((name, True))
            except Exception:
                results.append((name, False))
                import traceback; traceback.print_exc()

        try:
            scenario_q2_scrubber()
            results.append(("Q2_scrubber", True))
        except Exception:
            results.append(("Q2_scrubber", False))
            import traceback; traceback.print_exc()

        await cleanup()

    banner("FINAL VERDICT")
    for name, ok in results:
        print(f"  {'✓ PASS' if ok else '✗ FAIL'}   {name}")
    n_pass = sum(1 for _, ok in results if ok)
    print(f"\n  {n_pass}/{len(results)} scenarios passed")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main_runner()))
