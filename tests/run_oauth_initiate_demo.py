"""
Phase 4.1 demo runner — invoke /v2/oauth/slack/initiate end-to-end against
production Postgres + production Redis, then verify that all four side-
effects landed: saga row, Redis nonce, audit log row, valid authorize URL.

The runner mounts the FastAPI app in-process via httpx ASGITransport,
so what we exercise is the SAME route handler the orchestrator service
will serve. No mocks for crypto, DB, or Redis.

Required env (set inline below for the demo):
    DATABASE_URL                (production Postgres public proxy)
    REDIS_URL                   (production Redis public proxy)
    SIYADAH_OAUTH_STATE_KEY     (state HMAC key — generate locally for demo)
    SIYADAH_OAUTH_MK            (master key — for crypto module load only)
    SLACK_CLIENT_ID             (placeholder — operator sets in Railway later)
    SLACK_REDIRECT_URI          (placeholder)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Generate ephemeral keys + creds BEFORE main is imported so module init
# sees them. These are LOCAL to the demo — production uses Railway secrets.
os.environ.setdefault("SIYADAH_OAUTH_STATE_KEY",
                      base64.urlsafe_b64encode(b"\x42" * 32).decode().rstrip("="))
os.environ.setdefault("SIYADAH_OAUTH_MK",
                      base64.urlsafe_b64encode(b"\x07" * 32).decode().rstrip("="))
os.environ.setdefault("SLACK_CLIENT_ID", "DEMO_SLACK_CLIENT_ID_12345")
os.environ.setdefault("SLACK_REDIRECT_URI",
                      "https://siyadah-orchestrator-production.up.railway.app"
                      "/v2/oauth/slack/callback")

# Production endpoints
os.environ.setdefault("DATABASE_URL",
                      "postgresql+asyncpg://postgres:utYxWmdoDWsJRYAioDgsDnYEhfHQgsjz"
                      "@caboose.proxy.rlwy.net:28585/railway")
os.environ.setdefault("REDIS_URL",
                      "redis://default:PVtXVtYgmXPgOWhvUfxRuYtBvriMwhrj"
                      "@nozomi.proxy.rlwy.net:56937")
os.environ.setdefault("SIYADAH_SKIP_PG_SSL", "1")
os.environ.setdefault("ORCHESTRATOR_ALLOWED_ORIGINS", "http://testclient")
os.environ.setdefault("AP_BASE_URL", "https://activepieces-production-2499.up.railway.app")
os.environ.setdefault("AP_EMAIL", "")
os.environ.setdefault("AP_PASSWORD", "")
os.environ.setdefault("AP_PROJECT_ID", "ou4jOTA4KMnDrzOVsKWvd")

import hashlib as _hashlib   # noqa: E402
import httpx                  # noqa: E402
from sqlalchemy import select, text   # noqa: E402

import main                   # noqa: E402  (triggers app + lifespan setup)
from database import async_session, engine, init_db   # noqa: E402
from models import OAuthSaga, Project, TenantApiKey, TenantAuditLog   # noqa: E402


# Demo tenant — created if missing, kept around so the user can inspect
# the audit row afterwards. Idempotent re-run safe.
TENANT = "demo-oauth-phase41"
RAW_KEY = "demo-key-phase41-" + "x" * 40


def banner(label: str):
    print(f"\n{'═' * 72}\n  {label}\n{'═' * 72}")


async def seed_tenant():
    """Idempotently create the demo project + API key."""
    key_hash = _hashlib.sha256(RAW_KEY.encode()).hexdigest()
    async with async_session() as s:
        existing = (await s.execute(
            select(Project).where(Project.project_id == TENANT)
        )).scalar_one_or_none()
        if not existing:
            s.add(Project(project_id=TENANT, name="OAuth Phase 4.1 demo"))
            s.add(TenantApiKey(
                project_id=TENANT, key_hash=key_hash,
                label="phase-4.1-demo", scopes=["read", "write"],
            ))
            await s.commit()
            print(f"  seeded tenant '{TENANT}' + api key")
        else:
            # Ensure key exists too
            kx = (await s.execute(
                select(TenantApiKey).where(TenantApiKey.key_hash == key_hash)
            )).scalar_one_or_none()
            if not kx:
                s.add(TenantApiKey(
                    project_id=TENANT, key_hash=key_hash,
                    label="phase-4.1-demo", scopes=["read", "write"],
                ))
                await s.commit()
            print(f"  tenant '{TENANT}' already exists — reusing")


async def run_initiate(client: httpx.AsyncClient) -> dict:
    """Hit POST /v2/oauth/slack/initiate exactly as the BFF would."""
    resp = await client.post(
        "/v2/oauth/slack/initiate",
        headers={"X-API-Key": RAW_KEY, "X-Siyadah-Tenant": TENANT},
        json={"return_path": "/dashboard/integrations"},
    )
    print(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"  body: {resp.text[:500]}")
        raise RuntimeError(f"initiate failed: {resp.status_code}")
    return resp.json()


async def verify_saga_in_db(saga_id: str):
    """Find the saga we just created. Print every field so the user can
    see the 'birth' in detail."""
    async with async_session() as s:
        saga = (await s.execute(
            select(OAuthSaga).where(OAuthSaga.id == saga_id)
        )).scalar_one_or_none()
    if not saga:
        raise AssertionError(f"saga {saga_id} not in DB")
    print(f"\n  ┌──── oauth_sagas row (born) ────")
    print(f"  │ id                    : {saga.id}")
    print(f"  │ tenant_id             : {saga.tenant_id}")
    print(f"  │ provider              : {saga.provider}")
    print(f"  │ status                : {saga.status}")
    print(f"  │ scope                 : {saga.scope}")
    print(f"  │ state_nonce           : {saga.state_nonce[:16]}…")
    print(f"  │ pkce_verifier         : {saga.pkce_verifier[:16]}…  (64-char b64url)")
    print(f"  │ initiated_at          : {saga.initiated_at}")
    print(f"  │ expires_at            : {saga.expires_at}")
    print(f"  │ encrypted_token_id    : {saga.encrypted_token_id}  (will fill at TOKEN_OBTAINED)")
    print(f"  │ ap_connection_externalId: {saga.ap_connection_external_id}")
    print(f"  └────────────────────────")
    assert saga.status == "INITIATED"
    assert saga.provider == "slack"
    assert len(saga.pkce_verifier) == 64
    return saga


async def verify_redis_nonce(nonce: str, tenant: str):
    """Confirm the nonce is registered in production Redis with TTL ≤ 300."""
    from mcp_sse import _redis
    if _redis is None:
        raise RuntimeError("mcp_sse._redis is None — Redis not initialised")
    key = f"oauth:nonce:{nonce}"
    val = await _redis.get(key)
    ttl = await _redis.ttl(key)
    print(f"\n  ┌──── Redis nonce binding ────")
    print(f"  │ key  : {key[:40]}…")
    print(f"  │ value: {val}  (== tenant_id)")
    print(f"  │ ttl  : {ttl}s  (≤ 300)")
    print(f"  └────────────────────────")
    assert val == tenant or (isinstance(val, bytes) and val.decode() == tenant)
    assert 0 < ttl <= 300


async def verify_audit_event(saga_id: str):
    """The oauth.initiated row in tenant_audit_log."""
    async with async_session() as s:
        rows = (await s.execute(
            select(TenantAuditLog).where(
                TenantAuditLog.event_type == "oauth.initiated",
                TenantAuditLog.project_id == TENANT,
            ).order_by(TenantAuditLog.occurred_at.desc()).limit(5)
        )).scalars().all()
    matching = [r for r in rows if r.event_meta and r.event_meta.get("saga_id") == saga_id]
    print(f"\n  ┌──── tenant_audit_log row (oauth.initiated) ────")
    if not matching:
        print(f"  │ ✗ NO matching row found for saga {saga_id}")
        print(f"  │   (recent oauth.initiated events: {len(rows)})")
        raise AssertionError("audit event missing")
    a = matching[0]
    print(f"  │ id           : {a.id}")
    print(f"  │ occurred_at  : {a.occurred_at}")
    print(f"  │ project_id   : {a.project_id}")
    print(f"  │ endpoint     : {a.endpoint}")
    print(f"  │ http_status  : {a.http_status}")
    print(f"  │ event_type   : {a.event_type}")
    print(f"  │ event_meta   : {a.event_meta}")
    print(f"  └────────────────────────")
    assert a.event_type == "oauth.initiated"
    assert a.event_meta.get("provider") == "slack"
    assert a.event_meta.get("saga_id") == saga_id


def verify_authorize_url(url: str):
    """Decompose the URL and check every important parameter."""
    parsed = urlparse(url)
    params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
    print(f"\n  ┌──── authorization_url decomposition ────")
    print(f"  │ scheme + host  : {parsed.scheme}://{parsed.netloc}")
    print(f"  │ path           : {parsed.path}")
    print(f"  │ params:")
    for k, v in params.items():
        v_show = v if len(str(v)) < 80 else str(v)[:77] + "…"
        print(f"  │   {k:25s} = {v_show}")
    print(f"  └────────────────────────")
    assert parsed.netloc == "slack.com"
    assert parsed.path == "/oauth/v2/authorize"
    assert params["client_id"] == "DEMO_SLACK_CLIENT_ID_12345"
    assert params["response_type"] == "code"
    assert "chat:write" in params["scope"]
    assert "code_challenge" not in params, "Slack uses no PKCE — should be absent"
    assert "state" in params and len(params["state"]) > 50


async def main_runner():
    banner("Phase 4.1 — OAuth Initiate (Slack)")
    print("Bootstrapping app context (lifespan: DB init, Redis init) …")

    # We need to enter the app's lifespan so DB + Redis are live.
    async with main.app.router.lifespan_context(main.app):
        print("  ✓ lifespan started — DB + Redis ready")

        await seed_tenant()

        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testclient", timeout=30,
        ) as c:
            banner("STEP 1 — POST /v2/oauth/slack/initiate")
            data = await run_initiate(c)
            saga_id = data["saga_id"]
            auth_url = data["authorization_url"]
            print(f"  saga_id        : {saga_id}")
            print(f"  expires_at     : {data['expires_at']}")
            print(f"  scopes         : {data['scopes']}")

            banner("STEP 2 — Verify saga 'born' in oauth_sagas")
            saga = await verify_saga_in_db(saga_id)

            banner("STEP 3 — Verify nonce in production Redis")
            await verify_redis_nonce(saga.state_nonce, TENANT)

            banner("STEP 4 — Verify oauth.initiated audit event")
            await verify_audit_event(saga_id)

            banner("STEP 5 — Inspect authorization_url")
            verify_authorize_url(auth_url)

            # Compensation test — second initiate produces a SECOND saga
            # (different nonce) without colliding. Idempotency at the
            # PROVIDER+TENANT level is intentionally not enforced —
            # users can re-initiate at will.
            banner("STEP 6 — Second initiate creates a fresh saga")
            data2 = await run_initiate(c)
            assert data2["saga_id"] != saga_id, "saga_id collided"
            print(f"  ✓ second saga_id: {data2['saga_id']} (distinct)")

    banner("FINAL VERDICT")
    print("  ✓ STEP 1: POST returned 200 with saga_id + authorization_url")
    print("  ✓ STEP 2: oauth_sagas row exists with status=INITIATED")
    print("  ✓ STEP 3: Redis nonce registered with valid TTL")
    print("  ✓ STEP 4: tenant_audit_log captured oauth.initiated event")
    print("  ✓ STEP 5: authorization_url is well-formed for slack.com")
    print("  ✓ STEP 6: re-initiate yields a fresh saga (no collision)")
    print(f"\n  Phase 4.1 PASSED — first OAuth saga is alive in production.")


if __name__ == "__main__":
    asyncio.run(main_runner())
