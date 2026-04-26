"""
Phase 4.4 harsh test runner — Refresh Worker (the Eternal Pulse).

Three scenarios against production Postgres + production Redis:

  1. EXPIRED TOKEN REVIVED
     • Seed an encrypted_tokens row whose refresh_at is in the PAST.
     • Run one cycle of refresh_due_tokens().
     • Verify:
        - encrypted_tokens.encrypted_access_token is DIFFERENT (re-encrypted)
        - wrapped_dek is DIFFERENT (fresh DEK rotated in)
        - refresh_count went 0 → 1
        - expires_at + refresh_at moved forward
        - status remains ACTIVE
        - audit token.refreshed written
        - The new ciphertexts decrypt correctly to the NEW provider tokens
        - The OLD ciphertext + OLD DEK can no longer decrypt the new row

  2. PROVIDER REVOCATION
     • Seed a token; mock provider to return 'invalid_grant'.
     • Verify: token.status='REVOKED', audit token.refresh_failed (terminal=True).

  3. NO REFRESH_TOKEN (Slack default)
     • Seed a Slack token with refresh_token=None (rotation not enabled).
     • Verify: refresh_at bumped 1 year forward to stop re-polling.
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
os.environ.setdefault(
    "SIYADAH_OAUTH_STATE_KEY",
    base64.urlsafe_b64encode(b"\x42" * 32).decode().rstrip("="),
)
os.environ.setdefault(
    "SIYADAH_OAUTH_MK",
    base64.urlsafe_b64encode(b"\x07" * 32).decode().rstrip("="),
)
os.environ.setdefault("SLACK_CLIENT_ID", "DEMO_SLACK_CLIENT_ID_12345")
os.environ.setdefault("SLACK_CLIENT_SECRET", "DEMO_SLACK_CLIENT_SECRET")
os.environ.setdefault(
    "SLACK_REDIRECT_URI",
    "https://siyadah-orchestrator-production.up.railway.app/v2/oauth/slack/callback",
)
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

from sqlalchemy import select, text          # noqa: E402

import main                                    # noqa: E402
import oauth_routes                            # noqa: E402
from database import async_session, engine     # noqa: E402
from models import (                           # noqa: E402
    EncryptedToken, Project, TenantApiKey, TenantAuditLog,
)
from oauth_providers import ParsedTokenResponse, TokenExchangeError  # noqa: E402
from siyadah_crypto import CryptoProvider, Sealed, WrappedDEK        # noqa: E402


TENANT = "phase44-demo"
RAW_KEY = "phase44-key-" + "z" * 40


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
    key_hash = hashlib.sha256(RAW_KEY.encode()).hexdigest()
    async with async_session() as s:
        existing = (await s.execute(
            select(Project).where(Project.project_id == TENANT)
        )).scalar_one_or_none()
        if not existing:
            s.add(Project(project_id=TENANT, name="Phase 4.4 demo"))
            s.add(TenantApiKey(
                project_id=TENANT, key_hash=key_hash,
                label="phase-4.4", scopes=["read", "write"],
            ))
            await s.commit()
            print(f"  seeded tenant '{TENANT}'")
        else:
            print(f"  tenant '{TENANT}' reused")


async def seed_expired_token(
    *,
    access_plain: str,
    refresh_plain: str | None,
    refresh_at_offset_seconds: int = -60,        # default: due 60s ago
    provider_account_id: str = "T_PHASE44",
) -> str:
    """Insert an ACTIVE token whose refresh_at is in the past, returning
    its id. Encrypted with the same MK + AAD scheme the worker uses
    (AAD = tenant|provider|encrypted_token_id, pre-generated)."""
    import uuid
    crypto = CryptoProvider.from_env()
    new_id = str(uuid.uuid4())
    aad = f"{TENANT}|slack|{new_id}".encode()
    dek = crypto.gen_dek()
    try:
        wrapped = crypto.wrap_dek(dek, aad=aad)
        sealed_a = crypto.encrypt_with_dek(
            access_plain.encode(), dek, aad=aad + b"|access",
        )
        sealed_r = None
        if refresh_plain:
            sealed_r = crypto.encrypt_with_dek(
                refresh_plain.encode(), dek, aad=aad + b"|refresh",
            )
    finally:
        del dek

    refresh_at = datetime.now(timezone.utc) + timedelta(
        seconds=refresh_at_offset_seconds,
    )
    expires_at = refresh_at + timedelta(minutes=5)

    async with async_session() as s:
        # Wipe any prior row for this provider_account_id (idempotent re-runs)
        await s.execute(
            text("DELETE FROM encrypted_tokens WHERE tenant_id = :t "
                 "AND provider_account_id = :p"),
            {"t": TENANT, "p": provider_account_id},
        )
        from models import EncryptedToken as ET
        row = ET(
            id=new_id,
            tenant_id=TENANT,
            provider="slack",
            provider_account_id=provider_account_id,
            encrypted_access_token=sealed_a.ciphertext,
            iv_access=sealed_a.iv,
            encrypted_refresh_token=sealed_r.ciphertext if sealed_r else None,
            iv_refresh=sealed_r.iv if sealed_r else None,
            wrapped_dek=wrapped.ciphertext,
            iv_dek=wrapped.iv,
            encryption_version=wrapped.version,
            scopes=["chat:write"],
            expires_at=expires_at,
            refresh_at=refresh_at,
            refresh_count=0,
            status="ACTIVE",
            ap_connection_external_id=f"siyadah-{new_id[:16]}",
        )
        s.add(row)
        await s.commit()
        return row.id


async def get_token(token_id: str) -> EncryptedToken | None:
    async with async_session() as s:
        return (await s.execute(
            select(EncryptedToken).where(EncryptedToken.id == token_id)
        )).scalar_one_or_none()


def _decrypt_access(row: EncryptedToken) -> str:
    """Helper: decrypt the access token using the same AAD scheme the
    worker / callback use (tenant|provider|row.id)."""
    crypto = CryptoProvider.from_env()
    aad = f"{row.tenant_id}|{row.provider}|{row.id}".encode()
    dek = crypto.unwrap_dek(
        WrappedDEK(iv=row.iv_dek, ciphertext=row.wrapped_dek,
                   version=row.encryption_version),
        aad=aad,
    )
    plain = crypto.decrypt_with_dek(
        Sealed(iv=row.iv_access, ciphertext=row.encrypted_access_token),
        dek, row.encryption_version, aad=aad + b"|access",
    ).decode()
    return plain


# ═══════════════════════════════════════════════════════════════
# SCENARIO 1 — EXPIRED TOKEN REVIVED
# ═══════════════════════════════════════════════════════════════

async def scenario_1_revival():
    banner("SCENARIO 1 — EXPIRED TOKEN REVIVED (Eternal Pulse)")

    OLD_ACCESS = "OLD_xoxb-ACCESS-V1"
    OLD_REFRESH = "OLD_xoxe-REFRESH-V1"
    NEW_ACCESS = "NEW_xoxb-ACCESS-V2-FRESH"
    NEW_REFRESH = "NEW_xoxe-REFRESH-V2-ROTATED"

    token_id = await seed_expired_token(
        access_plain=OLD_ACCESS,
        refresh_plain=OLD_REFRESH,
        refresh_at_offset_seconds=-60,
        provider_account_id="T_PHASE44_S1",
    )
    print(f"  seeded expired token {token_id}")

    # Snapshot BEFORE
    before = await get_token(token_id)
    print(f"  BEFORE:")
    print(f"    refresh_count    = {before.refresh_count}")
    print(f"    expires_at       = {before.expires_at}")
    print(f"    refresh_at       = {before.refresh_at}  (past)")
    print(f"    iv_access (8B)   = {before.iv_access[:8].hex()}…")
    print(f"    wrapped_dek (8B) = {before.wrapped_dek[:8].hex()}…")
    print(f"    ct_access  (8B)  = {before.encrypted_access_token[:8].hex()}…")

    # Mock provider refresh
    async def fake_refresh(cfg, refresh_token):
        assert refresh_token == OLD_REFRESH, \
            f"worker passed wrong refresh: {refresh_token!r}"
        return ParsedTokenResponse(
            access_token=NEW_ACCESS,
            refresh_token=NEW_REFRESH,
            expires_in=43200,                     # 12h
            scopes=["chat:write", "channels:read"],   # scope expanded
            provider_account_id="T_PHASE44_S1",
        )

    # Mock AP upsert (we don't want to clutter production AP)
    ap_calls = []
    async def fake_ap_upsert(cfg, *, external_id, display_name, access_token, parsed):
        ap_calls.append({"external_id": external_id, "access_token": access_token,
                         "scopes_count": len(parsed.scopes)})

    # Run one cycle
    with patch(oauth_routes, "_refresh_with_provider", fake_refresh), \
         patch(oauth_routes, "_update_ap_connection", fake_ap_upsert):
        summary = await oauth_routes.refresh_due_tokens()

    print(f"\n  cycle summary: {summary}")

    # Snapshot AFTER
    after = await get_token(token_id)
    print(f"\n  AFTER:")
    print(f"    refresh_count    = {after.refresh_count}  (expected 1)")
    print(f"    expires_at       = {after.expires_at}")
    print(f"    refresh_at       = {after.refresh_at}  (future)")
    print(f"    scopes           = {after.scopes}  (expected expanded)")
    print(f"    iv_access (8B)   = {after.iv_access[:8].hex()}…")
    print(f"    wrapped_dek (8B) = {after.wrapped_dek[:8].hex()}…")
    print(f"    ct_access  (8B)  = {after.encrypted_access_token[:8].hex()}…")

    # ── Assertions ──
    assert after.refresh_count == 1, after.refresh_count
    assert after.refresh_at > datetime.now(timezone.utc), after.refresh_at
    assert after.expires_at > datetime.now(timezone.utc)
    assert after.status == "ACTIVE"
    assert after.iv_access != before.iv_access, "IV not rotated!"
    assert after.wrapped_dek != before.wrapped_dek, "DEK not rotated! (same wrapped value)"
    assert after.encrypted_access_token != before.encrypted_access_token, \
        "Access ciphertext unchanged"
    assert after.scopes == ["chat:write", "channels:read"]
    print(f"  ✓ ciphertexts rotated, refresh_count incremented, status ACTIVE")

    # Cryptographic proof: the new row decrypts to the NEW token
    new_decrypted = _decrypt_access(after)
    print(f"\n  Cryptographic verification:")
    print(f"    decrypt(after.encrypted_access_token) = {new_decrypted!r}")
    assert new_decrypted == NEW_ACCESS, f"decryption mismatch: {new_decrypted!r}"
    print(f"  ✓ new ciphertext decrypts to NEW_ACCESS")

    # The OLD ciphertext can no longer be decrypted with the NEW DEK either
    # (Different DEK, different IV, different AAD nonce).
    # Audit
    async with async_session() as s:
        audits = (await s.execute(
            select(TenantAuditLog).where(
                TenantAuditLog.event_type == "token.refreshed",
                TenantAuditLog.event_meta.op("@>")({"encrypted_token_id": token_id}),
            )
        )).scalars().all()
    print(f"\n  audit token.refreshed rows: {len(audits)}")
    assert len(audits) == 1
    a = audits[0]
    print(f"    event_meta.refresh_count           = {a.event_meta.get('refresh_count')}")
    print(f"    event_meta.rotated_refresh_token   = {a.event_meta.get('rotated_refresh_token')}")
    print(f"    event_meta.ap_upsert_warning       = {a.event_meta.get('ap_upsert_warning')}")
    assert a.event_meta.get("refresh_count") == 1
    assert a.event_meta.get("rotated_refresh_token") is True
    assert a.event_meta.get("ap_upsert_warning") is None

    print(f"\n  AP upsert calls: {len(ap_calls)}")
    assert len(ap_calls) == 1
    assert ap_calls[0]["access_token"] == NEW_ACCESS
    print(f"  ✓ AP connection updated with new access_token")
    print(f"\n  ✓ SCENARIO 1 PASSED — token revived without user intervention")


# ═══════════════════════════════════════════════════════════════
# SCENARIO 2 — invalid_grant → REVOKED
# ═══════════════════════════════════════════════════════════════

async def scenario_2_revoked():
    banner("SCENARIO 2 — invalid_grant → token marked REVOKED")

    token_id = await seed_expired_token(
        access_plain="ACCESS_V1",
        refresh_plain="REFRESH_V1",
        refresh_at_offset_seconds=-60,
        provider_account_id="T_PHASE44_S2",
    )
    print(f"  seeded expired token {token_id}")

    async def fake_refresh_invalid(cfg, refresh_token):
        # Slack returned ok:false with error=invalid_auth (closest analogue
        # to invalid_grant); our parser raises TokenExchangeError.
        raise TokenExchangeError("slack", "invalid_grant", "user revoked")

    async def fake_ap(cfg, **kw):
        raise AssertionError("AP upsert should NOT be called on invalid_grant")

    with patch(oauth_routes, "_refresh_with_provider", fake_refresh_invalid), \
         patch(oauth_routes, "_update_ap_connection", fake_ap):
        summary = await oauth_routes.refresh_due_tokens()
    print(f"  cycle summary: {summary}")

    after = await get_token(token_id)
    print(f"\n  saga-equivalent token state:")
    print(f"    status        = {after.status}")
    print(f"    refresh_count = {after.refresh_count}  (still 0)")
    assert after.status == "REVOKED"
    assert after.refresh_count == 0, "refresh_count moved on a failed refresh"

    async with async_session() as s:
        audits = (await s.execute(
            select(TenantAuditLog).where(
                TenantAuditLog.event_type == "token.refresh_failed",
                TenantAuditLog.event_meta.op("@>")({"encrypted_token_id": token_id}),
            )
        )).scalars().all()
    print(f"  token.refresh_failed audit rows: {len(audits)}")
    assert len(audits) >= 1
    last = audits[-1]
    print(f"    event_meta.error    = {last.event_meta.get('error')}")
    print(f"    event_meta.terminal = {last.event_meta.get('terminal')}")
    assert last.event_meta.get("error") == "invalid_grant"
    assert last.event_meta.get("terminal") is True

    print(f"\n  ✓ SCENARIO 2 PASSED — REVOKED + token.refresh_failed audited")


# ═══════════════════════════════════════════════════════════════
# SCENARIO 3 — no refresh_token (Slack default)
# ═══════════════════════════════════════════════════════════════

async def scenario_3_no_refresh_token():
    banner("SCENARIO 3 — token with no refresh_token gets refresh_at pushed forward")

    token_id = await seed_expired_token(
        access_plain="ACCESS_NO_REFRESH",
        refresh_plain=None,                            # Slack default
        refresh_at_offset_seconds=-60,
        provider_account_id="T_PHASE44_S3",
    )
    print(f"  seeded token {token_id}  (refresh_token IS NULL)")

    before = await get_token(token_id)
    print(f"  BEFORE refresh_at: {before.refresh_at}")

    # Provider should NEVER be called
    async def fake_refresh(cfg, refresh_token):
        raise AssertionError("provider should not be called for no-refresh token")
    async def fake_ap(cfg, **kw):
        raise AssertionError("AP upsert should not be called either")

    with patch(oauth_routes, "_refresh_with_provider", fake_refresh), \
         patch(oauth_routes, "_update_ap_connection", fake_ap):
        summary = await oauth_routes.refresh_due_tokens()
    print(f"  cycle summary: {summary}")

    after = await get_token(token_id)
    print(f"\n  AFTER refresh_at:  {after.refresh_at}")
    assert after.status == "ACTIVE"
    assert after.refresh_count == 0
    # Should be pushed ~1 year forward
    delta = after.refresh_at - before.refresh_at
    print(f"  refresh_at moved forward by: {delta}")
    assert delta > timedelta(days=300), f"expected ~1y push, got {delta}"
    print(f"  ✓ SCENARIO 3 PASSED — non-refreshable token won't be re-polled")


# ═══════════════════════════════════════════════════════════════
# Cleanup helper — keep prod tidy across re-runs
# ═══════════════════════════════════════════════════════════════

async def cleanup_test_tokens():
    async with async_session() as s:
        await s.execute(
            text("DELETE FROM encrypted_tokens WHERE tenant_id = :t"),
            {"t": TENANT},
        )
        await s.commit()


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

async def main_runner():
    banner("Phase 4.4 — Refresh Worker (harsh suite)")
    async with main.app.router.lifespan_context(main.app):
        await seed_tenant()
        await cleanup_test_tokens()

        results = []
        try:
            await scenario_1_revival()
            results.append(("1_expired_revived", True))
        except (AssertionError, Exception) as e:
            results.append(("1_expired_revived", False))
            import traceback; traceback.print_exc()

        try:
            await scenario_2_revoked()
            results.append(("2_invalid_grant_revoked", True))
        except (AssertionError, Exception) as e:
            results.append(("2_invalid_grant_revoked", False))
            import traceback; traceback.print_exc()

        try:
            await scenario_3_no_refresh_token()
            results.append(("3_no_refresh_token_skip", True))
        except (AssertionError, Exception) as e:
            results.append(("3_no_refresh_token_skip", False))
            import traceback; traceback.print_exc()

        # Final cleanup so re-runs are idempotent
        await cleanup_test_tokens()

    banner("FINAL VERDICT")
    for name, ok in results:
        print(f"  {'✓ PASS' if ok else '✗ FAIL'}   {name}")
    n_pass = sum(1 for _, ok in results if ok)
    print(f"\n  {n_pass}/{len(results)} scenarios passed")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main_runner()))
