"""
Phase 4.5 — Provider revocation webhooks (Layer 4 of Sovereign-Grade).

Slack first. Google follows in 4.5b.

Slack contract (per https://api.slack.com/authentication/verifying-requests-from-slack):

  X-Slack-Request-Timestamp: <unix_seconds>
  X-Slack-Signature:         v0=<hex_hmac_sha256>

  basestring = "v0:" + timestamp + ":" + raw_body
  expected   = "v0=" + hmac_sha256(SLACK_SIGNING_SECRET, basestring).hex()
  compare    = constant-time hex compare against the X-Slack-Signature header

  Replay defence: reject if |now - timestamp| > 300 seconds.

Idempotency: Slack retries up to 3× on non-200 responses. We dedupe on
`event_id` via Redis SETNX with a 24h TTL — second arrival is a no-op.

Threats this module defends — Gap-4 threat model rows 11, 16, 17.
"""
from __future__ import annotations

import base64 as _b64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, Header, HTTPException, Request
from jwt import PyJWKClient
from sqlalchemy import select, update

log = logging.getLogger("siyadah.webhooks")

router = APIRouter(prefix="/v2/webhooks", tags=["webhooks"])


# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

SLACK_SIG_TIMESTAMP_TOLERANCE = 300                    # 5 min — Slack docs
SLACK_EVENT_DEDUPE_TTL = 24 * 3600                     # 24h — Slack's retry window
SLACK_SIG_HEADER = "X-Slack-Signature"
SLACK_TS_HEADER = "X-Slack-Request-Timestamp"
SLACK_SIGNING_SECRET_ENV = "SLACK_SIGNING_SECRET"

# ── Google RISC / Pub/Sub push (Loop 2) ──
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = frozenset({
    "https://accounts.google.com", "accounts.google.com",
})
GOOGLE_AUDIENCE_ENV = "GOOGLE_PUBSUB_AUDIENCE"
GOOGLE_JWKS_LIFESPAN_SECONDS = 3600                    # 1h — Google rotates ~daily
GOOGLE_EVENT_DEDUPE_TTL = 24 * 3600

# Module-level JWKS client (cached + lazy). PyJWKClient handles the
# rotation: when an unknown `kid` is seen, it re-fetches the JWKS once.
_google_jwks_client: Optional[PyJWKClient] = None


def _get_google_jwks_client() -> PyJWKClient:
    """Lazy singleton. Fetches Google's JWKS only on first use, caches
    keys for GOOGLE_JWKS_LIFESPAN_SECONDS. Module-level for testability:
    tests monkey-patch this to inject a local key set."""
    global _google_jwks_client
    if _google_jwks_client is None:
        _google_jwks_client = PyJWKClient(
            GOOGLE_JWKS_URL,
            cache_keys=True,
            max_cached_keys=16,
            cache_jwk_set=True,
            lifespan=GOOGLE_JWKS_LIFESPAN_SECONDS,
        )
    return _google_jwks_client


# ═══════════════════════════════════════════════════════════════
# Signature verification (constant-time, replay-defended)
# ═══════════════════════════════════════════════════════════════

def _now_seconds() -> int:
    """Indirection so tests can monkey-patch the clock."""
    return int(time.time())


def _verify_slack_signature(
    *, raw_body: bytes, signature: str, timestamp: str,
) -> None:
    """Raise HTTPException on any failure path. Returns silently on success.

    Order of checks (cheapest → most expensive):
      1. Signing secret configured
      2. Timestamp parses + within ±5 min of now
      3. HMAC matches (constant-time)
    """
    secret = os.getenv(SLACK_SIGNING_SECRET_ENV, "").encode()
    if not secret:
        # 503 because this is OUR config error, not the caller's fault
        raise HTTPException(503, f"{SLACK_SIGNING_SECRET_ENV} not configured")
    if not signature or not signature.startswith("v0="):
        raise HTTPException(401, "missing or malformed X-Slack-Signature")
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        raise HTTPException(401, "invalid X-Slack-Request-Timestamp")
    drift = abs(_now_seconds() - ts_int)
    if drift > SLACK_SIG_TIMESTAMP_TOLERANCE:
        # Stale → likely replay attack OR clock skew. Either way, refuse.
        raise HTTPException(
            401,
            f"stale timestamp (drift={drift}s > {SLACK_SIG_TIMESTAMP_TOLERANCE}s)",
        )
    basestring = b"v0:" + timestamp.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret, basestring, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        raise HTTPException(401, "signature mismatch")


# ═══════════════════════════════════════════════════════════════
# Slack webhook
# ═══════════════════════════════════════════════════════════════

async def _get_redis():
    from mcp_sse import _redis
    if _redis is None:
        raise HTTPException(503, "redis not initialised")
    return _redis


async def _revoke_slack_team(team_id: str, *, event_id: str) -> int:
    """Mark every ACTIVE encrypted_token for this Slack team as REVOKED.

    A single team_id can be referenced by multiple tenants if they all
    installed the same Slack app — we revoke ALL of them, audit each,
    and return the count.

    Idempotent: if a row is already REVOKED we skip it (no double-audit).
    """
    from database import async_session
    from models import EncryptedToken, TenantAuditLog

    async with async_session() as s:
        rows = (await s.execute(
            select(EncryptedToken).where(
                EncryptedToken.provider == "slack",
                EncryptedToken.provider_account_id == team_id,
                EncryptedToken.status == "ACTIVE",
            )
        )).scalars().all()

        revoked_ids: list[str] = []
        for row in rows:
            await s.execute(
                update(EncryptedToken).where(EncryptedToken.id == row.id).values(
                    status="REVOKED",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            revoked_ids.append(row.id)
            s.add(TenantAuditLog(
                project_id=row.tenant_id,
                endpoint="POST /v2/webhooks/slack/events",
                http_status=200,
                event_type="oauth.revoked",
                event_meta={
                    "provider": "slack",
                    "encrypted_token_id": row.id,
                    "provider_account_id": team_id,
                    "revoked_via": "webhook",
                    "slack_event_id": event_id,
                },
            ))
        await s.commit()

    log.warning(
        "[slack-webhook] revoked %d encrypted_tokens for team_id=%s event_id=%s",
        len(revoked_ids), team_id, event_id,
    )

    # Phase 4.5 — Real-time SSE notification. The mcp_sse layer publishes
    # to per-tenant Redis channels; subscribers (BFF) push to the user UI.
    # Best-effort — failure here doesn't deny the revocation.
    try:
        await _broadcast_revocation(revoked_ids, team_id)
    except Exception as e:
        log.error("[slack-webhook] SSE broadcast failed (non-fatal): %s", e)

    return len(revoked_ids)


async def _broadcast_revocation(token_ids: list[str], team_id: str) -> None:
    """Publish a Redis pub/sub message per affected tenant. The BFF (or any
    SSE subscriber on the tenant's channel) gets near-real-time notification.

    Channel format: `siyadah:tenant:{tenant_id}:oauth-events`
    Payload: JSON {event, provider, account_id, encrypted_token_id, ts}
    """
    if not token_ids:
        return
    from database import async_session
    from models import EncryptedToken
    redis = await _get_redis()
    async with async_session() as s:
        rows = (await s.execute(
            select(EncryptedToken.id, EncryptedToken.tenant_id)
            .where(EncryptedToken.id.in_(token_ids))
        )).all()
    for token_id, tenant_id in rows:
        channel = f"siyadah:tenant:{tenant_id}:oauth-events"
        message = json.dumps({
            "event": "oauth.revoked",
            "provider": "slack",
            "provider_account_id": team_id,
            "encrypted_token_id": token_id,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        try:
            await redis.publish(channel, message)
        except Exception as e:
            log.warning("[slack-webhook] publish to %s failed: %s", channel, e)


@router.post("/slack/events")
async def slack_events_webhook(
    request: Request,
    x_slack_signature: str = Header("", alias=SLACK_SIG_HEADER),
    x_slack_request_timestamp: str = Header("", alias=SLACK_TS_HEADER),
):
    """Slack Events API entry point. PUBLIC route (no X-API-Key) —
    authenticates via HMAC signature only.

    Handles:
      • url_verification — initial setup challenge from Slack UI
      • event_callback / app_uninstalled — wipes tokens for that team
      • event_callback / tokens_revoked — same effect, different cause

    Rejects:
      • missing/malformed signature → 401
      • stale timestamp (replay) → 401
      • signature mismatch → 401
      • non-JSON body → 400
    """
    raw_body = await request.body()
    _verify_slack_signature(
        raw_body=raw_body,
        signature=x_slack_signature,
        timestamp=x_slack_request_timestamp,
    )

    # After signature passes, parse JSON. Pre-parse is intentionally
    # avoided — verifying THE EXACT bytes Slack signed, not a re-encoding.
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"invalid JSON: {e}") from e

    # ── URL verification handshake ─────────────────────────────────
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge", "")
        log.info("[slack-webhook] url_verification handshake")
        return {"challenge": challenge}

    # ── Event callback ─────────────────────────────────────────────
    if payload.get("type") != "event_callback":
        log.info("[slack-webhook] ignoring envelope type=%s",
                 payload.get("type"))
        return {"ok": True, "ignored": True}

    event = payload.get("event", {}) or {}
    event_type = event.get("type", "")
    team_id = payload.get("team_id", "") or ""
    event_id = payload.get("event_id", "") or ""

    # ── Idempotency: dedupe by event_id (Slack retries on non-200) ──
    if event_id:
        redis = await _get_redis()
        dedupe_key = f"slack:event_id:{event_id}"
        is_first = await redis.set(dedupe_key, "1",
                                    nx=True, ex=SLACK_EVENT_DEDUPE_TTL)
        if not is_first:
            log.info("[slack-webhook] duplicate event_id %s — skip", event_id)
            return {"ok": True, "duplicate": True}

    # ── app_uninstalled / tokens_revoked → revoke tokens ────────────
    if event_type in ("app_uninstalled", "tokens_revoked"):
        if not team_id:
            log.warning("[slack-webhook] %s with no team_id — skipping",
                        event_type)
            return {"ok": True, "no_team_id": True}
        revoked = await _revoke_slack_team(team_id, event_id=event_id)
        return {
            "ok": True,
            "event_type": event_type,
            "team_id": team_id,
            "revoked_tokens": revoked,
        }

    # Other event types — just ack so Slack stops retrying
    return {"ok": True, "event_type": event_type, "handled": False}


# ═══════════════════════════════════════════════════════════════
# Google RISC (Loop 2) — Pub/Sub push with JWT verification
# ═══════════════════════════════════════════════════════════════
#
# RISC = Cross-Account Protection. When a user revokes Siyadah's access
# from their Google account UI, Google publishes a Security Event Token
# (SET) to a Pub/Sub topic. Our push subscription delivers it here.
#
# Two layers of authentication:
#   1. Authorization: Bearer <ID_TOKEN>  ← JWT signed by Google's RSA
#      keys (rotated). Verify against Google's JWKS.
#   2. The body's `data` field IS the SET — itself a JWT, signed by
#      the Google service account that owns the topic. For Loop 2 we
#      verify the OUTER ID token (the security gate) and decode the
#      inner SET claims without re-verifying the signature, since the
#      OUTER auth already proves it came from Google. Future hardening
#      could verify the inner SET signature separately.
#
# Threats this defends — Gap-4 threat model rows 11, 16, 17 (same as
# Slack but for Google). Plus #10 — encryption downgrade — the JWT
# verification ENFORCES alg=RS256, so an attacker can't strip alg to
# "none" and bypass.

class GoogleVerifyError(Exception):
    """Top-level marker for any failure to verify a Google push request."""


def _verify_google_id_token(token: str, *, audience: str) -> dict:
    """Verify the outer Bearer token. Returns claims on success.
    Raises GoogleVerifyError (mapped to 401) on any failure path:
      • signature mismatch against Google's JWKS
      • expired
      • wrong issuer
      • wrong audience
      • missing required claims
      • alg ≠ RS256 (defends against alg=none / HS256 confusion attacks)
    """
    try:
        client = _get_google_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],                       # explicit allow-list
            audience=audience,
            issuer=list(GOOGLE_ISSUERS),
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise GoogleVerifyError(f"expired: {e}") from e
    except jwt.InvalidAudienceError as e:
        raise GoogleVerifyError(f"audience mismatch: {e}") from e
    except jwt.InvalidIssuerError as e:
        raise GoogleVerifyError(f"issuer mismatch: {e}") from e
    except jwt.InvalidSignatureError as e:
        raise GoogleVerifyError(f"signature mismatch: {e}") from e
    except jwt.MissingRequiredClaimError as e:
        raise GoogleVerifyError(f"missing claim: {e}") from e
    except jwt.InvalidAlgorithmError as e:
        raise GoogleVerifyError(f"algorithm rejected: {e}") from e
    except Exception as e:                              # PyJWT exception zoo
        raise GoogleVerifyError(f"{type(e).__name__}: {e}") from e
    return claims


async def _revoke_google_user(google_sub: str, *, event_id: str) -> int:
    """Mark every ACTIVE encrypted_token for this Google sub as REVOKED.

    `provider_account_id` on encrypted_tokens is the Google `sub` claim
    (stable per-Google-account-per-app identifier). One sub can be
    referenced by multiple Siyadah tenants — revoke all.
    """
    from database import async_session
    from models import EncryptedToken, TenantAuditLog

    async with async_session() as s:
        rows = (await s.execute(
            select(EncryptedToken).where(
                EncryptedToken.provider == "google",
                EncryptedToken.provider_account_id == google_sub,
                EncryptedToken.status == "ACTIVE",
            )
        )).scalars().all()

        revoked_ids: list[str] = []
        for row in rows:
            await s.execute(
                update(EncryptedToken).where(EncryptedToken.id == row.id).values(
                    status="REVOKED",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            revoked_ids.append(row.id)
            s.add(TenantAuditLog(
                project_id=row.tenant_id,
                endpoint="POST /v2/webhooks/google/risc",
                http_status=200,
                event_type="oauth.revoked",
                event_meta={
                    "provider": "google",
                    "encrypted_token_id": row.id,
                    "provider_account_id": google_sub,
                    "revoked_via": "webhook",
                    "google_event_id": event_id,
                },
            ))
        await s.commit()

    log.warning(
        "[google-risc] revoked %d encrypted_tokens for sub=%s event_id=%s",
        len(revoked_ids), google_sub[:12] + "…", event_id,
    )

    # SSE notification — same channel pattern as Slack
    if revoked_ids:
        try:
            redis = await _get_redis()
            from database import async_session as _s
            from models import EncryptedToken as _E
            async with _s() as s:
                pairs = (await s.execute(
                    select(_E.id, _E.tenant_id).where(_E.id.in_(revoked_ids))
                )).all()
            for token_id, tenant_id in pairs:
                channel = f"siyadah:tenant:{tenant_id}:oauth-events"
                payload = json.dumps({
                    "event": "oauth.revoked", "provider": "google",
                    "encrypted_token_id": token_id,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
                try:
                    await redis.publish(channel, payload)
                except Exception as e:
                    log.warning("[google-risc] publish failed: %s", e)
        except Exception as e:
            log.error("[google-risc] SSE broadcast failed (non-fatal): %s", e)

    return len(revoked_ids)


@router.post("/google/risc")
async def google_risc_webhook(
    request: Request,
    authorization: str = Header("", alias="Authorization"),
):
    """Google Pub/Sub push delivery for RISC events.

    Verifies the outer ID token in `Authorization: Bearer …`, then
    decodes the Pub/Sub envelope + the SET inside, and revokes tokens
    for any `tokens-revoked` / `account-disabled` / `sessions-revoked`
    event.
    """
    audience = os.getenv(GOOGLE_AUDIENCE_ENV, "").strip()
    if not audience:
        raise HTTPException(503, f"{GOOGLE_AUDIENCE_ENV} not configured")

    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing or malformed Authorization header")
    id_token = authorization.split(None, 1)[1].strip()

    try:
        outer_claims = _verify_google_id_token(id_token, audience=audience)
    except GoogleVerifyError as e:
        log.warning("[google-risc] id-token verification failed: %s", e)
        raise HTTPException(401, f"id token verification failed: {e}") from e

    # Body — Pub/Sub envelope
    try:
        envelope = await request.json()
    except Exception as e:
        raise HTTPException(400, f"invalid JSON: {e}") from e
    message = envelope.get("message") or {}
    if not message:
        raise HTTPException(400, "missing message in Pub/Sub envelope")

    pubsub_message_id = message.get("messageId", "")

    # Pub/Sub idempotency dedupe
    if pubsub_message_id:
        redis = await _get_redis()
        dedupe_key = f"google:risc:msg:{pubsub_message_id}"
        is_first = await redis.set(
            dedupe_key, "1", nx=True, ex=GOOGLE_EVENT_DEDUPE_TTL,
        )
        if not is_first:
            log.info("[google-risc] duplicate messageId %s — skip", pubsub_message_id)
            return {"ok": True, "duplicate": True}

    # Decode the SET (data is base64-encoded JWT, but for Loop 2 we
    # decode without re-verifying the signature — the outer ID token
    # already proves Google's authorship).
    raw_data = message.get("data", "")
    if not raw_data:
        return {"ok": True, "no_data": True}
    try:
        set_jwt = _b64.b64decode(raw_data).decode("utf-8")
        # Decode without verification — outer already verified
        set_claims = jwt.decode(
            set_jwt, options={"verify_signature": False, "verify_exp": False},
        )
    except Exception as e:
        log.warning("[google-risc] couldn't decode SET: %s", e)
        return {"ok": True, "decode_error": True}

    events = set_claims.get("events") or {}
    google_sub = set_claims.get("sub", "")
    set_jti = set_claims.get("jti", pubsub_message_id)

    revoke_event_types = {
        "https://schemas.openid.net/secevent/risc/event-type/tokens-revoked",
        "https://schemas.openid.net/secevent/risc/event-type/sessions-revoked",
        "https://schemas.openid.net/secevent/risc/event-type/account-disabled",
        "https://schemas.openid.net/secevent/risc/event-type/account-purged",
    }
    matched_events = [e for e in events.keys() if e in revoke_event_types]

    if not matched_events or not google_sub:
        return {
            "ok": True,
            "events_seen": list(events.keys()),
            "matched": matched_events,
            "handled": False,
        }

    revoked = await _revoke_google_user(google_sub, event_id=set_jti)
    return {
        "ok": True,
        "matched_events": matched_events,
        "google_sub_prefix": google_sub[:8] + "…",
        "revoked_tokens": revoked,
    }
