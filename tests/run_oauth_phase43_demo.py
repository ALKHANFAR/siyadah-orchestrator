"""
Phase 4.3 harsh test runner — AP connection link + L5 compensation.

Three scenarios against production Postgres + production Redis:

  1. HAPPY PATH (mocked AP success)
     • Token exchange mocked to return canary tokens
     • _create_ap_connection mocked to return a fake AP connection record
     • Saga climaxes: TOKEN_OBTAINED → COMPLETED
     • encrypted_tokens.ap_connection_external_id stamped
     • Audit chain: oauth.token_exchanged → oauth.completed

  2. AP FAILURE → L5 COMPENSATION (mocked AP failure)
     • Token exchange succeeds, encryption succeeds
     • _create_ap_connection raises (simulated AP 5xx / network)
     • Forensic verification:
        - encrypted_tokens row WIPED (DELETE)
        - saga transitions to COMPENSATED with failure_step='ap_connection'
        - audit: oauth.saga_compensated written
        - PLAINTEXT canary scan still finds 0 leaks (defence-in-depth)
        - 0% orphaned data: encrypted_token_id is NULL on the saga

  3. PRODUCTION-PARITY DRY RUN
     • Use real engine.create_app_connection but with intentionally
       BAD token (provider rejects) to verify the natural failure path
       triggers the SAME compensation branch end-to-end against the
       real Activepieces API.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import sys
from contextlib import contextmanager
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
# Real AP creds so engine.token is populated for production-parity scenario 3
os.environ.setdefault("AP_EMAIL", "a@siyadah-ai.com")
os.environ.setdefault("AP_PASSWORD", "Siyadah2026pass")
os.environ.setdefault("AP_PROJECT_ID", "ou4jOTA4KMnDrzOVsKWvd")

from urllib.parse import parse_qs, urlparse  # noqa: E402

import httpx                                  # noqa: E402
from sqlalchemy import select, text           # noqa: E402

import main                                    # noqa: E402
import oauth_routes                            # noqa: E402
from database import async_session, engine     # noqa: E402
from models import (                           # noqa: E402
    EncryptedToken, OAuthSaga, Project, TenantApiKey, TenantAuditLog,
)
from oauth_providers import ParsedTokenResponse  # noqa: E402


TENANT = "phase43-demo"
RAW_KEY = "phase43-key-" + "z" * 40
HEADERS = {"X-API-Key": RAW_KEY, "X-Siyadah-Tenant": TENANT}
PLAINTEXT_TOKEN = "P43_LEAK_CANARY_xoxb-PHASE43-AAAA"


def banner(label: str):
    print(f"\n{'═' * 76}\n  {label}\n{'═' * 76}")


@contextmanager
def patch_exchange(fn):
    saved = oauth_routes._exchange_code
    oauth_routes._exchange_code = fn
    try:
        yield
    finally:
        oauth_routes._exchange_code = saved


@contextmanager
def patch_ap_link(fn):
    saved = oauth_routes._create_ap_connection
    oauth_routes._create_ap_connection = fn
    try:
        yield
    finally:
        oauth_routes._create_ap_connection = saved


async def seed_tenant():
    key_hash = hashlib.sha256(RAW_KEY.encode()).hexdigest()
    async with async_session() as s:
        existing = (await s.execute(
            select(Project).where(Project.project_id == TENANT)
        )).scalar_one_or_none()
        if not existing:
            s.add(Project(project_id=TENANT, name="Phase 4.3 demo"))
            s.add(TenantApiKey(
                project_id=TENANT, key_hash=key_hash,
                label="phase-4.3", scopes=["read", "write"],
            ))
            await s.commit()
            print(f"  seeded tenant '{TENANT}'")
        else:
            kx = (await s.execute(
                select(TenantApiKey).where(TenantApiKey.key_hash == key_hash)
            )).scalar_one_or_none()
            if not kx:
                s.add(TenantApiKey(
                    project_id=TENANT, key_hash=key_hash,
                    label="phase-4.3", scopes=["read", "write"],
                ))
                await s.commit()
            print(f"  tenant '{TENANT}' reused")


async def initiate(client: httpx.AsyncClient) -> tuple[str, str]:
    r = await client.post(
        "/v2/oauth/slack/initiate",
        headers=HEADERS,
        json={"return_path": "/post-install"},
    )
    assert r.status_code == 200, f"initiate failed: {r.status_code} {r.text}"
    j = r.json()
    state = parse_qs(urlparse(j["authorization_url"]).query)["state"][0]
    return j["saga_id"], state


async def get_saga(saga_id: str) -> OAuthSaga:
    async with async_session() as s:
        return (await s.execute(
            select(OAuthSaga).where(OAuthSaga.id == saga_id)
        )).scalar_one()


async def get_token(token_id: str) -> EncryptedToken | None:
    async with async_session() as s:
        return (await s.execute(
            select(EncryptedToken).where(EncryptedToken.id == token_id)
        )).scalar_one_or_none()


async def search_plaintext(needle: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with engine.connect() as conn:
        counts["encrypted_tokens.encrypted_access_token"] = (await conn.execute(
            text("SELECT count(*) FROM encrypted_tokens "
                 "WHERE encode(encrypted_access_token, 'escape') LIKE :p"),
            {"p": f"%{needle}%"},
        )).scalar_one()
        counts["oauth_sagas.failure_reason"] = (await conn.execute(
            text("SELECT count(*) FROM oauth_sagas "
                 "WHERE failure_reason IS NOT NULL AND failure_reason LIKE :p"),
            {"p": f"%{needle}%"},
        )).scalar_one()
        counts["tenant_audit_log.event_meta"] = (await conn.execute(
            text("SELECT count(*) FROM tenant_audit_log "
                 "WHERE event_meta IS NOT NULL "
                 "  AND event_meta::text LIKE :p"),
            {"p": f"%{needle}%"},
        )).scalar_one()
    return counts


# ═══════════════════════════════════════════════════════════════
# SCENARIO 1 — Happy path with mocked AP success
# ═══════════════════════════════════════════════════════════════

async def scenario_1_happy(client: httpx.AsyncClient) -> dict:
    banner("SCENARIO 1 — HAPPY PATH (mocked AP success)")
    saga_id, state = await initiate(client)
    print(f"  initiated saga {saga_id}")

    async def fake_exchange(cfg, code, *, verifier=None):
        return ParsedTokenResponse(
            access_token="P43_HAPPY_xoxb-real-looking-token",
            refresh_token=None,
            expires_in=43200,
            scopes=["chat:write", "channels:read", "users:read"],
            provider_account_id="T_PHASE43_HAPPY",
        )

    async def fake_ap_link(cfg, *, saga_id, tenant_id, access_token, parsed):
        # Verify the linker received the decrypted token (not ciphertext)
        assert access_token.startswith("P43_HAPPY_"), \
            f"linker received non-plaintext: {access_token[:20]!r}"
        return {
            "id": "ap_demo_id_99999",
            "externalId": f"siyadah-{saga_id[:16]}",
            "displayName": f"Siyadah Slack ({tenant_id})",
            "type": "CUSTOM_AUTH",
        }

    with patch_exchange(fake_exchange), patch_ap_link(fake_ap_link):
        r = await client.get(
            f"/v2/oauth/slack/callback?code=demo&state={state}",
            follow_redirects=False,
        )
    print(f"  HTTP {r.status_code}")
    assert r.status_code == 200, f"happy path failed: {r.text}"
    body = r.json()
    print(f"  saga.status        = {body['status']}")
    print(f"  encrypted_token_id = {body['encrypted_token_id']}")

    saga = await get_saga(saga_id)
    print(f"  DB saga.status                      = {saga.status}")
    print(f"  DB saga.ap_connection_external_id   = {saga.ap_connection_external_id}")
    print(f"  DB saga.completed_at                = {saga.completed_at}")

    tok = await get_token(body["encrypted_token_id"])
    print(f"  DB token.ap_connection_external_id  = {tok.ap_connection_external_id}")
    print(f"  DB token.status                     = {tok.status}")

    assert saga.status == "COMPLETED"
    assert saga.ap_connection_external_id == f"siyadah-{saga_id[:16]}"
    assert saga.completed_at is not None
    assert tok.ap_connection_external_id == saga.ap_connection_external_id
    assert tok.status == "ACTIVE"

    # Verify oauth.completed audit row
    async with async_session() as s:
        completed = (await s.execute(
            select(TenantAuditLog).where(
                TenantAuditLog.event_type == "oauth.completed",
                TenantAuditLog.event_meta.op("@>")({"saga_id": saga_id}),
            )
        )).scalars().all()
    assert len(completed) == 1, f"expected 1 oauth.completed, got {len(completed)}"
    print(f"  ✓ oauth.completed audit written")
    print(f"  ✓ saga COMPLETED, encrypted_tokens linked to AP externalId")
    return {"saga_id": saga_id, "token_id": body["encrypted_token_id"]}


# ═══════════════════════════════════════════════════════════════
# SCENARIO 2 — AP failure → L5 compensation
# ═══════════════════════════════════════════════════════════════

async def scenario_2_compensation(client: httpx.AsyncClient):
    banner("SCENARIO 2 — AP FAILURE → L5 COMPENSATING ROLLBACK")
    saga_id, state = await initiate(client)
    print(f"  initiated saga {saga_id}")

    async def fake_exchange(cfg, code, *, verifier=None):
        return ParsedTokenResponse(
            access_token=PLAINTEXT_TOKEN,
            refresh_token="P43_REFRESH_CANARY_xoxe-PHASE43-BBBB",
            expires_in=43200,
            scopes=["chat:write"],
            provider_account_id="T_PHASE43_FAILURE",
        )

    async def fake_ap_failure(cfg, *, saga_id, tenant_id, access_token, parsed):
        # Simulate AP being down / 5xx
        raise RuntimeError(
            "AP create-connection failed: 503 {'code': 'INTERNAL_ERROR'}"
        )

    # Snapshot DB state BEFORE
    async with async_session() as s:
        n_tokens_before = (await s.execute(
            text("SELECT count(*) FROM encrypted_tokens "
                 "WHERE tenant_id = :t"),
            {"t": TENANT},
        )).scalar_one()

    with patch_exchange(fake_exchange), patch_ap_link(fake_ap_failure):
        r = await client.get(
            f"/v2/oauth/slack/callback?code=demo&state={state}",
            follow_redirects=False,
        )
    print(f"  HTTP {r.status_code}")
    assert r.status_code == 503, f"expected 503, got {r.status_code}: {r.text}"
    detail = r.json().get("detail", {})
    print(f"  detail: {detail}")
    assert detail.get("error") == "ap_connection_failed"
    assert detail.get("saga_status") == "COMPENSATED"

    # ── Forensic verification: 0 orphaned data ──
    saga = await get_saga(saga_id)
    print(f"\n  Post-failure forensic state:")
    print(f"    saga.status              = {saga.status}")
    print(f"    saga.failure_step        = {saga.failure_step}")
    print(f"    saga.encrypted_token_id  = {saga.encrypted_token_id}  (must be NULL)")
    print(f"    saga.ap_connection_ext_id= {saga.ap_connection_external_id}  (must be NULL)")
    print(f"    saga.completed_at        = {saga.completed_at}")
    print(f"    saga.failure_reason      = {(saga.failure_reason or '')[:80]}…")

    assert saga.status == "COMPENSATED", f"expected COMPENSATED, got {saga.status}"
    assert saga.failure_step == "ap_connection"
    assert saga.encrypted_token_id is None, "saga still references a token row!"
    assert saga.ap_connection_external_id is None
    assert "ap" in (saga.failure_reason or "").lower() or "503" in (saga.failure_reason or "")

    # encrypted_tokens row should be GONE
    async with async_session() as s:
        n_tokens_after = (await s.execute(
            text("SELECT count(*) FROM encrypted_tokens "
                 "WHERE tenant_id = :t"),
            {"t": TENANT},
        )).scalar_one()
    print(f"  encrypted_tokens for tenant: {n_tokens_before} → {n_tokens_after}")
    assert n_tokens_after == n_tokens_before, \
        f"a token row leaked through compensation: {n_tokens_before} → {n_tokens_after}"

    # No row anywhere references this saga any more
    async with async_session() as s:
        orphan = (await s.execute(
            text("SELECT count(*) FROM encrypted_tokens et "
                 "WHERE et.tenant_id = :t AND et.provider = 'slack' "
                 "  AND et.provider_account_id = 'T_PHASE43_FAILURE'"),
            {"t": TENANT},
        )).scalar_one()
    print(f"  encrypted_tokens for THIS account_id: {orphan}  (must be 0)")
    assert orphan == 0, "compensation left an orphan encrypted_tokens row"

    # ── Audit row ──
    async with async_session() as s:
        comp_audits = (await s.execute(
            select(TenantAuditLog).where(
                TenantAuditLog.event_type == "oauth.saga_compensated",
                TenantAuditLog.event_meta.op("@>")({"saga_id": saga_id}),
            )
        )).scalars().all()
    print(f"  oauth.saga_compensated audit rows: {len(comp_audits)}")
    assert len(comp_audits) == 1
    audit = comp_audits[0]
    print(f"    event_meta.compensation_step = {audit.event_meta.get('compensation_step')}")
    print(f"    event_meta.wiped_token_id    = {audit.event_meta.get('wiped_encrypted_token_id')}")
    assert audit.event_meta.get("compensation_step") == "ap_connection"

    # ── Plaintext canary scan after compensation ──
    print(f"\n  Forensic plaintext scan for {PLAINTEXT_TOKEN!r}:")
    counts = await search_plaintext(PLAINTEXT_TOKEN)
    leaked = False
    for col, n in counts.items():
        flag = "✗ LEAK" if n > 0 else "✓ clean"
        print(f"     {flag}  {col:50s} {n} match(es)")
        if n > 0:
            leaked = True
    assert not leaked, "plaintext leaked through compensation"
    print("\n  ✓ 0% orphaned data — compensation wiped cleanly")


# ═══════════════════════════════════════════════════════════════
# SCENARIO 3 — Production parity (REAL AP, intentionally bad token)
# ═══════════════════════════════════════════════════════════════

async def scenario_3_real_ap_rejection(client: httpx.AsyncClient):
    banner("SCENARIO 3 — REAL AP REJECTS BAD TOKEN → SAME COMPENSATION PATH")
    saga_id, state = await initiate(client)
    print(f"  initiated saga {saga_id}")

    # Real exchange path is mocked to give a token that AP will reject
    # via Slack auth_test (the natural failure mode). We DO NOT patch
    # _create_ap_connection — the real one runs against production AP.
    async def fake_exchange(cfg, code, *, verifier=None):
        return ParsedTokenResponse(
            access_token="xoxb-DELIBERATELY_INVALID_TOKEN_PHASE43",
            refresh_token=None,
            expires_in=43200,
            scopes=["chat:write"],
            provider_account_id="T_PHASE43_REAL_AP",
        )

    with patch_exchange(fake_exchange):
        r = await client.get(
            f"/v2/oauth/slack/callback?code=demo&state={state}",
            follow_redirects=False,
        )
    print(f"  HTTP {r.status_code}")
    print(f"  body : {r.text[:200]}")

    saga = await get_saga(saga_id)
    print(f"\n  saga.status         = {saga.status}")
    print(f"  saga.failure_step   = {saga.failure_step}")
    print(f"  saga.failure_reason = {(saga.failure_reason or '')[:120]}…")

    # Real AP REJECTS the bad token — must produce the same COMPENSATED outcome
    assert r.status_code == 503, f"expected 503 from natural AP failure, got {r.status_code}"
    assert saga.status == "COMPENSATED"
    assert saga.failure_step == "ap_connection"
    assert saga.encrypted_token_id is None

    # No encrypted_tokens row should exist for this saga's account
    async with async_session() as s:
        n = (await s.execute(
            text("SELECT count(*) FROM encrypted_tokens "
                 "WHERE provider_account_id = 'T_PHASE43_REAL_AP'"),
        )).scalar_one()
    print(f"  encrypted_tokens for T_PHASE43_REAL_AP: {n}  (must be 0)")
    assert n == 0
    print("  ✓ REAL AP failure handled with the same L5 compensation path")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

async def main_runner():
    banner("Phase 4.3 — AP Connection Linker + L5 Compensation (harsh suite)")
    async with main.app.router.lifespan_context(main.app):
        await seed_tenant()
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testclient", timeout=60,
        ) as client:
            results = []
            try:
                _ = await scenario_1_happy(client)
                results.append(("1_happy_path", True))
            except AssertionError:
                results.append(("1_happy_path", False))
                import traceback; traceback.print_exc()

            try:
                await scenario_2_compensation(client)
                results.append(("2_compensation_rollback", True))
            except AssertionError:
                results.append(("2_compensation_rollback", False))
                import traceback; traceback.print_exc()

            try:
                await scenario_3_real_ap_rejection(client)
                results.append(("3_real_ap_rejection", True))
            except AssertionError:
                results.append(("3_real_ap_rejection", False))
                import traceback; traceback.print_exc()

    banner("FINAL VERDICT")
    for name, ok in results:
        print(f"  {'✓ PASS' if ok else '✗ FAIL'}   {name}")
    n_pass = sum(1 for _, ok in results if ok)
    print(f"\n  {n_pass}/{len(results)} scenarios passed")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main_runner()))
