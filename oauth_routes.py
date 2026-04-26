"""
OAuth route layer — Phase 4.1 + 4.2.

Phase 4.1 — `/v2/oauth/{provider}/initiate`
Phase 4.2 — `/v2/oauth/{provider}/callback`

  initiate flow:
    1. Generate state token (HMAC) + PKCE pair via siyadah_oauth_state.
    2. Insert `oauth_sagas` row with status=INITIATED.
    3. Register nonce in Redis (Layer-5 cross-system idempotency).
    4. Write `oauth.initiated` event to tenant_audit_log.
    5. Return the provider's authorization URL.

  callback flow:
    1. Verify state HMAC + extract tenant_id.
    2. Look up saga by nonce; verify INITIATED status.
    3. Atomic Redis nonce consume (single-use).
    4. Exchange code → tokens (provider-specific).
    5. Envelope-encrypt + persist + transition saga in one transaction.
    6. Audit oauth.token_exchanged.

  Compensating action: any post-saga failure marks the saga FAILED with
  failure_step set, AND writes oauth.failed audit row. Never leave a saga
  stranded in INITIATED.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update

from oauth_providers import (
    ParsedTokenResponse,
    ProviderConfig,
    TokenExchangeError,
    UnknownProviderError,
    get_provider,
)
from siyadah_crypto import CryptoProvider
from siyadah_oauth_state import (
    NonceNotFoundError,
    NonceReplayError,
    NonceStore,
    StateError,
    StateProvider,
    gen_pkce,
)

log = logging.getLogger("siyadah.oauth")
router = APIRouter(prefix="/v2/oauth", tags=["oauth"])


# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

STATE_TTL_SECONDS = 300                                  # 5 min — must match StateProvider default
PLACEHOLDER_CLIENT_ID = "<SLACK_CLIENT_ID_NOT_SET>"
PLACEHOLDER_REDIRECT = "<SLACK_REDIRECT_URI_NOT_SET>"


# ═══════════════════════════════════════════════════════════════
# Pydantic models
# ═══════════════════════════════════════════════════════════════

class OAuthInitiateBody(BaseModel):
    return_path: str = Field(default="/", max_length=512)
    scopes: Optional[list[str]] = Field(default=None)


class OAuthInitiateResponse(BaseModel):
    authorization_url: str
    saga_id: str
    expires_at: str
    provider: str
    scopes: list[str]


class OAuthCallbackResponse(BaseModel):
    saga_id: str
    status: str
    provider: str
    encrypted_token_id: Optional[str] = None
    return_path: Optional[str] = None
    failure_step: Optional[str] = None
    failure_reason: Optional[str] = None


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _build_authorize_url(
    cfg: ProviderConfig,
    *,
    state_token: str,
    pkce_challenge: str,
    scopes: list[str],
) -> str:
    client_id = cfg.client_id() or PLACEHOLDER_CLIENT_ID
    redirect_uri = cfg.redirect_uri() or PLACEHOLDER_REDIRECT
    if client_id == PLACEHOLDER_CLIENT_ID:
        log.warning(
            "OAuth %s: %s not set — returning placeholder URL", cfg.name, cfg.client_id_env,
        )
    params = {
        "client_id": client_id,
        "scope": cfg.scope_separator.join(scopes),
        "state": state_token,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }
    if cfg.uses_pkce:
        params["code_challenge"] = pkce_challenge
        params["code_challenge_method"] = "S256"
    params.update(cfg.extra_authorize_params)
    return f"{cfg.authorize_url}?{urlencode(params)}"


async def _get_redis():
    from mcp_sse import _redis
    if _redis is None:
        raise HTTPException(503, "redis not initialised")
    return _redis


def _get_crypto() -> CryptoProvider:
    """Indirection so tests can swap a faulty crypto for failure-mode demos."""
    return CryptoProvider.from_env()


async def _exchange_code(
    cfg: ProviderConfig, code: str, *, verifier: Optional[str] = None,
) -> ParsedTokenResponse:
    """POST to provider's token endpoint, parse response. Module-level so
    tests can monkey-patch."""
    if not cfg.token_url:
        raise TokenExchangeError(cfg.name, "token_url_not_configured")
    data = {
        "client_id":     os.getenv(cfg.client_id_env, ""),
        "client_secret": os.getenv(cfg.client_secret_env, ""),
        "code":          code,
        "redirect_uri":  os.getenv(cfg.redirect_uri_env, ""),
    }
    if cfg.uses_pkce and verifier:
        data["code_verifier"] = verifier
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(cfg.token_url, data=data)
        try:
            j = r.json()
        except Exception as e:
            raise TokenExchangeError(
                cfg.name, "non_json_response", f"status={r.status_code}",
            ) from e
    if cfg.parse_response is None:
        raise TokenExchangeError(cfg.name, "no_parser_configured")
    return cfg.parse_response(j)


async def _mark_saga_failed(saga_id: str, *, step: str, reason: str) -> None:
    """Idempotent saga FAILED transition + oauth.failed audit row."""
    from database import async_session
    from models import OAuthSaga, TenantAuditLog
    truncated = (reason or "")[:500]
    async with async_session() as s:
        saga = (await s.execute(
            select(OAuthSaga).where(OAuthSaga.id == saga_id)
        )).scalar_one_or_none()
        if saga is None:
            log.warning("[oauth.callback] saga %s vanished during failure handling", saga_id)
            return
        if saga.status != "FAILED":
            await s.execute(
                update(OAuthSaga).where(OAuthSaga.id == saga_id).values(
                    status="FAILED",
                    failure_step=step,
                    failure_reason=truncated,
                    completed_at=datetime.now(timezone.utc),
                )
            )
        s.add(TenantAuditLog(
            project_id=saga.tenant_id,
            endpoint=f"GET /v2/oauth/{saga.provider}/callback",
            http_status=400,
            event_type="oauth.failed",
            event_meta={
                "provider": saga.provider,
                "saga_id": saga_id,
                "failure_step": step,
                "failure_reason": truncated,
            },
        ))
        await s.commit()


async def _persist_tokens(
    *,
    saga_id: str,
    tenant_id: str,
    provider: str,
    parsed: ParsedTokenResponse,
    crypto: CryptoProvider,
) -> str:
    """Envelope-encrypt + persist + transition saga atomically.

    On any error inside this function NO encrypted_tokens row exists
    (rollback) — caller marks saga FAILED via _mark_saga_failed.
    """
    from database import async_session
    from models import EncryptedToken, OAuthSaga

    # Pre-generate the row id so AAD is bound to the encrypted_tokens row
    # itself, not the saga. The refresh worker only sees the row, not the
    # saga, so AAD must be derivable from row state alone.
    import uuid
    new_token_id = str(uuid.uuid4())
    row_aad = f"{tenant_id}|{provider}|{new_token_id}".encode()
    dek = crypto.gen_dek()
    try:
        wrapped = crypto.wrap_dek(dek, aad=row_aad)
        sealed_access = crypto.encrypt_with_dek(
            parsed.access_token.encode("utf-8"), dek,
            aad=row_aad + b"|access",
        )
        sealed_refresh = None
        if parsed.refresh_token:
            sealed_refresh = crypto.encrypt_with_dek(
                parsed.refresh_token.encode("utf-8"), dek,
                aad=row_aad + b"|refresh",
            )
    finally:
        del dek

    expires_at = None
    refresh_at = None
    if parsed.expires_in:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=parsed.expires_in)
        refresh_at = expires_at - timedelta(minutes=5)

    async with async_session() as s:
        # Compensating revoke of any existing ACTIVE row (so the unique
        # constraint permits the new ACTIVE insert).
        if parsed.provider_account_id:
            await s.execute(
                update(EncryptedToken)
                .where(
                    EncryptedToken.tenant_id == tenant_id,
                    EncryptedToken.provider == provider,
                    EncryptedToken.provider_account_id == parsed.provider_account_id,
                    EncryptedToken.status == "ACTIVE",
                )
                .values(status="REVOKED", updated_at=datetime.now(timezone.utc))
            )

        token_row = EncryptedToken(
            id=new_token_id,                         # pre-generated for AAD binding
            tenant_id=tenant_id,
            provider=provider,
            provider_account_id=parsed.provider_account_id,
            encrypted_access_token=sealed_access.ciphertext,
            iv_access=sealed_access.iv,
            encrypted_refresh_token=sealed_refresh.ciphertext if sealed_refresh else None,
            iv_refresh=sealed_refresh.iv if sealed_refresh else None,
            wrapped_dek=wrapped.ciphertext,
            iv_dek=wrapped.iv,
            encryption_version=wrapped.version,
            scopes=list(parsed.scopes),
            expires_at=expires_at,
            refresh_at=refresh_at,
            status="ACTIVE",
        )
        s.add(token_row)
        await s.flush()

        await s.execute(
            update(OAuthSaga)
            .where(OAuthSaga.id == saga_id)
            .values(
                status="TOKEN_OBTAINED",
                encrypted_token_id=token_row.id,
            )
        )
        await s.commit()
        return token_row.id


def _decode_state_unsafe_for_attribution(state: str) -> Optional[dict]:
    """Decode payload WITHOUT verifying HMAC — used only to extract tid
    so we can call verify_state with the right expected tenant. Never
    used for auth decisions; verify_state is the gate."""
    import base64
    import json
    if "." not in state:
        return None
    try:
        payload_b64 = state.split(".", 1)[0]
        pad = "=" * ((4 - len(payload_b64) % 4) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + pad)
        d = json.loads(payload_bytes)
        return d if isinstance(d, dict) and "tid" in d and "n" in d else None
    except Exception:
        return None


async def _find_saga_by_nonce(nonce: str) -> Optional[str]:
    from database import async_session
    from models import OAuthSaga
    async with async_session() as s:
        sid = (await s.execute(
            select(OAuthSaga.id).where(OAuthSaga.state_nonce == nonce)
        )).scalar_one_or_none()
    return sid


# ═══════════════════════════════════════════════════════════════
# Phase 4.3 — AP connection linker + L5 compensating rollback
# ═══════════════════════════════════════════════════════════════

async def _create_ap_connection(
    cfg: ProviderConfig,
    *,
    saga_id: str,
    tenant_id: str,
    access_token: str,
    parsed: ParsedTokenResponse,
) -> dict:
    """POST /v1/app-connections to Activepieces.

    Returns the AP connection record dict on success. Raises on any
    transport error or non-2xx status. Indirection lives at module
    level so tests can monkey-patch a faulty version that simulates
    AP being down.

    `external_id` is deterministic and traceable: derived from the
    saga so an operator can join saga → AP connection without auxiliary
    state.
    """
    if cfg.ap_value_builder is None:
        raise RuntimeError(
            f"provider {cfg.name} has no ap_value_builder — Phase 4.3 disabled"
        )
    ap_base = os.getenv("AP_BASE_URL", "").rstrip("/")
    ap_pid = os.getenv("AP_PROJECT_ID", "")
    if not ap_base or not ap_pid:
        raise RuntimeError("AP_BASE_URL or AP_PROJECT_ID not set")

    # Auth as the orchestrator (operator-level). Uses the cached engine
    # token if present, else signs in fresh.
    from main import _engine
    if _engine is None or not _engine.token:
        raise RuntimeError("AP engine not authenticated")

    external_id = f"siyadah-{saga_id[:16]}"
    display_name = f"Siyadah {cfg.name.title()} ({tenant_id})"
    value_obj = cfg.ap_value_builder(access_token, parsed)

    payload = {
        "projectId": ap_pid,
        "externalId": external_id,
        "displayName": display_name,
        "pieceName": f"@activepieces/piece-{cfg.name}",
        "type": cfg.ap_connection_type,
        "value": value_obj,
    }

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{ap_base}/api/v1/app-connections",
            headers={"Authorization": f"Bearer {_engine.token}"},
            json=payload,
        )
        if r.status_code >= 400:
            # Capture body for forensics; keep it short for the saga column.
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text[:300]}
            raise RuntimeError(
                f"AP create-connection failed: {r.status_code} {body}"
            )
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"AP returned non-JSON: {e}")
    return data


async def _compensate_ap_failure(
    *,
    saga_id: str,
    tenant_id: str,
    encrypted_token_id: str,
    cfg_name: str,
    error_reason: str,
) -> None:
    """L5 compensating rollback when the AP create-connection step fails.

    Effects (all in ONE transaction so partial-rollback is impossible):
      1. DELETE encrypted_tokens row              ← Wipe the Vault entry
      2. UPDATE oauth_sagas: status='COMPENSATED', failure_step='ap_connection'
      3. INSERT tenant_audit_log with event_type='oauth.saga_compensated'

    The FK on oauth_sagas.encrypted_token_id is `ON DELETE SET NULL`,
    so the saga's pointer auto-clears when we delete the token row.
    """
    from database import async_session
    from models import EncryptedToken, OAuthSaga, TenantAuditLog
    truncated = (error_reason or "")[:500]
    async with async_session() as s:
        await s.execute(
            delete(EncryptedToken).where(EncryptedToken.id == encrypted_token_id)
        )
        await s.execute(
            update(OAuthSaga).where(OAuthSaga.id == saga_id).values(
                status="COMPENSATED",
                failure_step="ap_connection",
                failure_reason=truncated,
                completed_at=datetime.now(timezone.utc),
                # encrypted_token_id will become NULL via the FK ON DELETE SET NULL,
                # but we also clear it explicitly so the row reads sensibly mid-tx.
                encrypted_token_id=None,
            )
        )
        s.add(TenantAuditLog(
            project_id=tenant_id,
            endpoint=f"GET /v2/oauth/{cfg_name}/callback",
            http_status=503,
            event_type="oauth.saga_compensated",
            event_meta={
                "provider": cfg_name,
                "saga_id": saga_id,
                "compensation_step": "ap_connection",
                "error_reason": truncated,
                "wiped_encrypted_token_id": encrypted_token_id,
            },
        ))
        await s.commit()
    log.warning(
        "[oauth.compensate] saga=%s wiped encrypted_token=%s — reason=%s",
        saga_id, encrypted_token_id, truncated[:120],
    )


# ═══════════════════════════════════════════════════════════════
# Phase 4.4 — Refresh Worker (eternal pulse)
# ═══════════════════════════════════════════════════════════════

REFRESH_LOCK_TTL = 300                               # 5min — bounds longest reasonable refresh (Q1 fix)
REFRESH_LEASE_TTL_SECONDS = 300                      # Postgres-side lease (Q1 cross-replica)
REFRESH_DEFAULT_INTERVAL = int(os.getenv("OAUTH_REFRESH_INTERVAL_SECONDS", "60"))
REFRESH_BATCH_LIMIT = int(os.getenv("OAUTH_REFRESH_BATCH_LIMIT", "50"))
REFRESH_PARALLELISM = int(os.getenv("OAUTH_REFRESH_PARALLELISM", "20"))    # asyncio.gather fan-out (Q9)


async def _refresh_with_provider(
    cfg: ProviderConfig, refresh_token: str,
) -> ParsedTokenResponse:
    """POST grant_type=refresh_token to the provider's token endpoint.

    Module-level so the harsh suite can monkey-patch a fake response
    (success / invalid_grant / 5xx) without standing up a real Slack.
    """
    if not cfg.token_url:
        raise TokenExchangeError(cfg.name, "token_url_not_configured")
    data = {
        "client_id":     os.getenv(cfg.client_id_env, ""),
        "client_secret": os.getenv(cfg.client_secret_env, ""),
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(cfg.token_url, data=data)
        try:
            j = r.json()
        except Exception as e:
            raise TokenExchangeError(
                cfg.name, "non_json_response", f"status={r.status_code}",
            ) from e
    if cfg.parse_response is None:
        raise TokenExchangeError(cfg.name, "no_parser_configured")
    return cfg.parse_response(j)


async def _update_ap_connection(
    cfg: ProviderConfig,
    *,
    external_id: str,
    display_name: str,
    access_token: str,
    parsed: ParsedTokenResponse,
) -> None:
    """Idempotent upsert of the AP connection's value (verified empirically:
    POST /v1/app-connections with an existing externalId rotates the value).

    Best-effort from the worker's perspective — failure here logs warning
    but doesn't roll back the DB. The next refresh cycle re-tries; in the
    meantime the OLD AP token may go stale, which is bounded by interval.
    """
    if cfg.ap_value_builder is None:
        raise RuntimeError(f"provider {cfg.name} has no ap_value_builder")
    ap_base = os.getenv("AP_BASE_URL", "").rstrip("/")
    ap_pid = os.getenv("AP_PROJECT_ID", "")
    if not ap_base or not ap_pid:
        raise RuntimeError("AP_BASE_URL or AP_PROJECT_ID not set")
    from main import _engine
    if _engine is None or not _engine.token:
        raise RuntimeError("AP engine not authenticated")
    payload = {
        "projectId": ap_pid,
        "externalId": external_id,
        "displayName": display_name,
        "pieceName": f"@activepieces/piece-{cfg.name}",
        "type": cfg.ap_connection_type,
        "value": cfg.ap_value_builder(access_token, parsed),
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{ap_base}/api/v1/app-connections",
            headers={"Authorization": f"Bearer {_engine.token}"},
            json=payload,
        )
        if r.status_code >= 400:
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text[:300]}
            raise RuntimeError(f"AP upsert failed: {r.status_code} {body}")


async def _audit_refresh(
    *,
    tenant_id: str,
    provider: str,
    token_id: str,
    event: str,                  # 'token.refreshed' | 'token.refresh_failed'
    ok: bool,
    extra: dict | None = None,
) -> None:
    """Single audit row — same shape as the OAuth callback events for
    forensic uniformity. Failure to write audit must not abort refresh."""
    from database import async_session
    from models import TenantAuditLog
    try:
        async with async_session() as s:
            s.add(TenantAuditLog(
                project_id=tenant_id,
                endpoint="WORKER /oauth/refresh",
                http_status=200 if ok else 400,
                event_type=event,
                event_meta={
                    "provider": provider,
                    "encrypted_token_id": token_id,
                    **(extra or {}),
                },
            ))
            await s.commit()
    except Exception as e:
        log.error("[refresh] audit write failed (non-fatal): %s", e)


async def _refresh_one_token(
    token_id: str,
    *,
    crypto: Optional[CryptoProvider] = None,
) -> dict:
    """Rotate one expiring token end-to-end.

    Sequence:
      1. Load encrypted_tokens row.
      2. Acquire Redis mutex on the token (single-flight).
      3. Decrypt refresh_token (and capture old AAD).
      4. Call provider with grant_type=refresh_token.
      5. Generate FRESH DEK (rotation property — every refresh rotates
         the DEK so old DEK leaks don't compromise the new ciphertext).
      6. Re-encrypt access + refresh under the new DEK.
      7. Atomic UPDATE of encrypted_tokens (ciphertexts + IVs +
         wrapped_dek + scopes + expires_at + refresh_at + refresh_count).
      8. Best-effort upsert AP connection with the new access token.
      9. Audit token.refreshed.

    Failure modes:
      • invalid_grant from provider → mark token REVOKED + token.refresh_failed
      • Other TokenExchangeError → keep token ACTIVE, audit failure, retry next cycle
      • Decrypt error → mark token CORRUPTED + alert
      • DB error → audit failure, no rotation; old row preserved
      • AP upsert error → DB rotated successfully; warn; next cycle reconciles

    Returns a dict with the action taken — useful for the cycle summary.
    """
    from database import async_session
    from models import EncryptedToken
    from siyadah_oauth_state import _b64url_dec  # noqa: F401  unused but documents API surface

    if crypto is None:
        crypto = _get_crypto()

    redis = await _get_redis()

    # 1. Load row
    async with async_session() as s:
        row: EncryptedToken = (await s.execute(
            select(EncryptedToken).where(EncryptedToken.id == token_id)
        )).scalar_one_or_none()
    if row is None:
        return {"token_id": token_id, "action": "skip_missing"}
    if row.status != "ACTIVE":
        return {"token_id": token_id, "action": f"skip_status_{row.status}"}
    if row.encrypted_refresh_token is None:
        # Provider doesn't refresh (e.g. Slack without rotation enabled).
        # Bump refresh_at far into the future to stop polling.
        async with async_session() as s:
            await s.execute(
                update(EncryptedToken).where(EncryptedToken.id == token_id).values(
                    refresh_at=datetime.now(timezone.utc) + timedelta(days=365),
                )
            )
            await s.commit()
        return {"token_id": token_id, "action": "no_refresh_token_skip"}

    # 2. Redis mutex (single-flight)
    lock_key = f"oauth:refresh:lock:{token_id}"
    got_lock = await redis.set(lock_key, "1", nx=True, ex=REFRESH_LOCK_TTL)
    if not got_lock:
        return {"token_id": token_id, "action": "skip_locked"}

    try:
        cfg = get_provider(row.provider)

        # 3. Decrypt refresh token (and access for rebuilding AP value if AP refuses missing field)
        old_aad = f"{row.tenant_id}|{row.provider}|{row.id}".encode()
        try:
            from siyadah_crypto import Sealed, WrappedDEK
            wrapped_old = WrappedDEK(
                iv=row.iv_dek,
                ciphertext=row.wrapped_dek,
                version=row.encryption_version,
            )
            dek_old = crypto.unwrap_dek(wrapped_old, aad=old_aad)
            try:
                refresh_plaintext = crypto.decrypt_with_dek(
                    Sealed(iv=row.iv_refresh, ciphertext=row.encrypted_refresh_token),
                    dek_old, row.encryption_version,
                    aad=old_aad + b"|refresh",
                ).decode("utf-8")
            finally:
                del dek_old
        except Exception as e:
            log.error("[refresh] decrypt failed for token=%s: %s", token_id, e)
            await _audit_refresh(
                tenant_id=row.tenant_id, provider=row.provider, token_id=token_id,
                event="token.refresh_failed", ok=False,
                extra={"step": "decrypt", "error": f"{type(e).__name__}: {e}"},
            )
            # Mark CORRUPTED so the worker doesn't retry forever
            async with async_session() as s:
                await s.execute(
                    update(EncryptedToken).where(EncryptedToken.id == token_id).values(
                        status="CORRUPTED",
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await s.commit()
            return {"token_id": token_id, "action": "corrupted"}

        # 4. Call provider
        try:
            new_parsed = await _refresh_with_provider(cfg, refresh_plaintext)
        except TokenExchangeError as e:
            # invalid_grant means the user revoked. Mark REVOKED.
            terminal_codes = {"invalid_grant", "token_revoked", "invalid_refresh_token"}
            if e.code in terminal_codes:
                async with async_session() as s:
                    await s.execute(
                        update(EncryptedToken).where(EncryptedToken.id == token_id).values(
                            status="REVOKED",
                            updated_at=datetime.now(timezone.utc),
                        )
                    )
                    await s.commit()
                await _audit_refresh(
                    tenant_id=row.tenant_id, provider=row.provider, token_id=token_id,
                    event="token.refresh_failed", ok=False,
                    extra={"step": "provider", "error": e.code, "terminal": True},
                )
                return {"token_id": token_id, "action": "revoked"}
            # Transient — retry next cycle
            await _audit_refresh(
                tenant_id=row.tenant_id, provider=row.provider, token_id=token_id,
                event="token.refresh_failed", ok=False,
                extra={"step": "provider", "error": e.code, "transient": True},
            )
            return {"token_id": token_id, "action": "transient_provider_failure"}
        finally:
            # Scrub plaintext refresh from memory regardless of outcome
            refresh_plaintext = ""  # noqa: F841

        # 5–6. FRESH DEK + re-encrypt
        new_dek = crypto.gen_dek()
        try:
            new_wrapped = crypto.wrap_dek(new_dek, aad=old_aad)
            sealed_access = crypto.encrypt_with_dek(
                new_parsed.access_token.encode("utf-8"),
                new_dek,
                aad=old_aad + b"|access",
            )
            sealed_refresh = None
            # Provider may rotate the refresh_token (recommended) or keep it.
            new_refresh = new_parsed.refresh_token
            if new_refresh:
                sealed_refresh = crypto.encrypt_with_dek(
                    new_refresh.encode("utf-8"),
                    new_dek,
                    aad=old_aad + b"|refresh",
                )
        finally:
            del new_dek

        new_expires_at = None
        new_refresh_at = None
        if new_parsed.expires_in:
            new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=new_parsed.expires_in)
            new_refresh_at = new_expires_at - timedelta(minutes=5)

        # 7. Atomic DB update
        async with async_session() as s:
            await s.execute(
                update(EncryptedToken).where(EncryptedToken.id == token_id).values(
                    encrypted_access_token=sealed_access.ciphertext,
                    iv_access=sealed_access.iv,
                    encrypted_refresh_token=(sealed_refresh.ciphertext
                                             if sealed_refresh else
                                             row.encrypted_refresh_token),
                    iv_refresh=(sealed_refresh.iv
                                if sealed_refresh else
                                row.iv_refresh),
                    wrapped_dek=new_wrapped.ciphertext,
                    iv_dek=new_wrapped.iv,
                    encryption_version=new_wrapped.version,
                    scopes=list(new_parsed.scopes) or row.scopes,
                    expires_at=new_expires_at,
                    refresh_at=new_refresh_at,
                    refresh_count=row.refresh_count + 1,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await s.commit()

        # 8. AP upsert — Q4: on failure, set ap_sync_pending=True so the
        # NEXT cycle picks this row up via the AP-retry-only path.
        # On success, clear ap_sync_pending. Either way, DB rotation
        # already succeeded above — no provider call wasted.
        ap_warning = None
        ap_synced = False
        if row.ap_connection_external_id:
            try:
                await _update_ap_connection(
                    cfg,
                    external_id=row.ap_connection_external_id,
                    display_name=f"Siyadah {cfg.name.title()} ({row.tenant_id})",
                    access_token=new_parsed.access_token,
                    parsed=new_parsed,
                )
                ap_synced = True
            except Exception as e:
                ap_warning = f"{type(e).__name__}: {e}"
                log.warning(
                    "[refresh] AP upsert failed for token=%s ext=%s: %s — "
                    "marking ap_sync_pending=true; next cycle will retry "
                    "WITHOUT calling the provider again",
                    token_id, row.ap_connection_external_id, ap_warning,
                )
        else:
            ap_synced = True   # No AP connection to sync — vacuously synced

        # Update the ap_sync_pending flag in its own tiny tx so it's
        # durable even if downstream code crashes
        from database import async_session
        from models import EncryptedToken
        async with async_session() as s:
            await s.execute(
                update(EncryptedToken).where(EncryptedToken.id == token_id).values(
                    ap_sync_pending=(not ap_synced),
                )
            )
            await s.commit()

        # 9. Audit
        await _audit_refresh(
            tenant_id=row.tenant_id, provider=row.provider, token_id=token_id,
            event="token.refreshed", ok=True,
            extra={
                "refresh_count": row.refresh_count + 1,
                "new_expires_at": new_expires_at.isoformat() if new_expires_at else None,
                "rotated_refresh_token": sealed_refresh is not None,
                "ap_upsert_warning": ap_warning,
                "ap_sync_pending": not ap_synced,
            },
        )
        return {
            "token_id": token_id,
            "action": "rotated" if ap_synced else "rotated_ap_pending",
            "refresh_count": row.refresh_count + 1,
            "ap_upsert_warning": ap_warning,
        }
    finally:
        # Best-effort lock release
        try:
            await redis.delete(lock_key)
        except Exception:
            pass


async def _retry_ap_only(
    token_id: str, *, crypto: Optional[CryptoProvider] = None,
) -> dict:
    """Q4 fast-path — token is already rotated in DB (refresh_at in future,
    refresh_count > 0). Only the AP-side connection needs catching up.
    Decrypts the EXISTING access_token and re-pushes to AP. NEVER calls
    the provider's refresh endpoint, so we don't waste a refresh_token
    rotation on a bug that's outside the provider.
    """
    from database import async_session
    from models import EncryptedToken
    from siyadah_crypto import Sealed, WrappedDEK

    if crypto is None:
        crypto = _get_crypto()

    # Single-flight via the same Redis lock — prevents two replicas
    # racing on the same row.
    redis = await _get_redis()
    lock_key = f"oauth:refresh:lock:{token_id}"
    got = await redis.set(lock_key, "1", nx=True, ex=REFRESH_LOCK_TTL)
    if not got:
        return {"token_id": token_id, "action": "skip_locked"}

    try:
        async with async_session() as s:
            row: EncryptedToken = (await s.execute(
                select(EncryptedToken).where(EncryptedToken.id == token_id)
            )).scalar_one_or_none()
        if row is None:
            return {"token_id": token_id, "action": "skip_missing"}
        if row.status != "ACTIVE" or not row.ap_sync_pending:
            return {"token_id": token_id, "action": "skip_state"}

        cfg = get_provider(row.provider)
        if not row.ap_connection_external_id:
            # Nothing to sync to — clear the flag and move on
            async with async_session() as s:
                await s.execute(
                    update(EncryptedToken).where(EncryptedToken.id == token_id).values(
                        ap_sync_pending=False,
                    )
                )
                await s.commit()
            return {"token_id": token_id, "action": "ap_no_target"}

        # Decrypt the EXISTING access_token (already rotated by the prior
        # full-refresh cycle; we just need to push it to AP).
        aad = f"{row.tenant_id}|{row.provider}|{row.id}".encode()
        try:
            dek = crypto.unwrap_dek(
                WrappedDEK(iv=row.iv_dek, ciphertext=row.wrapped_dek,
                           version=row.encryption_version),
                aad=aad,
            )
            try:
                access = crypto.decrypt_with_dek(
                    Sealed(iv=row.iv_access, ciphertext=row.encrypted_access_token),
                    dek, row.encryption_version,
                    aad=aad + b"|access",
                ).decode("utf-8")
            finally:
                del dek
        except Exception as e:
            await _audit_refresh(
                tenant_id=row.tenant_id, provider=row.provider, token_id=token_id,
                event="oauth.ap_resync_failed", ok=False,
                extra={"step": "decrypt", "error": f"{type(e).__name__}: {e}"},
            )
            return {"token_id": token_id, "action": "ap_retry_decrypt_fail"}

        # Build a minimal ParsedTokenResponse from row state — no provider
        # call, just enough metadata for cfg.ap_value_builder.
        from oauth_providers import ParsedTokenResponse
        synthesised = ParsedTokenResponse(
            access_token=access,
            refresh_token=None,
            expires_in=None,
            scopes=list(row.scopes or []),
            provider_account_id=row.provider_account_id,
        )
        try:
            await _update_ap_connection(
                cfg,
                external_id=row.ap_connection_external_id,
                display_name=f"Siyadah {cfg.name.title()} ({row.tenant_id})",
                access_token=access,
                parsed=synthesised,
            )
        except Exception as e:
            await _audit_refresh(
                tenant_id=row.tenant_id, provider=row.provider, token_id=token_id,
                event="oauth.ap_resync_failed", ok=False,
                extra={"step": "ap_upsert", "error": f"{type(e).__name__}: {e}"},
            )
            log.warning(
                "[ap-retry] still failing for token=%s ext=%s: %s",
                token_id, row.ap_connection_external_id, e,
            )
            return {"token_id": token_id, "action": "ap_retry_failed"}
        finally:
            # Scrub plaintext from local scope
            access = ""

        # Success — clear the flag
        async with async_session() as s:
            await s.execute(
                update(EncryptedToken).where(EncryptedToken.id == token_id).values(
                    ap_sync_pending=False,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await s.commit()
        await _audit_refresh(
            tenant_id=row.tenant_id, provider=row.provider, token_id=token_id,
            event="oauth.ap_resynced", ok=True,
            extra={"recovered_via": "ap_retry_only"},
        )
        return {"token_id": token_id, "action": "ap_resynced"}
    finally:
        try:
            await redis.delete(lock_key)
        except Exception:
            pass


async def _claim_due_tokens(limit: int) -> tuple[list[str], list[str]]:
    """Q1 — atomically claim a batch of tokens needing work. Cross-replica
    safe via SELECT FOR UPDATE SKIP LOCKED + processing_until lease.

    Returns (full_refresh_ids, ap_retry_ids).

    The lease prevents two replicas claiming the same row. If a worker
    crashes mid-cycle, the lease expires (5 min) and the next cycle
    re-claims the row.
    """
    from database import async_session
    from models import EncryptedToken
    from sqlalchemy import or_, and_

    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=REFRESH_LEASE_TTL_SECONDS)

    async with async_session() as s:
        # Atomically: pick rows due for FULL refresh OR AP retry, that
        # are not currently leased, lock them, mark with our lease.
        stmt = (
            select(EncryptedToken.id, EncryptedToken.ap_sync_pending,
                   EncryptedToken.refresh_at)
            .where(
                EncryptedToken.status == "ACTIVE",
                or_(
                    # Full refresh due
                    and_(
                        EncryptedToken.refresh_at.isnot(None),
                        EncryptedToken.refresh_at <= now,
                    ),
                    # AP-only retry needed
                    EncryptedToken.ap_sync_pending == True,        # noqa: E712
                ),
                or_(
                    EncryptedToken.processing_until.is_(None),
                    EncryptedToken.processing_until < now,
                ),
            )
            .order_by(EncryptedToken.refresh_at.asc().nulls_last())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (await s.execute(stmt)).all()
        if not rows:
            await s.commit()
            return [], []

        ids = [r[0] for r in rows]
        # Stamp lease in same transaction → atomic claim
        await s.execute(
            update(EncryptedToken)
            .where(EncryptedToken.id.in_(ids))
            .values(processing_until=lease_until)
        )
        await s.commit()

    # Split into the two paths so the dispatcher knows which function to call
    full_refresh_ids: list[str] = []
    ap_retry_ids: list[str] = []
    for tid, sync_pending, refresh_at in rows:
        # If a row is BOTH due for refresh AND ap_sync_pending, the
        # full refresh path handles both (it sets ap_sync_pending after
        # its own AP upsert attempt, so the flag is naturally reconciled).
        if refresh_at is not None and refresh_at <= now:
            full_refresh_ids.append(tid)
        elif sync_pending:
            ap_retry_ids.append(tid)
    return full_refresh_ids, ap_retry_ids


async def _release_lease(token_id: str) -> None:
    """Clear processing_until so the row is re-eligible. Best-effort —
    if this fails the row's lease will simply expire after REFRESH_LEASE_TTL."""
    from database import async_session
    from models import EncryptedToken
    try:
        async with async_session() as s:
            await s.execute(
                update(EncryptedToken).where(EncryptedToken.id == token_id).values(
                    processing_until=None,
                )
            )
            await s.commit()
    except Exception as e:
        log.debug("[refresh] lease-release failed for token=%s (will expire): %s",
                  token_id, e)


async def refresh_due_tokens(
    *, limit: int = REFRESH_BATCH_LIMIT,
    crypto: Optional[CryptoProvider] = None,
) -> dict:
    """One pass of the refresh worker.

    Hardened (Phase 4.6):
      • Q1 — atomic claim via SELECT FOR UPDATE SKIP LOCKED + lease,
        so multi-replica deployments never double-process a row.
      • Q4 — splits work into two paths: full provider refresh AND
        AP-only retry (for rows whose previous refresh succeeded but
        AP upsert had failed).
      • Q9 — per-token processing runs in PARALLEL via asyncio.gather
        with a bounded fan-out (REFRESH_PARALLELISM).
    """
    from database import async_session
    from models import EncryptedToken

    now = datetime.now(timezone.utc)
    full_ids, ap_ids = await _claim_due_tokens(limit)

    summary: dict = {
        "checked_at": now.isoformat(),
        "claimed": len(full_ids) + len(ap_ids),
        "full_refresh": len(full_ids),
        "ap_retry": len(ap_ids),
        "actions": {},
    }
    if not full_ids and not ap_ids:
        return summary

    if crypto is None:
        crypto = _get_crypto()

    # Q9 — bounded parallelism via semaphore inside gather
    sem = asyncio.Semaphore(REFRESH_PARALLELISM)

    async def _full(tid: str):
        async with sem:
            try:
                return await _refresh_one_token(tid, crypto=crypto)
            except Exception as e:
                log.error("[refresh] unhandled error in full-refresh for %s: %s",
                          tid, e)
                return {"token_id": tid, "action": "unhandled_error"}
            finally:
                await _release_lease(tid)

    async def _ap(tid: str):
        async with sem:
            try:
                return await _retry_ap_only(tid, crypto=crypto)
            except Exception as e:
                log.error("[refresh] unhandled error in ap-retry for %s: %s",
                          tid, e)
                return {"token_id": tid, "action": "unhandled_error"}
            finally:
                await _release_lease(tid)

    results = await asyncio.gather(
        *(_full(t) for t in full_ids),
        *(_ap(t) for t in ap_ids),
        return_exceptions=False,            # we already swallow per-task above
    )

    for res in results:
        action = res.get("action", "unknown") if isinstance(res, dict) else "unknown"
        summary["actions"][action] = summary["actions"].get(action, 0) + 1

    return summary


async def _refresh_loop(interval: int = REFRESH_DEFAULT_INTERVAL):
    """The eternal pulse. Mounted as an asyncio task in main.lifespan."""
    log.info("[refresh-worker] starting; interval=%ds, batch=%d",
             interval, REFRESH_BATCH_LIMIT)
    while True:
        try:
            summary = await refresh_due_tokens()
            if summary["due"] > 0:
                log.info("[refresh-worker] cycle: %s", summary)
        except Exception as e:
            log.error("[refresh-worker] cycle failed: %s", e)
        await asyncio.sleep(interval)


async def _finalize_saga_completed(
    *,
    saga_id: str,
    tenant_id: str,
    encrypted_token_id: str,
    ap_external_id: str,
    cfg_name: str,
    parsed: ParsedTokenResponse,
) -> None:
    """Move saga TOKEN_OBTAINED → AP_CONNECTION_CREATED → COMPLETED in
    one atomic transaction, plus stamp encrypted_tokens with the
    AP-side externalId so the row is fully linked.
    """
    from database import async_session
    from models import EncryptedToken, OAuthSaga, TenantAuditLog
    now = datetime.now(timezone.utc)
    async with async_session() as s:
        await s.execute(
            update(EncryptedToken)
            .where(EncryptedToken.id == encrypted_token_id)
            .values(ap_connection_external_id=ap_external_id)
        )
        # Two-step transition in one tx — AP_CONNECTION_CREATED is a
        # logical waypoint; we land on COMPLETED for the row's final
        # written value but persist the audit trail explicitly.
        await s.execute(
            update(OAuthSaga).where(OAuthSaga.id == saga_id).values(
                status="COMPLETED",
                ap_connection_external_id=ap_external_id,
                completed_at=now,
            )
        )
        s.add(TenantAuditLog(
            project_id=tenant_id,
            endpoint=f"GET /v2/oauth/{cfg_name}/callback",
            http_status=200,
            event_type="oauth.completed",
            event_meta={
                "provider": cfg_name,
                "saga_id": saga_id,
                "encrypted_token_id": encrypted_token_id,
                "ap_connection_external_id": ap_external_id,
                "scopes_count": len(parsed.scopes),
                "provider_account_id": parsed.provider_account_id,
            },
        ))
        await s.commit()


# ═══════════════════════════════════════════════════════════════
# /initiate
# ═══════════════════════════════════════════════════════════════

@router.post("/{provider}/initiate", response_model=OAuthInitiateResponse)
async def oauth_initiate(
    provider: str, request: Request, body: OAuthInitiateBody,
):
    try:
        cfg = get_provider(provider)
    except UnknownProviderError as e:
        raise HTTPException(404, str(e)) from e

    from main import resolve_pid
    tenant_id = resolve_pid(request, None)
    if not tenant_id:
        raise HTTPException(401, "tenant required")

    sp = StateProvider.from_env()
    state_token, nonce = sp.make_state(
        tenant_id, body.return_path, ttl_seconds=STATE_TTL_SECONDS,
    )
    pkce_verifier, pkce_challenge = gen_pkce()
    scopes = body.scopes or list(cfg.default_scopes)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=STATE_TTL_SECONDS)

    from database import async_session
    from models import OAuthSaga, TenantAuditLog

    async with async_session() as s:
        saga = OAuthSaga(
            tenant_id=tenant_id,
            provider=cfg.name,
            scope=scopes,
            state_nonce=nonce,
            pkce_verifier=pkce_verifier,
            status="INITIATED",
            expires_at=expires_at,
        )
        s.add(saga)
        await s.commit()
        saga_id = saga.id

    redis = await _get_redis()
    try:
        await NonceStore(redis, ttl_seconds=STATE_TTL_SECONDS).register(
            nonce, tenant_id,
        )
    except NonceReplayError:
        async with async_session() as s:
            await s.execute(delete(OAuthSaga).where(OAuthSaga.id == saga_id))
            await s.commit()
        log.error("[oauth.initiate] nonce collision (1/2^192) — saga %s rolled back", saga_id)
        raise HTTPException(500, "OAuth state initialization failed") from None
    except Exception as e:
        async with async_session() as s:
            await s.execute(delete(OAuthSaga).where(OAuthSaga.id == saga_id))
            await s.commit()
        log.error("[oauth.initiate] redis failure: %s — saga %s rolled back", e, saga_id)
        raise HTTPException(503, "redis unavailable for OAuth state") from e

    try:
        async with async_session() as s:
            req_id = getattr(request.state, "request_id", None)
            api_key_hash = getattr(request.state, "api_key_hash", None)
            ip = request.client.host if request.client else None
            s.add(TenantAuditLog(
                project_id=tenant_id,
                api_key_hash=api_key_hash,
                endpoint=f"POST /v2/oauth/{provider}/initiate",
                http_status=200,
                request_id=req_id,
                remote_ip=ip,
                user_agent=request.headers.get("user-agent"),
                event_type="oauth.initiated",
                event_meta={
                    "provider": cfg.name,
                    "saga_id": saga_id,
                    "scopes_count": len(scopes),
                    "return_path": body.return_path,
                    "expires_at": expires_at.isoformat(),
                },
            ))
            await s.commit()
    except Exception as e:
        log.error("[oauth.initiate] audit write failed (non-fatal): %s", e)

    auth_url = _build_authorize_url(
        cfg, state_token=state_token,
        pkce_challenge=pkce_challenge, scopes=scopes,
    )

    log.info(
        "[oauth.initiate] tenant=%s provider=%s saga=%s scopes=%d",
        tenant_id, cfg.name, saga_id, len(scopes),
    )

    return OAuthInitiateResponse(
        authorization_url=auth_url,
        saga_id=saga_id,
        expires_at=expires_at.isoformat(),
        provider=cfg.name,
        scopes=scopes,
    )


# ═══════════════════════════════════════════════════════════════
# /callback
# ═══════════════════════════════════════════════════════════════

@router.get("/{provider}/callback", response_model=OAuthCallbackResponse)
async def oauth_callback(
    provider: str,
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None, alias="error_description"),
):
    try:
        cfg = get_provider(provider)
    except UnknownProviderError as e:
        raise HTTPException(404, str(e)) from e

    # Provider-side error (user denied, scope rejected, etc.)
    if error:
        if state:
            try:
                claims_un = _decode_state_unsafe_for_attribution(state)
                if claims_un:
                    sid = await _find_saga_by_nonce(claims_un["n"])
                    if sid:
                        await _mark_saga_failed(
                            sid, step="provider_denied",
                            reason=f"{error}: {error_description or ''}".strip(": "),
                        )
            except Exception as e:
                log.warning("[oauth.callback] couldn't attribute provider error: %s", e)
        raise HTTPException(400, detail={
            "error": error, "error_description": error_description,
        })

    if not code or not state:
        raise HTTPException(400, "missing code or state")

    # Verify state HMAC (we extract tid from payload first to know what
    # tenant to compare against — verify_state is the actual gate).
    sp = StateProvider.from_env()
    try:
        claims_un = _decode_state_unsafe_for_attribution(state)
        if not claims_un:
            raise HTTPException(400, "malformed state")
        claims = sp.verify_state(state, claims_un["tid"])
    except StateError as e:
        log.warning("[oauth.callback] state verification failed: %s", type(e).__name__)
        raise HTTPException(400, f"state verification failed: {type(e).__name__}") from e

    tenant_id = claims.tenant_id
    nonce = claims.nonce

    from database import async_session
    from models import OAuthSaga, TenantAuditLog
    async with async_session() as s:
        saga = (await s.execute(
            select(OAuthSaga).where(OAuthSaga.state_nonce == nonce)
        )).scalar_one_or_none()
    if saga is None:
        raise HTTPException(400, "saga not found for this state")
    if saga.tenant_id != tenant_id or saga.provider != cfg.name:
        raise HTTPException(403, "saga/state tenant or provider mismatch")
    if saga.status != "INITIATED":
        raise HTTPException(409, f"saga is {saga.status}, expected INITIATED")

    saga_id = saga.id
    pkce_verifier = saga.pkce_verifier

    # Atomic single-use nonce consume
    redis = await _get_redis()
    try:
        await NonceStore(redis).consume(nonce, tenant_id)
    except NonceNotFoundError:
        await _mark_saga_failed(saga_id, step="nonce_consume", reason="nonce_replayed_or_expired")
        raise HTTPException(409, "nonce already consumed or expired") from None
    except StateError as e:
        await _mark_saga_failed(saga_id, step="nonce_consume", reason=str(e))
        raise HTTPException(403, f"nonce check failed: {type(e).__name__}") from e

    # Exchange code → tokens
    try:
        parsed = await _exchange_code(
            cfg, code, verifier=pkce_verifier if cfg.uses_pkce else None,
        )
    except TokenExchangeError as e:
        await _mark_saga_failed(saga_id, step="token_exchange", reason=str(e))
        raise HTTPException(400, detail={
            "error": "token_exchange_failed",
            "provider_error": e.code,
            "provider": cfg.name,
        }) from e
    except Exception as e:
        await _mark_saga_failed(saga_id, step="token_exchange", reason=f"{type(e).__name__}: {e}")
        raise HTTPException(502, "token exchange transport error") from e

    # Encrypt + persist + transition saga
    try:
        crypto = _get_crypto()
        encrypted_token_id = await _persist_tokens(
            saga_id=saga_id, tenant_id=tenant_id, provider=cfg.name,
            parsed=parsed, crypto=crypto,
        )
    except Exception as e:
        msg = str(e)
        # Heuristic: errors mentioning AES/DEK/encrypt/crypto get failure_step='encrypt'
        msg_lower = (msg + " " + type(e).__name__).lower()
        if any(t in msg_lower for t in ("aes", "dek", "encrypt", "crypto")):
            step = "encrypt"
        else:
            step = "db_insert"
        await _mark_saga_failed(saga_id, step=step, reason=f"{type(e).__name__}: {msg}")
        log.error("[oauth.callback] persist failed (step=%s): %s — saga %s", step, e, saga_id)
        raise HTTPException(500, f"failed to persist tokens: {type(e).__name__}") from e

    # ── Audit token_exchanged (waypoint, not terminal) ─────────────
    try:
        async with async_session() as s:
            s.add(TenantAuditLog(
                project_id=tenant_id,
                endpoint=f"GET /v2/oauth/{cfg.name}/callback",
                http_status=200,
                event_type="oauth.token_exchanged",
                event_meta={
                    "provider": cfg.name,
                    "saga_id": saga_id,
                    "encrypted_token_id": encrypted_token_id,
                    "scopes_count": len(parsed.scopes),
                    "provider_account_id": parsed.provider_account_id,
                    "has_refresh_token": parsed.refresh_token is not None,
                },
            ))
            await s.commit()
    except Exception as e:
        log.error("[oauth.callback] audit failed (non-fatal): %s", e)

    # ── Phase 4.3 — link to Activepieces + final saga transition ───
    try:
        ap_record = await _create_ap_connection(
            cfg,
            saga_id=saga_id,
            tenant_id=tenant_id,
            access_token=parsed.access_token,
            parsed=parsed,
        )
        ap_external_id = ap_record.get("externalId") or ap_record.get("id", "")
        if not ap_external_id:
            raise RuntimeError(f"AP returned no externalId: {ap_record}")
    except Exception as e:
        # L5 compensating rollback — Wipe the encrypted_tokens row,
        # transition saga to COMPENSATED, audit oauth.saga_compensated.
        # No partial state survives.
        await _compensate_ap_failure(
            saga_id=saga_id,
            tenant_id=tenant_id,
            encrypted_token_id=encrypted_token_id,
            cfg_name=cfg.name,
            error_reason=f"{type(e).__name__}: {e}",
        )
        log.error(
            "[oauth.callback] AP link failed → COMPENSATED. saga=%s reason=%s",
            saga_id, str(e)[:200],
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": "ap_connection_failed",
                "saga_status": "COMPENSATED",
                "message": "Activepieces connection creation failed; "
                           "tokens wiped and saga rolled back.",
            },
        ) from e

    # Drop the in-memory plaintext as soon as AP has accepted it.
    parsed_access = parsed.access_token  # noqa: F841 — keep ref clear
    del parsed_access

    # ── Saga climax: TOKEN_OBTAINED → COMPLETED (atomic) ───────────
    try:
        await _finalize_saga_completed(
            saga_id=saga_id,
            tenant_id=tenant_id,
            encrypted_token_id=encrypted_token_id,
            ap_external_id=ap_external_id,
            cfg_name=cfg.name,
            parsed=parsed,
        )
    except Exception as e:
        # Saga finalize failed AFTER AP accepted. Two options:
        #   (a) leave saga at TOKEN_OBTAINED + AP connection orphan
        #   (b) compensate (delete AP connection + wipe vault)
        # We pick (b) — full rollback — to honour the No-Ghost rule.
        # Best-effort delete of the AP connection (idempotent).
        try:
            from main import _engine
            ap_base = os.getenv("AP_BASE_URL", "").rstrip("/")
            async with httpx.AsyncClient(timeout=10) as c:
                await c.delete(
                    f"{ap_base}/api/v1/app-connections/{ap_record.get('id', '')}",
                    headers={"Authorization": f"Bearer {_engine.token}"},
                )
        except Exception as cleanup_err:
            log.error(
                "[oauth.callback] could not roll back AP connection %s: %s",
                ap_record.get("id"), cleanup_err,
            )
        await _compensate_ap_failure(
            saga_id=saga_id,
            tenant_id=tenant_id,
            encrypted_token_id=encrypted_token_id,
            cfg_name=cfg.name,
            error_reason=f"finalize_failed: {type(e).__name__}: {e}",
        )
        raise HTTPException(500, "saga finalization failed") from e

    log.info(
        "[oauth.callback] tenant=%s provider=%s saga=%s → COMPLETED ap_conn=%s",
        tenant_id, cfg.name, saga_id, ap_external_id,
    )

    return OAuthCallbackResponse(
        saga_id=saga_id,
        status="COMPLETED",
        provider=cfg.name,
        encrypted_token_id=encrypted_token_id,
        return_path=claims.return_path,
    )
