"""
Siyadah OAuth State + PKCE — Layer 1 of Sovereign-Grade OAuth
==============================================================

Three primitives that defend the OAuth handshake itself:

  1. HMAC-signed state token
       payload = {tid, n, iat, exp, rp}     ← compact JSON, sorted keys
       state   = b64url(payload) + "." + b64url(HMAC-SHA256(key, payload))

     Defends: CSRF on callback (#1), state replay (#2 — paired with nonce),
              cross-tenant takeover (#3 via tid claim).

  2. Redis-backed single-use nonce store
       key  : "oauth:nonce:<nonce>"
       val  : tenant_id
       TTL  : 300s (5 min default)
       SET-NX on register; GETDEL on consume → atomic single-use semantics.

     Defends: replay (#15 — second consume gets None), race-on-callback (#19).

  3. PKCE (S256)
       verifier  = b64url(64 random bytes) → 64-char URL-safe string
       challenge = b64url(SHA-256(verifier))

     Defends: authorization-code interception (#4) — even if a malicious
              proxy/extension grabs the `?code=` parameter, the verifier
              never left this server, so the code can't be redeemed.

Separate from the Master Key (siyadah_crypto.py): state signing key has
a much shorter blast radius. If compromised, attacker can forge state
tokens until rotation, but cannot read tokens or DEKs.

Threats this module defends — Gap-4 threat model rows 1, 2, 3, 4, 15, 19.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass


# ═══════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════

STATE_TTL_SECONDS: int = 300                # 5 minutes — caller can override per-call
STATE_KEY_BYTES: int = 32                   # HMAC-SHA256 key
NONCE_BYTES: int = 24                       # 192 bits → 32-char b64url string
PKCE_VERIFIER_BYTES: int = 48               # → 64-char b64url verifier (RFC 7636 ≥43, ≤128)
NONCE_REDIS_PREFIX: str = "oauth:nonce:"
_STATE_KEY_ENV_VAR: str = "SIYADAH_OAUTH_STATE_KEY"


# ═══════════════════════════════════════════════════════════════
# Errors — every one structured so the caller can switch on type
# ═══════════════════════════════════════════════════════════════

class StateError(Exception):
    """Base class. Caller catches StateError to reject the callback."""


class StateConfigError(StateError):
    """Signing key missing/malformed/wrong size. Boot-time fatal."""


class StateMalformedError(StateError):
    """Token doesn't have the expected shape — bad b64, missing claim."""


class StateSignatureError(StateError):
    """HMAC mismatch. Either tampered or signed by a different key."""


class StateExpiredError(StateError):
    """exp claim is in the past. 5-min TTL elapsed."""


class StateTenantMismatchError(StateError):
    """tid claim ≠ expected tenant. Cross-tenant attack or stale token."""


class NonceReplayError(StateError):
    """Nonce already exists in Redis — register failed (write-side)."""


class NonceNotFoundError(StateError):
    """Nonce missing from Redis on consume — replay (already used) or expired."""


# ═══════════════════════════════════════════════════════════════
# Dataclasses — structured state claims after verification
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class StateClaims:
    """Verified state — every field already authenticated by HMAC."""
    tenant_id: str
    nonce: str
    issued_at: int          # unix seconds
    expires_at: int         # unix seconds
    return_path: str


# ═══════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════

_B64URL_RE = re.compile(r"^[A-Za-z0-9_\-]*$")


def _b64url_enc(b: bytes) -> str:
    """URL-safe base64 without padding — RFC 4648 §5, no trailing '='."""
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64url_dec(s: str) -> bytes:
    """Inverse of _b64url_enc. Strict on alphabet — Python's stdlib decoder
    silently drops non-base64 chars by default, so we pre-validate before
    decoding. This makes garbage input (e.g. '###') raise a clean ValueError
    instead of producing empty output that would later fail HMAC ambiguously.
    """
    if not _B64URL_RE.match(s):
        raise ValueError(f"non-URL-safe-base64 characters in input")
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _ct_eq(a: bytes, b: bytes) -> bool:
    """Constant-time comparison. Defends against timing-side-channel."""
    return hmac.compare_digest(a, b)


def _now() -> int:
    return int(time.time())


# ═══════════════════════════════════════════════════════════════
# StateProvider — HMAC issue + verify
# ═══════════════════════════════════════════════════════════════

class StateProvider:
    """The only public surface for state-token issuance and verification.

    Construct via `from_env()` in production; tests instantiate directly
    with a known key.
    """

    def __init__(self, signing_key: bytes):
        if not isinstance(signing_key, (bytes, bytearray)):
            raise StateConfigError(
                f"signing_key must be bytes, got {type(signing_key).__name__}"
            )
        if len(signing_key) != STATE_KEY_BYTES:
            raise StateConfigError(
                f"signing_key must be {STATE_KEY_BYTES} bytes "
                f"(got {len(signing_key)})"
            )
        self._key = bytes(signing_key)

    @classmethod
    def from_env(cls, env_var: str = _STATE_KEY_ENV_VAR) -> "StateProvider":
        raw = os.getenv(env_var, "").strip()
        if not raw:
            raise StateConfigError(
                f"{env_var} not set. Generate with:\n"
                f"  python -c 'import os,base64; "
                f"print(base64.urlsafe_b64encode(os.urandom(32)).decode())'"
            )
        try:
            key = _b64url_dec(raw)
        except Exception as e:
            raise StateConfigError(
                f"{env_var} not valid urlsafe base64: {e}"
            ) from e
        return cls(key)

    # ───── issue ─────

    def make_state(
        self,
        tenant_id: str,
        return_path: str = "/",
        *,
        ttl_seconds: int = STATE_TTL_SECONDS,
        now: int | None = None,
    ) -> tuple[str, str]:
        """Generate a signed state token + the bound nonce.

        The caller MUST register the returned nonce in `NonceStore` before
        sending the user to the OAuth provider — otherwise the callback
        path will reject with NonceNotFoundError.

        Returns
        -------
        (state_token, nonce) — both URL-safe; state goes into the OAuth
        URL `state=` parameter, nonce is internal-only.
        """
        if not tenant_id:
            raise StateConfigError("tenant_id is required")
        nonce = _b64url_enc(secrets.token_bytes(NONCE_BYTES))
        issued = _now() if now is None else now
        payload = {
            "tid": tenant_id,
            "n":   nonce,
            "iat": issued,
            "exp": issued + ttl_seconds,
            "rp":  return_path,
        }
        # sort_keys + compact separators → canonical bytes; HMAC is then
        # over EXACTLY the bytes we transmit (no whitespace surprise).
        payload_bytes = json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        sig = hmac.new(self._key, payload_bytes, hashlib.sha256).digest()
        token = _b64url_enc(payload_bytes) + "." + _b64url_enc(sig)
        return token, nonce

    # ───── verify ─────

    def verify_state(
        self,
        token: str,
        expected_tenant_id: str,
        *,
        now: int | None = None,
    ) -> StateClaims:
        """Verify a token end-to-end. Returns claims on success; raises
        a typed StateError subclass on EVERY failure path.

        Order of checks (cheapest first, then crypto, then business rules):
          1. Shape: not empty, exactly one '.' separator, both halves b64.
          2. HMAC: payload signature matches expected key.
          3. JSON: payload is well-formed JSON with all required claims.
          4. Tenant: tid == expected_tenant_id (constant-time compare).
          5. Expiry: exp > now.

        Nonce single-use is checked separately by NonceStore.consume —
        verify_state is pure (no I/O), so it's cheap to retry.
        """
        if not token or not isinstance(token, str):
            raise StateMalformedError("token is empty or non-string")
        parts = token.split(".")
        if len(parts) != 2:
            raise StateMalformedError(
                f"expected 'payload.sig', got {len(parts)} parts"
            )
        try:
            payload_bytes = _b64url_dec(parts[0])
            sig = _b64url_dec(parts[1])
        except Exception as e:
            raise StateMalformedError(f"bad base64: {e}") from e

        # Crypto — constant-time HMAC compare
        expected_sig = hmac.new(self._key, payload_bytes, hashlib.sha256).digest()
        if not _ct_eq(sig, expected_sig):
            raise StateSignatureError(
                "HMAC mismatch — tampered token or wrong signing key"
            )

        # Payload parsing (only after HMAC succeeds — never trust unsigned bytes)
        try:
            payload = json.loads(payload_bytes)
        except Exception as e:
            raise StateMalformedError(f"payload not valid JSON: {e}") from e
        if not isinstance(payload, dict):
            raise StateMalformedError("payload is not an object")
        for k in ("tid", "n", "iat", "exp", "rp"):
            if k not in payload:
                raise StateMalformedError(f"missing claim: {k!r}")

        # Tenant binding — constant-time compare
        if not _ct_eq(
            str(payload["tid"]).encode(),
            str(expected_tenant_id).encode(),
        ):
            raise StateTenantMismatchError(
                f"state bound to different tenant than expected"
            )

        # Expiry
        check_now = _now() if now is None else now
        if check_now >= int(payload["exp"]):
            raise StateExpiredError(
                f"state expired (exp={payload['exp']}, now={check_now})"
            )

        return StateClaims(
            tenant_id=str(payload["tid"]),
            nonce=str(payload["n"]),
            issued_at=int(payload["iat"]),
            expires_at=int(payload["exp"]),
            return_path=str(payload["rp"]),
        )


# ═══════════════════════════════════════════════════════════════
# NonceStore — Redis-backed, atomic single-use
# ═══════════════════════════════════════════════════════════════

class NonceStore:
    """Wraps a redis.asyncio (or compatible) client. Two operations:

      • register(nonce, tenant_id)  — SET NX EX 300. Fails if duplicate.
      • consume(nonce, tenant_id)   — GETDEL. Returns nothing on success;
                                       raises NonceNotFoundError if missing
                                       (already consumed / expired / never set);
                                       raises StateTenantMismatchError if
                                       the nonce belongs to a different tenant.

    Uses GETDEL (Redis ≥ 6.2) for atomicity — two concurrent callbacks
    race-free; only one wins.
    """

    def __init__(self, redis_client, *, ttl_seconds: int = STATE_TTL_SECONDS):
        self._r = redis_client
        self._ttl = ttl_seconds

    @staticmethod
    def _key(nonce: str) -> str:
        return f"{NONCE_REDIS_PREFIX}{nonce}"

    async def register(self, nonce: str, tenant_id: str) -> None:
        """Write the nonce → tenant binding. Fails if nonce already exists.

        Replay defence at WRITE time: an attacker who replays a state token
        can't pre-register its nonce after we've already done so.
        """
        if not nonce or not tenant_id:
            raise StateConfigError("nonce and tenant_id required")
        ok = await self._r.set(
            self._key(nonce), tenant_id, nx=True, ex=self._ttl,
        )
        if not ok:
            raise NonceReplayError(
                f"nonce already registered (prefix={nonce[:8]}…)"
            )

    async def consume(self, nonce: str, expected_tenant_id: str) -> None:
        """Atomically read+delete the nonce. Single-use semantics.

        Replay defence at READ time: a second `consume` for the same nonce
        gets None from GETDEL and raises NonceNotFoundError.
        """
        raw = await self._r.getdel(self._key(nonce))
        if raw is None:
            raise NonceNotFoundError(
                "nonce missing — already consumed, expired, or never registered"
            )
        actual = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        if not _ct_eq(str(actual).encode(), str(expected_tenant_id).encode()):
            # Re-register the nonce briefly for the LEGITIMATE owner? No —
            # we already consumed it. The legitimate owner will hit
            # NonceNotFoundError on retry, which is the correct user-visible
            # outcome (one attempt allowed; restart the flow). Better to fail
            # closed than to leak attack signals.
            raise StateTenantMismatchError(
                f"nonce belongs to a different tenant than the caller"
            )

    async def ttl_seconds(self, nonce: str) -> int:
        """Diagnostic: returns seconds remaining for a nonce. -2 if missing,
        -1 if no expiry. Used in tests; not in production critical path."""
        return await self._r.ttl(self._key(nonce))


# ═══════════════════════════════════════════════════════════════
# PKCE — code_verifier + code_challenge (S256)
# ═══════════════════════════════════════════════════════════════

PKCE_METHOD: str = "S256"


def gen_pkce() -> tuple[str, str]:
    """Generate a fresh PKCE pair. Returns (verifier, challenge).

    Verifier is 64 URL-safe characters (~256 bits of entropy). Challenge
    is the SHA-256 of the verifier, b64url-encoded without padding —
    exactly what the OAuth provider expects with `code_challenge_method=S256`.

    Caller stores `verifier` server-side (in the OAuth saga row) and
    sends `challenge` to the provider in the authorization URL. On
    callback, the verifier is sent to the token endpoint; the provider
    re-hashes it and compares — codes intercepted in transit are useless.
    """
    verifier = _b64url_enc(secrets.token_bytes(PKCE_VERIFIER_BYTES))
    challenge = _b64url_enc(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def verify_pkce(verifier: str, expected_challenge: str) -> bool:
    """Sanity check: re-hash the verifier and compare to the expected challenge.

    The OAuth PROVIDER does this canonically. We expose it for tests and
    for any internal consistency check we want before sending the
    verifier on the wire.
    """
    if not verifier or not expected_challenge:
        return False
    actual = _b64url_enc(hashlib.sha256(verifier.encode("ascii")).digest())
    return _ct_eq(actual.encode(), expected_challenge.encode())
