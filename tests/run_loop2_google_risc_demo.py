"""
Loop 2 — Google RISC Webhook (JWT verification + JWKS caching).

Eight scenarios against production Postgres + production Redis.

  1. VALID JWT + tokens-revoked event → tokens REVOKED + audit
  2. EXPIRED JWT → 401 (no DB mutation)
  3. WRONG-KEY JWT (signed by attacker key) → 401
  4. WRONG ISSUER → 401
  5. WRONG AUDIENCE → 401
  6. ALG=none ATTACK → 401 (PyJWT enforces RS256 allow-list)
  7. PUB/SUB MESSAGE-ID DEDUPE (replay) → first 200, second 200 with duplicate=True
  8. EVENT TYPE NOT MATCHED (e.g. account-enabled) → 200 handled=False, no revocation

JWKS substitution: we generate a local RSA keypair, build a JWKS that
contains its public key, and monkey-patch _get_google_jwks_client to
use a stub PyJWKClient that returns our local key. This lets us forge
"valid" Google-signed tokens for the test without touching real Google.
"""
from __future__ import annotations

import asyncio
import base64 as b64
import hashlib
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Env BEFORE main is imported
os.environ.setdefault(
    "SIYADAH_OAUTH_STATE_KEY",
    b64.urlsafe_b64encode(b"\x42" * 32).decode().rstrip("="),
)
os.environ.setdefault(
    "SIYADAH_OAUTH_MK",
    b64.urlsafe_b64encode(b"\x07" * 32).decode().rstrip("="),
)
os.environ.setdefault("SLACK_CLIENT_ID", "DEMO")
os.environ.setdefault("SLACK_CLIENT_SECRET", "DEMO")
os.environ.setdefault("SLACK_REDIRECT_URI", "https://example.com/cb")
os.environ.setdefault("SLACK_SIGNING_SECRET", "ignored-for-loop2")
os.environ["GOOGLE_PUBSUB_AUDIENCE"] = "https://orchestrator.example.com/v2/webhooks/google/risc"
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
import jwt                           # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives import serialization   # noqa: E402
from sqlalchemy import select, text  # noqa: E402

import main                          # noqa: E402
import oauth_webhooks                # noqa: E402
from database import async_session   # noqa: E402
from models import (                 # noqa: E402
    EncryptedToken, Project, TenantApiKey, TenantAuditLog,
)
from siyadah_crypto import CryptoProvider  # noqa: E402


TENANT = "loop2-google-risc"
AUDIENCE = os.environ["GOOGLE_PUBSUB_AUDIENCE"]
ISSUER = "https://accounts.google.com"


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
# Local RSA keypair + JWKS stub
# ═══════════════════════════════════════════════════════════════

print("Generating ephemeral RSA-2048 keypair for the test JWKS …")
GOOD_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ATTACKER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

GOOD_KEY_PEM = GOOD_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
)
GOOD_PUB_PEM = GOOD_KEY.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)
ATTACKER_KEY_PEM = ATTACKER_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
)


class _StubSigningKey:
    def __init__(self, public_key):
        self.key = public_key


class _StubJWKSClient:
    """Drop-in for PyJWKClient. Returns our local public key for any
    `kid`. Real Google keys aren't fetched — pure local verification."""
    def __init__(self):
        self._pub = GOOD_KEY.public_key()
        self._fetch_count = 0

    def get_signing_key_from_jwt(self, token: str):
        self._fetch_count += 1
        return _StubSigningKey(self._pub)


STUB_JWKS = _StubJWKSClient()


def _patch_jwks():
    """Replace oauth_webhooks._get_google_jwks_client with our stub."""
    return patch(oauth_webhooks, "_get_google_jwks_client", lambda: STUB_JWKS)


# ═══════════════════════════════════════════════════════════════
# JWT helpers
# ═══════════════════════════════════════════════════════════════

def make_id_token(
    *,
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    exp_offset: int = 600,           # 10 min in the future
    iat_offset: int = 0,
    private_key_pem: bytes = GOOD_KEY_PEM,
    algorithm: str = "RS256",
    extra_claims: dict | None = None,
) -> str:
    """Build a Google-style ID token. Signed with GOOD_KEY by default."""
    now = int(time.time())
    claims = {
        "iss": issuer,
        "aud": audience,
        "iat": now + iat_offset,
        "exp": now + exp_offset,
        "email": "service-account@google.com",
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, private_key_pem, algorithm=algorithm)


def make_set_jwt(
    *, google_sub: str, event_types: list[str] | None = None,
    jti: str | None = None,
) -> str:
    """Build the inner Security Event Token (RISC payload). The outer
    handler decodes this WITHOUT verifying its signature — the outer
    ID token is the gate."""
    if event_types is None:
        event_types = ["https://schemas.openid.net/secevent/risc/event-type/tokens-revoked"]
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "siyadah-orchestrator-app",
        "iat": int(time.time()),
        "jti": jti or uuid.uuid4().hex,
        "sub": google_sub,
        "events": {et: {"subject": {"sub": google_sub}} for et in event_types},
    }
    # Use a throwaway key — handler doesn't verify the inner SET signature
    return jwt.encode(claims, GOOD_KEY_PEM, algorithm="RS256")


def make_pubsub_envelope(set_jwt: str, *, message_id: str | None = None) -> dict:
    """Build the Pub/Sub push payload."""
    return {
        "subscription": "projects/siyadah/subscriptions/risc",
        "message": {
            "data": b64.b64encode(set_jwt.encode()).decode(),
            "messageId": message_id or uuid.uuid4().hex,
            "publishTime": datetime.now(timezone.utc).isoformat(),
        },
    }


# ═══════════════════════════════════════════════════════════════
# DB seeding
# ═══════════════════════════════════════════════════════════════

async def seed_tenant():
    async with async_session() as s:
        existing = (await s.execute(
            select(Project).where(Project.project_id == TENANT)
        )).scalar_one_or_none()
        if not existing:
            s.add(Project(project_id=TENANT, name="Loop2 Google RISC demo"))
            s.add(TenantApiKey(
                project_id=TENANT,
                key_hash=hashlib.sha256(b"loop2-key").hexdigest(),
                label="loop2", scopes=["read", "write"],
            ))
            await s.commit()


async def cleanup():
    async with async_session() as s:
        await s.execute(
            text("DELETE FROM encrypted_tokens WHERE tenant_id = :t"),
            {"t": TENANT},
        )
        await s.commit()


async def seed_google_token(google_sub: str) -> str:
    crypto = CryptoProvider.from_env()
    new_id = str(uuid.uuid4())
    aad = f"{TENANT}|google|{new_id}".encode()
    dek = crypto.gen_dek()
    try:
        wrapped = crypto.wrap_dek(dek, aad=aad)
        sealed_a = crypto.encrypt_with_dek(b"ya29.demo", dek, aad=aad + b"|access")
    finally:
        del dek
    async with async_session() as s:
        s.add(EncryptedToken(
            id=new_id, tenant_id=TENANT, provider="google",
            provider_account_id=google_sub,
            encrypted_access_token=sealed_a.ciphertext, iv_access=sealed_a.iv,
            wrapped_dek=wrapped.ciphertext, iv_dek=wrapped.iv,
            encryption_version=wrapped.version,
            scopes=["openid"], status="ACTIVE",
        ))
        await s.commit()
    return new_id


async def get_status(token_id: str) -> str | None:
    async with async_session() as s:
        return (await s.execute(
            select(EncryptedToken.status).where(EncryptedToken.id == token_id)
        )).scalar_one_or_none()


# ═══════════════════════════════════════════════════════════════
# SCENARIOS
# ═══════════════════════════════════════════════════════════════

async def scenario_1_valid_revoked(client: httpx.AsyncClient):
    banner("1 — VALID JWT + tokens-revoked → tokens REVOKED")
    google_sub = f"google-sub-S1-{uuid.uuid4().hex[:8]}"
    token_id = await seed_google_token(google_sub)
    set_jwt = make_set_jwt(google_sub=google_sub)
    envelope = make_pubsub_envelope(set_jwt)
    id_token = make_id_token()

    with _patch_jwks():
        r = await client.post(
            "/v2/webhooks/google/risc",
            headers={"Authorization": f"Bearer {id_token}"},
            json=envelope,
        )
    print(f"  HTTP {r.status_code}  body: {r.json()}")
    assert r.status_code == 200
    body = r.json()
    assert body.get("revoked_tokens") == 1

    status = await get_status(token_id)
    print(f"  status_after = {status}")
    assert status == "REVOKED"

    async with async_session() as s:
        audits = (await s.execute(
            select(TenantAuditLog).where(
                TenantAuditLog.event_type == "oauth.revoked",
                TenantAuditLog.event_meta.op("@>")(
                    {"encrypted_token_id": token_id, "provider": "google"}
                ),
            )
        )).scalars().all()
    assert len(audits) == 1
    a = audits[0]
    assert a.event_meta.get("revoked_via") == "webhook"
    print(f"  audit oauth.revoked: ✓ provider=google google_event_id={a.event_meta.get('google_event_id')}")
    print("  ✓ SCENARIO 1 PASSED")


async def scenario_2_expired(client: httpx.AsyncClient):
    banner("2 — EXPIRED JWT → 401")
    google_sub = f"google-sub-S2-{uuid.uuid4().hex[:8]}"
    token_id = await seed_google_token(google_sub)
    # Build an exp=10s-in-the-past token
    id_token = make_id_token(exp_offset=-10, iat_offset=-100)
    envelope = make_pubsub_envelope(make_set_jwt(google_sub=google_sub))

    with _patch_jwks():
        r = await client.post(
            "/v2/webhooks/google/risc",
            headers={"Authorization": f"Bearer {id_token}"},
            json=envelope,
        )
    print(f"  HTTP {r.status_code}  detail: {r.text[:160]}")
    assert r.status_code == 401
    assert "expired" in r.text.lower()
    assert await get_status(token_id) == "ACTIVE"
    print("  ✓ SCENARIO 2 PASSED — expired JWT rejected, no DB mutation")


async def scenario_3_wrong_key(client: httpx.AsyncClient):
    banner("3 — JWT signed by attacker's key → 401")
    google_sub = f"google-sub-S3-{uuid.uuid4().hex[:8]}"
    token_id = await seed_google_token(google_sub)
    id_token = make_id_token(private_key_pem=ATTACKER_KEY_PEM)
    envelope = make_pubsub_envelope(make_set_jwt(google_sub=google_sub))

    with _patch_jwks():
        r = await client.post(
            "/v2/webhooks/google/risc",
            headers={"Authorization": f"Bearer {id_token}"},
            json=envelope,
        )
    print(f"  HTTP {r.status_code}  detail: {r.text[:160]}")
    assert r.status_code == 401
    assert "signature" in r.text.lower()
    assert await get_status(token_id) == "ACTIVE"
    print("  ✓ SCENARIO 3 PASSED — wrong-key JWT rejected")


async def scenario_4_wrong_issuer(client: httpx.AsyncClient):
    banner("4 — Wrong issuer → 401")
    google_sub = f"google-sub-S4-{uuid.uuid4().hex[:8]}"
    token_id = await seed_google_token(google_sub)
    id_token = make_id_token(issuer="https://evil.com")
    envelope = make_pubsub_envelope(make_set_jwt(google_sub=google_sub))

    with _patch_jwks():
        r = await client.post(
            "/v2/webhooks/google/risc",
            headers={"Authorization": f"Bearer {id_token}"},
            json=envelope,
        )
    print(f"  HTTP {r.status_code}  detail: {r.text[:160]}")
    assert r.status_code == 401
    assert "issuer" in r.text.lower()
    assert await get_status(token_id) == "ACTIVE"
    print("  ✓ SCENARIO 4 PASSED — wrong issuer rejected")


async def scenario_5_wrong_audience(client: httpx.AsyncClient):
    banner("5 — Wrong audience → 401")
    google_sub = f"google-sub-S5-{uuid.uuid4().hex[:8]}"
    token_id = await seed_google_token(google_sub)
    id_token = make_id_token(audience="https://attacker.example.com")
    envelope = make_pubsub_envelope(make_set_jwt(google_sub=google_sub))

    with _patch_jwks():
        r = await client.post(
            "/v2/webhooks/google/risc",
            headers={"Authorization": f"Bearer {id_token}"},
            json=envelope,
        )
    print(f"  HTTP {r.status_code}  detail: {r.text[:160]}")
    assert r.status_code == 401
    assert "audience" in r.text.lower()
    assert await get_status(token_id) == "ACTIVE"
    print("  ✓ SCENARIO 5 PASSED — wrong audience rejected")


async def scenario_6_alg_none_attack(client: httpx.AsyncClient):
    banner("6 — alg=none / HS256 confusion attack → 401")
    google_sub = f"google-sub-S6-{uuid.uuid4().hex[:8]}"
    token_id = await seed_google_token(google_sub)
    # Manually craft an unsigned JWT (alg=none). PyJWT 2.x rejects this
    # by default unless explicitly allowed.
    header = b64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    payload = b64.urlsafe_b64encode(json.dumps({
        "iss": ISSUER, "aud": AUDIENCE,
        "iat": int(time.time()), "exp": int(time.time()) + 600,
    }).encode()).rstrip(b"=").decode()
    none_token = f"{header}.{payload}."

    envelope = make_pubsub_envelope(make_set_jwt(google_sub=google_sub))
    with _patch_jwks():
        r = await client.post(
            "/v2/webhooks/google/risc",
            headers={"Authorization": f"Bearer {none_token}"},
            json=envelope,
        )
    print(f"  HTTP {r.status_code}  detail: {r.text[:160]}")
    # Either 401 (algorithm rejected / signature mismatch) — never 200
    assert r.status_code == 401
    assert await get_status(token_id) == "ACTIVE"
    print("  ✓ SCENARIO 6 PASSED — alg=none attack defeated")


async def scenario_7_replay_dedupe(client: httpx.AsyncClient):
    banner("7 — Pub/Sub messageId dedupe → second call no-op")
    google_sub = f"google-sub-S7-{uuid.uuid4().hex[:8]}"
    token_id = await seed_google_token(google_sub)
    id_token = make_id_token()
    msg_id = "msg-" + uuid.uuid4().hex
    envelope = make_pubsub_envelope(make_set_jwt(google_sub=google_sub),
                                     message_id=msg_id)

    with _patch_jwks():
        r1 = await client.post(
            "/v2/webhooks/google/risc",
            headers={"Authorization": f"Bearer {id_token}"},
            json=envelope,
        )
        print(f"  first call:  HTTP {r1.status_code} body: {r1.json()}")
        assert r1.status_code == 200
        assert r1.json().get("revoked_tokens") == 1

        r2 = await client.post(
            "/v2/webhooks/google/risc",
            headers={"Authorization": f"Bearer {id_token}"},
            json=envelope,
        )
        print(f"  second call: HTTP {r2.status_code} body: {r2.json()}")
        assert r2.status_code == 200
        assert r2.json().get("duplicate") is True

    async with async_session() as s:
        n = (await s.execute(
            select(TenantAuditLog).where(
                TenantAuditLog.event_type == "oauth.revoked",
                TenantAuditLog.event_meta.op("@>")(
                    {"google_event_id": "", "provider": "google"}
                ) | TenantAuditLog.event_meta.op("@>")(
                    {"encrypted_token_id": token_id, "provider": "google"}
                ),
            )
        )).scalars().all()
    print(f"  audit rows for token: {len(n)}  (must be 1)")
    assert len(n) == 1
    print("  ✓ SCENARIO 7 PASSED — Pub/Sub dedupe by messageId works")


async def scenario_8_unmatched_event(client: httpx.AsyncClient):
    banner("8 — Event type NOT in revoke set → 200 handled=False, no revocation")
    google_sub = f"google-sub-S8-{uuid.uuid4().hex[:8]}"
    token_id = await seed_google_token(google_sub)
    set_jwt = make_set_jwt(
        google_sub=google_sub,
        event_types=["https://schemas.openid.net/secevent/risc/event-type/account-enabled"],
    )
    envelope = make_pubsub_envelope(set_jwt)
    id_token = make_id_token()

    with _patch_jwks():
        r = await client.post(
            "/v2/webhooks/google/risc",
            headers={"Authorization": f"Bearer {id_token}"},
            json=envelope,
        )
    print(f"  HTTP {r.status_code} body: {r.json()}")
    assert r.status_code == 200
    body = r.json()
    assert body.get("handled") is False
    assert "revoked_tokens" not in body or body.get("revoked_tokens") == 0
    assert await get_status(token_id) == "ACTIVE"
    print("  ✓ SCENARIO 8 PASSED — unmatched event ignored gracefully")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

async def main_runner():
    banner("Loop 2 — Google RISC Webhook (harsh suite)")
    async with main.app.router.lifespan_context(main.app):
        await seed_tenant()
        await cleanup()
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testclient", timeout=30,
        ) as c:
            results = []
            for name, fn in [
                ("1_valid_revoked",      scenario_1_valid_revoked),
                ("2_expired_401",        scenario_2_expired),
                ("3_wrong_key_401",      scenario_3_wrong_key),
                ("4_wrong_issuer_401",   scenario_4_wrong_issuer),
                ("5_wrong_audience_401", scenario_5_wrong_audience),
                ("6_alg_none_defended",  scenario_6_alg_none_attack),
                ("7_replay_dedupe",      scenario_7_replay_dedupe),
                ("8_unmatched_event",    scenario_8_unmatched_event),
            ]:
                try:
                    await fn(c)
                    results.append((name, True))
                except Exception:
                    results.append((name, False))
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
