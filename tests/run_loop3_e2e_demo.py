"""
Loop 3 — End-to-End Lifecycle Test

Single token, one tenant, all phases chained:

  ┌─────────────────────────────────────────────────────────────────┐
  │  Phase 4.1  POST /v2/oauth/slack/initiate                       │
  │             saga = INITIATED  +  audit oauth.initiated          │
  │                                                                 │
  │  Phase 4.2  GET  /v2/oauth/slack/callback                       │
  │             saga = TOKEN_OBTAINED  +  encrypted_tokens row      │
  │                                       audit oauth.token_exchanged│
  │                                                                 │
  │  Phase 4.3  AP connection created                               │
  │             saga = COMPLETED  +  audit oauth.completed          │
  │                                                                 │
  │  Phase 4.4  refresh_due_tokens()  (Loop1 hardening)             │
  │             ciphertext rotated, refresh_count: 0 → 1            │
  │             audit token.refreshed                               │
  │                                                                 │
  │  Phase 4.4  AP failure path → ap_sync_pending = true            │
  │             AP recovery cycle → ap_sync_pending = false         │
  │             provider NOT called twice                           │
  │             audit oauth.ap_resynced                             │
  │                                                                 │
  │  Phase 4.5  POST /v2/webhooks/slack/events  (app_uninstalled)   │
  │             token = REVOKED  +  audit oauth.revoked             │
  └─────────────────────────────────────────────────────────────────┘

Result is verified at every step. The full audit chain — every
oauth.* event_type — is dumped at the end as forensic evidence.
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
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Env BEFORE main is imported ──
os.environ.setdefault(
    "SIYADAH_OAUTH_STATE_KEY",
    base64.urlsafe_b64encode(b"\x42" * 32).decode().rstrip("="),
)
os.environ.setdefault(
    "SIYADAH_OAUTH_MK",
    base64.urlsafe_b64encode(b"\x07" * 32).decode().rstrip("="),
)
os.environ.setdefault("SLACK_CLIENT_ID", "DEMO_E2E")
os.environ.setdefault("SLACK_CLIENT_SECRET", "DEMO_E2E")
os.environ.setdefault(
    "SLACK_REDIRECT_URI",
    "https://siyadah-orchestrator-production.up.railway.app/v2/oauth/slack/callback",
)
os.environ["SLACK_SIGNING_SECRET"] = "loop3-e2e-secret-" + "x" * 32
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

import httpx                          # noqa: E402
from sqlalchemy import select, text   # noqa: E402

import main                           # noqa: E402
import oauth_routes                   # noqa: E402
import oauth_webhooks                 # noqa: E402
from database import async_session    # noqa: E402
from models import (                  # noqa: E402
    EncryptedToken, OAuthSaga, Project, TenantApiKey, TenantAuditLog,
)
from oauth_providers import ParsedTokenResponse  # noqa: E402


TENANT = "loop3-e2e"
RAW_KEY = "loop3-e2e-key-" + "z" * 40
HEADERS = {"X-API-Key": RAW_KEY, "X-Siyadah-Tenant": TENANT}
TEAM_ID = "T_LOOP3_E2E"

INITIAL_ACCESS = "xoxb-INITIAL-loop3-e2e-access"
INITIAL_REFRESH = "xoxe-INITIAL-loop3-e2e-refresh"
ROTATED_ACCESS = "xoxb-ROTATED-loop3-e2e-access"
ROTATED_REFRESH = "xoxe-ROTATED-loop3-e2e-refresh"


def banner(label: str):
    print(f"\n{'═' * 76}\n  {label}\n{'═' * 76}")


def step(n: str, label: str):
    print(f"\n────── STEP {n} ─ {label} ──────")


@contextmanager
def patch(target_module, name, fn):
    saved = getattr(target_module, name)
    setattr(target_module, name, fn)
    try:
        yield
    finally:
        setattr(target_module, name, saved)


# ═══════════════════════════════════════════════════════════════
# Setup
# ═══════════════════════════════════════════════════════════════

async def seed_tenant():
    key_hash = hashlib.sha256(RAW_KEY.encode()).hexdigest()
    async with async_session() as s:
        existing = (await s.execute(
            select(Project).where(Project.project_id == TENANT)
        )).scalar_one_or_none()
        if not existing:
            s.add(Project(project_id=TENANT, name="Loop3 E2E"))
            s.add(TenantApiKey(
                project_id=TENANT, key_hash=key_hash,
                label="loop3-e2e", scopes=["read", "write"],
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
                    label="loop3-e2e", scopes=["read", "write"],
                ))
                await s.commit()
            print(f"  tenant '{TENANT}' reused")


async def cleanup():
    async with async_session() as s:
        await s.execute(
            text("DELETE FROM encrypted_tokens WHERE tenant_id = :t"),
            {"t": TENANT},
        )
        # Clean prior sagas too so audit ordering is unambiguous
        await s.execute(
            text("DELETE FROM oauth_sagas WHERE tenant_id = :t"),
            {"t": TENANT},
        )
        await s.execute(
            text("DELETE FROM tenant_audit_log WHERE project_id = :t "
                 "AND event_type LIKE 'oauth.%' OR event_type LIKE 'token.%'"),
            {"t": TENANT},
        )
        await s.commit()


def slack_sign_body(body: bytes, ts: int) -> str:
    base = b"v0:" + str(ts).encode() + b":" + body
    return "v0=" + hmac.new(
        os.environ["SLACK_SIGNING_SECRET"].encode(),
        base, hashlib.sha256,
    ).hexdigest()


# ═══════════════════════════════════════════════════════════════
# E2E flow
# ═══════════════════════════════════════════════════════════════

async def main_runner():
    banner("Loop 3 — End-to-End Lifecycle (initiate → refresh → revoke)")
    async with main.app.router.lifespan_context(main.app):
        await seed_tenant()
        await cleanup()

        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testclient", timeout=60,
        ) as client:

            # ─── STEP 1 — Phase 4.1 initiate ────────────────────────
            step("1", "POST /v2/oauth/slack/initiate")
            r = await client.post(
                "/v2/oauth/slack/initiate", headers=HEADERS,
                json={"return_path": "/installed"},
            )
            assert r.status_code == 200, r.text
            j = r.json()
            saga_id = j["saga_id"]
            state = parse_qs(urlparse(j["authorization_url"]).query)["state"][0]
            print(f"  saga_id      = {saga_id}")
            print(f"  scopes       = {j['scopes']}")
            print(f"  state (hd)   = {state[:48]}…")

            async with async_session() as s:
                saga = (await s.execute(
                    select(OAuthSaga).where(OAuthSaga.id == saga_id)
                )).scalar_one()
            assert saga.status == "INITIATED"
            print(f"  ✓ saga.status = INITIATED")

            # ─── STEP 2 — Phase 4.2 callback (mock provider + AP) ──
            step("2", "GET /v2/oauth/slack/callback (mock provider + AP)")

            async def mock_exchange(cfg, code, *, verifier=None):
                return ParsedTokenResponse(
                    access_token=INITIAL_ACCESS,
                    refresh_token=INITIAL_REFRESH,
                    expires_in=43200,
                    scopes=["chat:write", "channels:read", "users:read"],
                    provider_account_id=TEAM_ID,
                )

            captured_ap_calls: list[dict] = []
            async def mock_ap_create(cfg, *, saga_id, tenant_id, access_token, parsed):
                captured_ap_calls.append({
                    "step": "create",
                    "access_token": access_token,
                    "saga_id": saga_id,
                })
                return {
                    "id": f"ap_e2e_{uuid.uuid4().hex[:8]}",
                    "externalId": f"siyadah-{saga_id[:16]}",
                    "displayName": f"Siyadah Slack ({tenant_id})",
                    "type": "CUSTOM_AUTH",
                }

            with patch(oauth_routes, "_exchange_code", mock_exchange), \
                 patch(oauth_routes, "_create_ap_connection", mock_ap_create):
                r2 = await client.get(
                    f"/v2/oauth/slack/callback?code=demo_code&state={state}",
                )
            assert r2.status_code == 200, r2.text
            cb = r2.json()
            encrypted_token_id = cb["encrypted_token_id"]
            print(f"  HTTP {r2.status_code}  saga.status = {cb['status']}")
            print(f"  encrypted_token_id = {encrypted_token_id}")
            print(f"  AP create calls    = {len(captured_ap_calls)}")
            assert cb["status"] == "COMPLETED"
            assert len(captured_ap_calls) == 1
            assert captured_ap_calls[0]["access_token"] == INITIAL_ACCESS

            async with async_session() as s:
                saga = (await s.execute(
                    select(OAuthSaga).where(OAuthSaga.id == saga_id)
                )).scalar_one()
                tok = (await s.execute(
                    select(EncryptedToken).where(EncryptedToken.id == encrypted_token_id)
                )).scalar_one()
            assert saga.status == "COMPLETED"
            assert saga.ap_connection_external_id == f"siyadah-{saga_id[:16]}"
            assert tok.status == "ACTIVE"
            assert tok.refresh_count == 0
            assert tok.provider_account_id == TEAM_ID
            print(f"  ✓ saga COMPLETED, encrypted_tokens row ACTIVE, "
                  f"AP externalId stamped")

            # ─── STEP 3 — Phase 4.4 force-due, run refresh cycle ───
            step("3", "Force refresh_at into the past + run refresh cycle "
                      "(provider OK, AP OK)")
            past = datetime.now(timezone.utc) - timedelta(seconds=60)
            async with async_session() as s:
                await s.execute(
                    text("UPDATE encrypted_tokens SET refresh_at = :p "
                         "WHERE id = :id"),
                    {"p": past, "id": encrypted_token_id},
                )
                await s.commit()

            async def mock_refresh(cfg, refresh_token):
                # Verify the worker decrypted and forwarded the right value
                assert refresh_token == INITIAL_REFRESH, \
                    f"unexpected refresh_token: {refresh_token[:20]}…"
                return ParsedTokenResponse(
                    access_token=ROTATED_ACCESS,
                    refresh_token=ROTATED_REFRESH,
                    expires_in=43200,
                    scopes=["chat:write", "channels:read"],
                    provider_account_id=TEAM_ID,
                )

            ap_update_count = 0
            async def mock_ap_update(cfg, **kw):
                nonlocal ap_update_count
                ap_update_count += 1
                assert kw["access_token"] == ROTATED_ACCESS

            with patch(oauth_routes, "_refresh_with_provider", mock_refresh), \
                 patch(oauth_routes, "_update_ap_connection", mock_ap_update):
                summary = await oauth_routes.refresh_due_tokens()
            print(f"  cycle summary: {summary}")
            print(f"  AP update calls: {ap_update_count}")
            assert summary["claimed"] == 1
            assert summary["actions"].get("rotated", 0) == 1

            async with async_session() as s:
                tok = (await s.execute(
                    select(EncryptedToken).where(EncryptedToken.id == encrypted_token_id)
                )).scalar_one()
            assert tok.refresh_count == 1
            assert tok.ap_sync_pending is False
            assert tok.status == "ACTIVE"
            print(f"  ✓ refresh_count: 0 → 1   ap_sync_pending = False")

            # ─── STEP 4 — Phase 4.4 hardening — AP failure → recovery ──
            step("4", "Force refresh again, simulate AP failure, "
                      "then verify Q4 recovery without provider re-call")
            past = datetime.now(timezone.utc) - timedelta(seconds=60)
            async with async_session() as s:
                await s.execute(
                    text("UPDATE encrypted_tokens SET refresh_at = :p "
                         "WHERE id = :id"),
                    {"p": past, "id": encrypted_token_id},
                )
                await s.commit()

            async def mock_refresh_2(cfg, refresh_token):
                # The previous rotation produced ROTATED_REFRESH; that's
                # what the worker should now decrypt and send.
                assert refresh_token == ROTATED_REFRESH
                return ParsedTokenResponse(
                    access_token=ROTATED_ACCESS + "-cycle2",
                    refresh_token=ROTATED_REFRESH + "-cycle2",
                    expires_in=43200, scopes=["chat:write"],
                    provider_account_id=TEAM_ID,
                )

            async def mock_ap_failing(cfg, **kw):
                raise RuntimeError("AP unreachable: 503 (simulated)")

            with patch(oauth_routes, "_refresh_with_provider", mock_refresh_2), \
                 patch(oauth_routes, "_update_ap_connection", mock_ap_failing):
                s_a = await oauth_routes.refresh_due_tokens()
            print(f"  cycle A (AP down): {s_a['actions']}")
            assert s_a["actions"].get("rotated_ap_pending") == 1

            async with async_session() as s:
                tok = (await s.execute(
                    select(EncryptedToken).where(EncryptedToken.id == encrypted_token_id)
                )).scalar_one()
            assert tok.ap_sync_pending is True
            assert tok.refresh_count == 2
            print(f"  ✓ ap_sync_pending = True, refresh_count: 1 → 2")

            # Cycle B: AP healed, provider must NOT be called
            provider_called = 0
            async def mock_refresh_panic(cfg, refresh_token):
                nonlocal provider_called
                provider_called += 1
                raise AssertionError("provider should NOT be called on AP-only retry")
            ap_recovery_calls: list[str] = []
            async def mock_ap_healthy(cfg, **kw):
                ap_recovery_calls.append(kw["access_token"])

            with patch(oauth_routes, "_refresh_with_provider", mock_refresh_panic), \
                 patch(oauth_routes, "_update_ap_connection", mock_ap_healthy):
                s_b = await oauth_routes.refresh_due_tokens()
            print(f"  cycle B (AP healed): {s_b['actions']}")
            print(f"  provider_called = {provider_called}")
            print(f"  ap_recovery_calls = {ap_recovery_calls}")
            assert s_b["actions"].get("ap_resynced") == 1
            assert provider_called == 0, "provider was called during AP-only retry"
            assert ap_recovery_calls == [ROTATED_ACCESS + "-cycle2"]

            async with async_session() as s:
                tok = (await s.execute(
                    select(EncryptedToken).where(EncryptedToken.id == encrypted_token_id)
                )).scalar_one()
            assert tok.ap_sync_pending is False
            assert tok.refresh_count == 2, "refresh_count incremented on AP-only retry"
            print(f"  ✓ ap_sync_pending = False, refresh_count stays at 2")

            # ─── STEP 5 — Phase 4.5 manual revoke via Slack webhook ──
            step("5", "POST /v2/webhooks/slack/events (app_uninstalled)")
            payload = {
                "type": "event_callback",
                "event_id": f"Ev_E2E_{uuid.uuid4().hex[:10]}",
                "team_id": TEAM_ID,
                "event": {"type": "app_uninstalled"},
            }
            body = json.dumps(payload, separators=(",", ":")).encode()
            ts = int(time.time())
            sig = slack_sign_body(body, ts)
            r5 = await client.post(
                "/v2/webhooks/slack/events",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Slack-Signature": sig,
                    "X-Slack-Request-Timestamp": str(ts),
                },
            )
            print(f"  HTTP {r5.status_code}  body: {r5.json()}")
            assert r5.status_code == 200
            assert r5.json().get("revoked_tokens") == 1

            async with async_session() as s:
                tok = (await s.execute(
                    select(EncryptedToken).where(EncryptedToken.id == encrypted_token_id)
                )).scalar_one()
            assert tok.status == "REVOKED"
            print(f"  ✓ encrypted_tokens.status = REVOKED")

            # ─── STEP 6 — Forensic audit trail dump ──
            step("6", "Forensic audit trail — full lifecycle in oauth.* events")
            async with async_session() as s:
                rows = (await s.execute(
                    select(TenantAuditLog).where(
                        TenantAuditLog.project_id == TENANT,
                        TenantAuditLog.event_type.like("oauth.%")
                        | TenantAuditLog.event_type.like("token.%"),
                    ).order_by(TenantAuditLog.occurred_at.asc())
                )).scalars().all()
            print(f"\n  Total audit rows for {TENANT}: {len(rows)}")
            print(f"  {'occurred_at':<32s} {'event_type':<28s} meta_summary")
            print(f"  {'-' * 32} {'-' * 28} {'-' * 30}")
            event_types_seen = []
            for r in rows:
                t = r.occurred_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
                meta = r.event_meta or {}
                summary = ""
                if "saga_id" in meta:
                    summary += f"saga={meta['saga_id'][:8]}…"
                if "encrypted_token_id" in meta:
                    summary += f" tok={meta['encrypted_token_id'][:8]}…"
                if "refresh_count" in meta:
                    summary += f" rc={meta['refresh_count']}"
                if "recovered_via" in meta:
                    summary += f" via={meta['recovered_via']}"
                if "revoked_via" in meta:
                    summary += f" via={meta['revoked_via']}"
                print(f"  {t:<32s} {r.event_type:<28s} {summary}")
                event_types_seen.append(r.event_type)

            # Verify every expected milestone exists in the trail
            expected = {
                "oauth.initiated",
                "oauth.token_exchanged",
                "oauth.completed",
                "token.refreshed",            # 2× expected (steps 3 + 4)
                "oauth.ap_resynced",
                "oauth.revoked",
            }
            missing = expected - set(event_types_seen)
            assert not missing, f"audit trail missing: {missing}"
            # token.refreshed should appear twice
            assert event_types_seen.count("token.refreshed") == 2, \
                f"expected 2 refresh events, got {event_types_seen.count('token.refreshed')}"
            print(f"\n  ✓ Every milestone present in audit trail.")
            print(f"    expected: {sorted(expected)}")
            print(f"    found:    {sorted(set(event_types_seen))}")

        await cleanup()

    banner("FINAL VERDICT")
    print("  ✓ Phase 4.1  initiate")
    print("  ✓ Phase 4.2  callback + envelope encryption")
    print("  ✓ Phase 4.3  AP linker")
    print("  ✓ Phase 4.4  refresh worker (rotated 0→1→2)")
    print("  ✓ Phase 4.4  Q4 hardening (AP failure → recovery WITHOUT provider re-call)")
    print("  ✓ Phase 4.5  Slack revocation webhook")
    print("\n  E2E lifecycle PASSED — full audit trail intact.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_runner()))
