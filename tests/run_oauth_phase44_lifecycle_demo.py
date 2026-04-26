"""
Phase 4.4 follow-on — verify the refresh worker is mounted in lifespan
and rotates tokens autonomously when SIYADAH_REFRESH_WORKER_ENABLED=true.

Test logic:
  1. Set SIYADAH_REFRESH_WORKER_ENABLED=true and a tight 3-second interval.
  2. Patch _refresh_with_provider + _update_ap_connection so we don't
     touch real Slack / real AP.
  3. Enter the FastAPI lifespan context (which spawns the worker task).
  4. Seed an expired token.
  5. Wait one cycle.
  6. Verify the seeded token was rotated AUTONOMOUSLY (we never called
     refresh_due_tokens() directly — the worker did it).
  7. Exit lifespan; verify the task was cancelled cleanly.

Disabled-flag scenario:
  • Re-enter lifespan with the flag OFF.
  • Verify no rotation happens within the same window.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Env BEFORE main is imported
os.environ["SIYADAH_OAUTH_STATE_KEY"] = base64.urlsafe_b64encode(b"\x42" * 32).decode().rstrip("=")
os.environ["SIYADAH_OAUTH_MK"] = base64.urlsafe_b64encode(b"\x07" * 32).decode().rstrip("=")
os.environ["SLACK_CLIENT_ID"] = "DEMO"
os.environ["SLACK_CLIENT_SECRET"] = "DEMO"
os.environ["SLACK_REDIRECT_URI"] = "https://example.com/cb"
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
# CRITICAL — short interval so the test doesn't hang
os.environ["OAUTH_REFRESH_INTERVAL_SECONDS"] = "3"

from sqlalchemy import select, text  # noqa: E402

from oauth_providers import ParsedTokenResponse  # noqa: E402


TENANT = "phase44-lifecycle"
RAW_KEY = "phase44-lc-key-" + "z" * 40


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


async def seed_tenant():
    from database import async_session
    from models import Project, TenantApiKey
    key_hash = hashlib.sha256(RAW_KEY.encode()).hexdigest()
    async with async_session() as s:
        existing = (await s.execute(
            select(Project).where(Project.project_id == TENANT)
        )).scalar_one_or_none()
        if not existing:
            s.add(Project(project_id=TENANT, name="Phase 4.4 lifecycle"))
            s.add(TenantApiKey(
                project_id=TENANT, key_hash=key_hash,
                label="phase-4.4-lc", scopes=["read", "write"],
            ))
            await s.commit()


async def cleanup():
    from database import async_session
    async with async_session() as s:
        await s.execute(
            text("DELETE FROM encrypted_tokens WHERE tenant_id = :t"),
            {"t": TENANT},
        )
        await s.commit()


async def seed_expired_token() -> str:
    """Same shape as Phase 4.4 demo — pre-generated id for AAD binding."""
    import uuid
    from database import async_session
    from models import EncryptedToken
    from siyadah_crypto import CryptoProvider

    crypto = CryptoProvider.from_env()
    new_id = str(uuid.uuid4())
    aad = f"{TENANT}|slack|{new_id}".encode()
    dek = crypto.gen_dek()
    try:
        wrapped = crypto.wrap_dek(dek, aad=aad)
        sealed_a = crypto.encrypt_with_dek(b"OLD_LIFECYCLE_xoxb-V1", dek, aad=aad + b"|access")
        sealed_r = crypto.encrypt_with_dek(b"OLD_LIFECYCLE_xoxe-V1", dek, aad=aad + b"|refresh")
    finally:
        del dek

    refresh_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    expires_at = refresh_at + timedelta(minutes=5)
    async with async_session() as s:
        s.add(EncryptedToken(
            id=new_id, tenant_id=TENANT, provider="slack",
            provider_account_id="T_LC",
            encrypted_access_token=sealed_a.ciphertext, iv_access=sealed_a.iv,
            encrypted_refresh_token=sealed_r.ciphertext, iv_refresh=sealed_r.iv,
            wrapped_dek=wrapped.ciphertext, iv_dek=wrapped.iv,
            encryption_version=wrapped.version,
            scopes=["chat:write"],
            expires_at=expires_at, refresh_at=refresh_at,
            refresh_count=0, status="ACTIVE",
            ap_connection_external_id=f"siyadah-{new_id[:16]}",
        ))
        await s.commit()
    return new_id


async def get_refresh_count(token_id: str) -> int:
    from database import async_session
    from models import EncryptedToken
    async with async_session() as s:
        c = (await s.execute(
            select(EncryptedToken.refresh_count).where(EncryptedToken.id == token_id)
        )).scalar_one_or_none()
    return c if c is not None else -1


async def has_active_task(name: str) -> bool:
    """Inspect asyncio task list for the worker."""
    return any(t.get_name() == name and not t.done() for t in asyncio.all_tasks())


# ═══════════════════════════════════════════════════════════════
# SCENARIO A — flag ON, expect autonomous rotation
# ═══════════════════════════════════════════════════════════════

async def scenario_a_flag_on():
    banner("SCENARIO A — SIYADAH_REFRESH_WORKER_ENABLED=true (autonomous rotation)")
    os.environ["SIYADAH_REFRESH_WORKER_ENABLED"] = "true"

    if "main" in sys.modules:
        del sys.modules["main"]
    if "oauth_routes" in sys.modules:
        del sys.modules["oauth_routes"]
    import main           # noqa: E402
    import oauth_routes   # noqa: E402

    async def fake_refresh(cfg, refresh_token):
        return ParsedTokenResponse(
            access_token="LC_NEW_xoxb-AUTONOMOUS",
            refresh_token="LC_NEW_xoxe-AUTONOMOUS",
            expires_in=43200,
            scopes=["chat:write"],
            provider_account_id="T_LC",
        )

    ap_called = []
    async def fake_ap(cfg, **kw):
        ap_called.append(kw.get("access_token"))

    with patch(oauth_routes, "_refresh_with_provider", fake_refresh), \
         patch(oauth_routes, "_update_ap_connection", fake_ap):
        async with main.app.router.lifespan_context(main.app):
            await seed_tenant()
            await cleanup()
            token_id = await seed_expired_token()
            print(f"  seeded expired token {token_id}")

            alive = await has_active_task("oauth-refresh-worker")
            print(f"  worker task alive: {alive}")
            assert alive, "worker not in asyncio.all_tasks()"

            # Wait until rotation happens — bounded poll, up to 4 cycles
            # (interval=3s; each cycle does DB+Redis I/O so allow 30s ceiling).
            print("  polling up to 30s for autonomous rotation …")
            count = 0
            for i in range(30):
                await asyncio.sleep(1)
                count = await get_refresh_count(token_id)
                if count >= 1:
                    print(f"    rotated after {i+1}s")
                    break

            print(f"  refresh_count after wait: {count}")
            assert count == 1, f"worker did not autonomously rotate (count={count})"
            assert len(ap_called) >= 1
            print(f"  ✓ AP upsert was called autonomously: {len(ap_called)} time(s)")

        alive_after = await has_active_task("oauth-refresh-worker")
        print(f"  worker task alive AFTER shutdown: {alive_after}")
        assert not alive_after, "worker task survived shutdown"
        print("  ✓ SCENARIO A PASSED — autonomous rotation + clean shutdown")


# ═══════════════════════════════════════════════════════════════
# SCENARIO B — flag OFF, no autonomous rotation
# ═══════════════════════════════════════════════════════════════

async def scenario_b_flag_off():
    """Spawn a fresh subprocess (clean asyncio loop) with the flag off,
    via a tempfile script (avoids -c newline pitfalls)."""
    banner("SCENARIO B — SIYADAH_REFRESH_WORKER_ENABLED=false (no worker spawned)")
    import tempfile

    script = '''\
import os
os.environ["SIYADAH_REFRESH_WORKER_ENABLED"] = "false"
import asyncio, sys
sys.path.insert(0, ".")
import main

async def m():
    async with main.app.router.lifespan_context(main.app):
        names = [t.get_name() for t in asyncio.all_tasks() if not t.done()]
        worker_present = any("oauth-refresh-worker" in n for n in names)
        print("TASKS:", names)
        print("WORKER_PRESENT:", worker_present)
        assert not worker_present, "worker spawned despite flag off"

asyncio.run(m())
'''
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(script)
        script_path = f.name
    try:
        proc = await asyncio.create_subprocess_exec(
            "/tmp/siyadah_venv/bin/python", script_path,
            cwd="/Users/aboeyad/Desktop/siyadah/orchestrator",
            env={**os.environ, "SIYADAH_REFRESH_WORKER_ENABLED": "false"},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode() + stderr.decode()
        for line in out.splitlines()[-30:]:
            print(f"  | {line}")
        assert proc.returncode == 0, f"subprocess failed rc={proc.returncode}"
        assert "WORKER_PRESENT: False" in out, "worker was spawned despite flag off"
        assert "NOT enabled" in out, "expected explicit 'NOT enabled' log line"
        print("  ✓ SCENARIO B PASSED — flag off, worker NOT spawned")
    finally:
        try:
            os.unlink(script_path)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

async def main_runner():
    banner("Phase 4.4 follow-on — Lifespan-Mounted Eternal Pulse")
    results = []
    try:
        await scenario_a_flag_on()
        results.append(("A_flag_on_autonomous", True))
    except Exception:
        results.append(("A_flag_on_autonomous", False))
        import traceback; traceback.print_exc()
    try:
        await scenario_b_flag_off()
        results.append(("B_flag_off_no_rotation", True))
    except Exception:
        results.append(("B_flag_off_no_rotation", False))
        import traceback; traceback.print_exc()

    # Final cleanup so re-runs are idempotent
    try:
        if "main" not in sys.modules:
            import main  # noqa: F401
        await cleanup()
    except Exception:
        pass

    banner("FINAL VERDICT")
    for name, ok in results:
        print(f"  {'✓ PASS' if ok else '✗ FAIL'}   {name}")
    n_pass = sum(1 for _, ok in results if ok)
    print(f"\n  {n_pass}/{len(results)} scenarios passed")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main_runner()))
