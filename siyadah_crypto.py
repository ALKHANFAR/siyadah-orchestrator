"""
Siyadah Crypto — Envelope Encryption (Layer 2 of Sovereign-Grade OAuth)
========================================================================

Bank-grade token storage using AES-GCM-256 envelope encryption:

  ┌─────────────────────┐     ┌──────────────────────────┐
  │  Master Key (MK)    │     │  Per-token DEK (random)  │
  │  32 bytes / env var │ ──► │   wraps the token        │
  │  Wraps every DEK    │     │   wrapped by MK          │
  └─────────────────────┘     └──────────────────────────┘

Each row in `encrypted_tokens` carries its own DEK. The DEK is itself
encrypted under the Master Key. Compromise of one DEK leaks one token;
compromise of the MK forces re-wrap of every DEK (~ms per row) without
ever decrypting the underlying tokens — that's the agility property
real KMS systems were designed to deliver.

Crypto agility is non-negotiable: every ciphertext is tagged with an
`encryption_version` that participates in the AAD. Downgrading the
version on storage invalidates the auth tag — tamper detected.

Today's primitive: AES-256-GCM (RFC 5116). 12-byte IV, 16-byte tag,
authenticated encryption with associated data. Not deterministic — two
encryptions of the same plaintext produce different ciphertexts. The
IV is randomly generated per call (`secrets.token_bytes(12)`).

KMS-Ready surface: when we move from Option-A (env var MK) to a real
KMS (AWS / GCP / Azure), the only change is `CryptoProvider.__init__`:
swap the AESGCM(MK) call for an external KMS client. The wrap/unwrap
contract stays identical.

Threats this module defends — see Gap-4 threat model row 6, 8, 9, 10.
"""
from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ═══════════════════════════════════════════════════════════════
# Versioning — the bedrock of crypto agility
# ═══════════════════════════════════════════════════════════════

CURRENT_ENCRYPTION_VERSION: int = 1
"""The version stamped on every NEW ciphertext today. Bump when changing
the underlying cipher (e.g. → ChaCha20-Poly1305, AES-OCB)."""

SUPPORTED_DECRYPT_VERSIONS: frozenset[int] = frozenset({1})
"""Versions still accepted by `decrypt_*` paths. NEVER include a version
that has been deprecated for security reasons — decrypt would silently
honour weak crypto. Strictly a superset of CURRENT_ENCRYPTION_VERSION
during migration windows; otherwise equal to it."""

_MK_ENV_VAR = "SIYADAH_OAUTH_MK"
_MASTER_KEY_BYTES = 32      # AES-256
_DEK_BYTES = 32             # AES-256
_GCM_IV_BYTES = 12          # NIST-recommended for AES-GCM
_GCM_TAG_BYTES = 16         # baked into ciphertext by the cryptography lib


# ═══════════════════════════════════════════════════════════════
# Errors
# ═══════════════════════════════════════════════════════════════

class CryptoConfigError(RuntimeError):
    """Master key missing, malformed, or wrong length. Fail-fast at boot."""


class DecryptionError(Exception):
    """Authenticated decryption failed: tampered ciphertext, wrong key,
    wrong AAD, or wrong IV. The cipher won't tell us *which* — by design."""


class UnsupportedVersionError(DecryptionError):
    """Ciphertext claims a version we no longer accept. Defends against
    downgrade attacks where an attacker replays an old, compromised
    ciphertext after we've upgraded the cipher."""


# ═══════════════════════════════════════════════════════════════
# Result types — frozen so callers can't mutate ciphertext after creation
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class Sealed:
    """One AES-GCM ciphertext + its 12-byte IV. The 16-byte auth tag is
    appended to `ciphertext` by the cryptography library."""
    iv: bytes
    ciphertext: bytes

    def __post_init__(self):
        if len(self.iv) != _GCM_IV_BYTES:
            raise ValueError(f"iv must be {_GCM_IV_BYTES} bytes, got {len(self.iv)}")
        if len(self.ciphertext) < _GCM_TAG_BYTES:
            raise ValueError("ciphertext shorter than auth tag — invalid blob")


@dataclass(frozen=True, slots=True)
class WrappedDEK:
    """A Data Encryption Key, encrypted under the Master Key. Carries the
    encryption_version so we know which decrypt path to take."""
    iv: bytes
    ciphertext: bytes
    version: int


# ═══════════════════════════════════════════════════════════════
# CryptoProvider — the only public surface
# ═══════════════════════════════════════════════════════════════

class CryptoProvider:
    """Single source of truth for envelope encryption.

    Construct via `from_env()` in production. Tests instantiate directly
    with a known key for determinism.
    """

    def __init__(self, master_key: bytes):
        if not isinstance(master_key, (bytes, bytearray)):
            raise CryptoConfigError(
                f"master_key must be bytes, got {type(master_key).__name__}"
            )
        if len(master_key) != _MASTER_KEY_BYTES:
            raise CryptoConfigError(
                f"master_key must be {_MASTER_KEY_BYTES} bytes "
                f"(AES-256), got {len(master_key)}"
            )
        self._mk = AESGCM(bytes(master_key))

    # ───── construction ─────

    @classmethod
    def from_env(cls, env_var: str = _MK_ENV_VAR) -> "CryptoProvider":
        raw = os.getenv(env_var, "").strip()
        if not raw:
            raise CryptoConfigError(
                f"{env_var} is not set. Generate a 32-byte key:\n"
                f"  python -c 'import os,base64; "
                f"print(base64.urlsafe_b64encode(os.urandom(32)).decode())'"
            )
        # Accept both padded and unpadded urlsafe base64 — RFC 4648 §5
        # allows either. Operators commonly strip the trailing '=' when
        # placing keys in env-var dashboards.
        pad = "=" * ((4 - len(raw) % 4) % 4)
        try:
            mk = base64.urlsafe_b64decode(raw + pad)
        except Exception as e:
            raise CryptoConfigError(
                f"{env_var} is not valid urlsafe base64: {e}"
            ) from e
        return cls(mk)

    # ───── DEK lifecycle ─────

    @staticmethod
    def gen_dek() -> bytes:
        """Cryptographically random 32-byte DEK. Use ONCE per logical row,
        never reuse across rows or fields."""
        return secrets.token_bytes(_DEK_BYTES)

    def wrap_dek(self, dek: bytes, *, aad: bytes = b"") -> WrappedDEK:
        """Encrypt a DEK under the Master Key. The version is bound into
        AAD so a stored ciphertext cannot be silently downgraded later."""
        if len(dek) != _DEK_BYTES:
            raise CryptoConfigError(
                f"DEK must be {_DEK_BYTES} bytes, got {len(dek)}"
            )
        iv = secrets.token_bytes(_GCM_IV_BYTES)
        full_aad = self._build_aad(aad, CURRENT_ENCRYPTION_VERSION)
        ct = self._mk.encrypt(iv, dek, full_aad)
        return WrappedDEK(iv=iv, ciphertext=ct, version=CURRENT_ENCRYPTION_VERSION)

    def unwrap_dek(self, wrapped: WrappedDEK, *, aad: bytes = b"") -> bytes:
        """Reverse wrap_dek. Raises UnsupportedVersionError on retired
        crypto, DecryptionError on tamper / wrong key / wrong AAD."""
        if wrapped.version not in SUPPORTED_DECRYPT_VERSIONS:
            raise UnsupportedVersionError(
                f"DEK version {wrapped.version} not in supported set "
                f"{sorted(SUPPORTED_DECRYPT_VERSIONS)}"
            )
        full_aad = self._build_aad(aad, wrapped.version)
        try:
            return self._mk.decrypt(wrapped.iv, wrapped.ciphertext, full_aad)
        except InvalidTag as e:
            raise DecryptionError(
                "DEK unwrap failed — tampered ciphertext, wrong MK, "
                "or AAD mismatch (cryptography won't tell us which)"
            ) from e

    # ───── token-level encryption (uses an unwrapped DEK) ─────

    @staticmethod
    def encrypt_with_dek(
        plaintext: bytes, dek: bytes, *, aad: bytes = b"",
    ) -> Sealed:
        """Encrypt plaintext under a DEK. Stamped with CURRENT_ENCRYPTION_VERSION
        in AAD — the caller must remember the version that was used (we store
        it once on the row, not per ciphertext)."""
        if len(dek) != _DEK_BYTES:
            raise CryptoConfigError(
                f"DEK must be {_DEK_BYTES} bytes, got {len(dek)}"
            )
        iv = secrets.token_bytes(_GCM_IV_BYTES)
        full_aad = CryptoProvider._build_aad(aad, CURRENT_ENCRYPTION_VERSION)
        ct = AESGCM(dek).encrypt(iv, plaintext, full_aad)
        return Sealed(iv=iv, ciphertext=ct)

    @staticmethod
    def decrypt_with_dek(
        sealed: Sealed, dek: bytes, version: int, *, aad: bytes = b"",
    ) -> bytes:
        """Reverse encrypt_with_dek. Refuses retired versions before any
        crypto work, so an attacker can't force us to even attempt a
        weak primitive."""
        if version not in SUPPORTED_DECRYPT_VERSIONS:
            raise UnsupportedVersionError(
                f"Token version {version} not in supported set "
                f"{sorted(SUPPORTED_DECRYPT_VERSIONS)}"
            )
        if len(dek) != _DEK_BYTES:
            raise CryptoConfigError(
                f"DEK must be {_DEK_BYTES} bytes, got {len(dek)}"
            )
        full_aad = CryptoProvider._build_aad(aad, version)
        try:
            return AESGCM(dek).decrypt(sealed.iv, sealed.ciphertext, full_aad)
        except InvalidTag as e:
            raise DecryptionError(
                "Token decrypt failed — tampered ciphertext, wrong DEK, "
                "or AAD mismatch"
            ) from e

    # ───── internals ─────

    @staticmethod
    def _build_aad(user_aad: bytes, version: int) -> bytes:
        """Prepend the version (2 bytes, big-endian) to caller AAD. The
        version is THUS authenticated — flipping it on storage invalidates
        the auth tag and triggers DecryptionError on read."""
        if version < 0 or version > 0xFFFF:
            raise CryptoConfigError(f"version {version} out of u16 range")
        return version.to_bytes(2, "big") + b"|" + user_aad
