"""
Phase 4.5 harsh test runner — Slack revocation webhook.

Six scenarios against production Postgres + production Redis:

  1. URL VERIFICATION HANDSHAKE
     • Slack sends type='url_verification' with `challenge`. We respond
       with the challenge echo. Required for Slack to enable the URL.

  2. VALID SIGNATURE + app_uninstalled → REVOKED
     • Seed a Slack token; send signed event_callback / app_uninstalled.
     • Verify: encrypted_tokens.status='REVOKED', oauth.revoked audit row
       with event_meta.slack_event_id stamped, status code 200.

  3. BAD SIGNATURE → 401, NO MUTATION
     • Send a payload with a tampered signature.
     • Verify: HTTP 401, encrypted_tokens.status still ACTIVE.

  4. STALE TIMESTAMP → 401, NO MUTATION
     • Send a properly-signed payload with timestamp 10 min in the past.
     • Verify: HTTP 401 'stale timestamp', no revocation.

  5. REPLAY (same event_id twice) → IDEMPOTENT
     • First call: 200 + revoke 1 token.
     • Second call (identical body+signature+timestamp): 200 with
       duplicate=True, NO additional audit row written.

  6. CROSS-TENANT (same team_id, two tenants)
     • Two tenants both have an ACTIVE token for team_id=T_SHARED.
     • Single webhook revokes BOTH tokens.
     • Two oauth.revoked audit rows (one per tenant).

  7. MISSING SIGNING SECRET → 503
     • Briefly unset SLACK_SIGNING_SECRET; verify the route refuses
       safely (caller's request unaffected — no false negatives).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
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
os.environ["SLACK_SIGNING_SECRET"] = "test-signing-secret-phase45-" + "x" * 32
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

import httpx                         # noqa: E402
from sqlalchemy import select, text  # noqa: E402

import main                           # noqa: E402
from database import async_session    # noqa: E402
from models import (                  # noqa: E402
    EncryptedToken, Project, TenantApiKey, TenantAuditLog,
)
from siyadah_crypto import CryptoProvider  # noqa: E402


SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
TENANT_A = "phase45-tenant-A"
TENANT_B = "phase45-tenant-B"


def banner(label: str):
    print(f"\n{'═' * 76}\n  {label}\n{'═' * 76}")


@contextmanager
def patch_clock(target_module, fixed_now: int):
    saved = target_module._now_seconds
    target_module._now_seconds = lambda: fixed_now
    try:
        yield
    finally:
        target_module._now_seconds = saved


# ═══════════════════════════════════════════════════════════════
# Helpers — sign + send
# ═══════════════════════════════════════════════════════════════

def slack_sign(body: bytes, timestamp: int, secret: str = SIGNING_SECRET) -> str:
    """Compute the v0=… signature exactly as Slack does."""
    base = b"v0:" + str(timestamp).encode() + b":" + body
    return "v0=" + hmac.new(
        secret.encode(), base, hashlib.sha256,
    ).hexdigest()


async def post_signed(
    client: httpx.AsyncClient, payload: dict,
    *, timestamp: int | None = None,
    sig_override: str | None = None,
) -> httpx.Response:
    """Send a signed POST to /v2/webhooks/slack/events. timestamp defaults
    to 'now'. sig_override lets us simulate tamper."""
    if timestamp is None:
        timestamp = int(time.time())
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = sig_override or slack_sign(body, timestamp)
    return await client.post(
        "/v2/webhooks/slack/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Signature": sig,
            "X-Slack-Request-Timestamp": str(timestamp),
        },
    )


# ═══════════════════════════════════════════════════════════════
# Tenant + token seeding
# ═══════════════════════════════════════════════════════════════

async def seed_tenants():
    """Create both demo tenants idempotently."""
    async with async_session() as s:
        for pid, name in [(TENANT_A, "Phase 4.5 Tenant A"),
                           (TENANT_B, "Phase 4.5 Tenant B")]:
            existing = (await s.execute(
                select(Project).where(Project.project_id == pid)
            )).scalar_one_or_none()
            if not existing:
                s.add(Project(project_id=pid, name=name))
                s.add(TenantApiKey(
                    project_id=pid,
                    key_hash=hashlib.sha256(f"key-{pid}".encode()).hexdigest(),
                    label="phase-4.5", scopes=["read", "write"],
                ))
        await s.commit()


async def cleanup_phase45():
    async with async_session() as s:
        await s.execute(
            text("DELETE FROM encrypted_tokens "
                 "WHERE tenant_id IN (:a, :b)"),
            {"a": TENANT_A, "b": TENANT_B},
        )
        await s.commit()


async def seed_active_token(tenant_id: str, team_id: str) -> str:
    """Insert a fresh ACTIVE Slack token bound to the given team_id."""
    crypto = CryptoProvider.from_env()
    new_id = str(uuid.uuid4())
    aad = f"{tenant_id}|slack|{new_id}".encode()
    dek = crypto.gen_dek()
    try:
        wrapped = crypto.wrap_dek(dek, aad=aad)
        sealed_a = crypto.encrypt_with_dek(b"xoxb-DEMO", dek, aad=aad + b"|access")
    finally:
        del dek

    async with async_session() as s:
        s.add(EncryptedToken(
            id=new_id, tenant_id=tenant_id, provider="slack",
            provider_account_id=team_id,
            encrypted_access_token=sealed_a.ciphertext, iv_access=sealed_a.iv,
            wrapped_dek=wrapped.ciphertext, iv_dek=wrapped.iv,
            encryption_version=wrapped.version,
            scopes=["chat:write"],
            status="ACTIVE",
        ))
        await s.commit()
    return new_id


async def get_token_status(token_id: str) -> str | None:
    async with async_session() as s:
        return (await s.execute(
            select(EncryptedToken.status)
            .where(EncryptedToken.id == token_id)
        )).scalar_one_or_none()


async def count_revoke_audit(saga_or_token_id: str) -> int:
    async with async_session() as s:
        return (await s.execute(
            select(TenantAuditLog).where(
                TenantAuditLog.event_type == "oauth.revoked",
                TenantAuditLog.event_meta.op("@>")(
                    {"encrypted_token_id": saga_or_token_id}
                ),
            )
        )).all().__len__()


# ═══════════════════════════════════════════════════════════════
# SCENARIOS
# ═══════════════════════════════════════════════════════════════

async def scenario_1_url_verification(client: httpx.AsyncClient):
    banner("SCENARIO 1 — URL verification handshake")
    challenge = "abc123-CHALLENGE-secret"
    payload = {"type": "url_verification", "challenge": challenge,
               "token": "ignored"}
    r = await post_signed(client, payload)
    print(f"  HTTP {r.status_code}")
    print(f"  body: {r.json()}")
    assert r.status_code == 200
    assert r.json()["challenge"] == challenge
    print("  ✓ Slack URL verification handshake OK")


async def scenario_2_valid_uninstall(client: httpx.AsyncClient):
    banner("SCENARIO 2 — VALID signature + app_uninstalled → REVOKED")
    team = "T_PHASE45_S2"
    token_id = await seed_active_token(TENANT_A, team)
    status_before = await get_token_status(token_id)
    print(f"  seeded token {token_id} status={status_before}")
    assert status_before == "ACTIVE"

    payload = {
        "type": "event_callback",
        "event_id": f"Ev_S2_{uuid.uuid4().hex[:8]}",
        "team_id": team,
        "event": {"type": "app_uninstalled"},
    }
    r = await post_signed(client, payload)
    print(f"  HTTP {r.status_code}  body: {r.json()}")
    assert r.status_code == 200
    body = r.json()
    assert body["event_type"] == "app_uninstalled"
    assert body["revoked_tokens"] == 1

    status_after = await get_token_status(token_id)
    print(f"  status_after = {status_after}")
    assert status_after == "REVOKED"

    # Audit row
    async with async_session() as s:
        audits = (await s.execute(
            select(TenantAuditLog).where(
                TenantAuditLog.event_type == "oauth.revoked",
                TenantAuditLog.event_meta.op("@>")(
                    {"encrypted_token_id": token_id},
                ),
            )
        )).scalars().all()
    print(f"  oauth.revoked audit rows: {len(audits)}")
    assert len(audits) == 1
    a = audits[0]
    print(f"    event_meta.revoked_via   = {a.event_meta.get('revoked_via')}")
    print(f"    event_meta.slack_event_id= {a.event_meta.get('slack_event_id')}")
    assert a.event_meta.get("revoked_via") == "webhook"
    assert a.event_meta.get("slack_event_id") == payload["event_id"]
    print("  ✓ token REVOKED + audit oauth.revoked with full forensics")


async def scenario_3_bad_signature(client: httpx.AsyncClient):
    banner("SCENARIO 3 — TAMPERED signature → 401, no DB mutation")
    team = "T_PHASE45_S3"
    token_id = await seed_active_token(TENANT_A, team)

    payload = {
        "type": "event_callback",
        "event_id": f"Ev_S3_{uuid.uuid4().hex[:8]}",
        "team_id": team,
        "event": {"type": "app_uninstalled"},
    }
    # Override signature — flip a hex char
    body = json.dumps(payload, separators=(",", ":")).encode()
    real = slack_sign(body, int(time.time()))
    bad = real[:-2] + ("ff" if real[-2:] != "ff" else "00")
    r = await post_signed(client, payload, sig_override=bad)
    print(f"  HTTP {r.status_code}  detail: {r.text[:200]}")
    assert r.status_code == 401
    assert "signature" in r.text.lower()

    status_after = await get_token_status(token_id)
    print(f"  status_after = {status_after}  (must remain ACTIVE)")
    assert status_after == "ACTIVE"
    print("  ✓ tampered signature → 401, no DB mutation")


async def scenario_4_stale_timestamp(client: httpx.AsyncClient):
    banner("SCENARIO 4 — STALE timestamp → 401")
    team = "T_PHASE45_S4"
    token_id = await seed_active_token(TENANT_A, team)

    stale_ts = int(time.time()) - 600     # 10 min in the past
    payload = {
        "type": "event_callback",
        "event_id": f"Ev_S4_{uuid.uuid4().hex[:8]}",
        "team_id": team,
        "event": {"type": "app_uninstalled"},
    }
    r = await post_signed(client, payload, timestamp=stale_ts)
    print(f"  HTTP {r.status_code}  detail: {r.text[:200]}")
    assert r.status_code == 401
    assert "stale" in r.text.lower() or "timestamp" in r.text.lower()

    status_after = await get_token_status(token_id)
    assert status_after == "ACTIVE"
    print("  ✓ stale timestamp → 401, replay defended")


async def scenario_5_replay_idempotent(client: httpx.AsyncClient):
    banner("SCENARIO 5 — REPLAY same event_id → idempotent (no double revoke)")
    team = "T_PHASE45_S5"
    token_id = await seed_active_token(TENANT_A, team)

    payload = {
        "type": "event_callback",
        "event_id": "Ev_REPLAY_FIXED_001",
        "team_id": team,
        "event": {"type": "app_uninstalled"},
    }
    # Pin timestamp so both calls have IDENTICAL body+ts → identical sig
    ts = int(time.time())

    r1 = await post_signed(client, payload, timestamp=ts)
    print(f"  first call:  HTTP {r1.status_code}  body: {r1.json()}")
    assert r1.status_code == 200
    assert r1.json().get("revoked_tokens") == 1

    r2 = await post_signed(client, payload, timestamp=ts)
    print(f"  second call: HTTP {r2.status_code}  body: {r2.json()}")
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True

    # Verify only ONE audit row was written
    async with async_session() as s:
        audits = (await s.execute(
            select(TenantAuditLog).where(
                TenantAuditLog.event_type == "oauth.revoked",
                TenantAuditLog.event_meta.op("@>")(
                    {"slack_event_id": payload["event_id"]},
                ),
            )
        )).scalars().all()
    print(f"  oauth.revoked rows for event_id: {len(audits)}  (must be 1)")
    assert len(audits) == 1
    print("  ✓ replay idempotent — single revocation, single audit")


async def scenario_6_cross_tenant(client: httpx.AsyncClient):
    banner("SCENARIO 6 — same team_id across 2 tenants → both REVOKED")
    team = "T_PHASE45_SHARED"
    tok_A = await seed_active_token(TENANT_A, team)
    tok_B = await seed_active_token(TENANT_B, team)
    print(f"  seeded tokens: A={tok_A}  B={tok_B}")

    payload = {
        "type": "event_callback",
        "event_id": f"Ev_S6_{uuid.uuid4().hex[:8]}",
        "team_id": team,
        "event": {"type": "app_uninstalled"},
    }
    r = await post_signed(client, payload)
    print(f"  HTTP {r.status_code}  body: {r.json()}")
    assert r.status_code == 200
    assert r.json()["revoked_tokens"] == 2

    sa = await get_token_status(tok_A)
    sb = await get_token_status(tok_B)
    print(f"  tenant A token: {sa}    tenant B token: {sb}")
    assert sa == "REVOKED" and sb == "REVOKED"

    # 2 audit rows — one per tenant
    async with async_session() as s:
        audits = (await s.execute(
            select(TenantAuditLog).where(
                TenantAuditLog.event_type == "oauth.revoked",
                TenantAuditLog.event_meta.op("@>")(
                    {"slack_event_id": payload["event_id"]},
                ),
            )
        )).scalars().all()
    tenants_audited = {a.project_id for a in audits}
    print(f"  audit rows: {len(audits)}  tenants: {tenants_audited}")
    assert len(audits) == 2
    assert tenants_audited == {TENANT_A, TENANT_B}
    print("  ✓ cross-tenant revocation: every affected tenant audited")


async def scenario_7_no_signing_secret(client: httpx.AsyncClient):
    banner("SCENARIO 7 — SLACK_SIGNING_SECRET unset → 503 (config error)")
    saved = os.environ.pop("SLACK_SIGNING_SECRET", None)
    try:
        payload = {"type": "url_verification", "challenge": "x"}
        body = json.dumps(payload).encode()
        ts = int(time.time())
        # Even a "valid" sig (made with the OLD secret) gets rejected
        # because the route can't load the secret to verify against.
        sig = slack_sign(body, ts, secret=saved or "anything")
        r = await client.post(
            "/v2/webhooks/slack/events",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Signature": sig,
                "X-Slack-Request-Timestamp": str(ts),
            },
        )
        print(f"  HTTP {r.status_code}  detail: {r.text[:200]}")
        assert r.status_code == 503
        assert "SIGNING_SECRET" in r.text or "not configured" in r.text
        print("  ✓ no signing secret → 503; no false negatives, no false positives")
    finally:
        if saved is not None:
            os.environ["SLACK_SIGNING_SECRET"] = saved


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

async def main_runner():
    banner("Phase 4.5 — Slack Revocation Webhook (harsh suite)")
    async with main.app.router.lifespan_context(main.app):
        await seed_tenants()
        await cleanup_phase45()
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testclient", timeout=30,
        ) as c:
            results = []
            for name, fn in [
                ("1_url_verification", scenario_1_url_verification),
                ("2_valid_uninstall_revoked", scenario_2_valid_uninstall),
                ("3_bad_signature_401", scenario_3_bad_signature),
                ("4_stale_timestamp_401", scenario_4_stale_timestamp),
                ("5_replay_idempotent", scenario_5_replay_idempotent),
                ("6_cross_tenant_revocation", scenario_6_cross_tenant),
                ("7_no_signing_secret_503", scenario_7_no_signing_secret),
            ]:
                try:
                    await fn(c)
                    results.append((name, True))
                except Exception:
                    results.append((name, False))
                    import traceback; traceback.print_exc()

        await cleanup_phase45()

    banner("FINAL VERDICT")
    for name, ok in results:
        print(f"  {'✓ PASS' if ok else '✗ FAIL'}   {name}")
    n_pass = sum(1 for _, ok in results if ok)
    print(f"\n  {n_pass}/{len(results)} scenarios passed")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main_runner()))
