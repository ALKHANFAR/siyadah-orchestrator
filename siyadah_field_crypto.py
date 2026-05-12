"""
siyadah_field_crypto — short-string field-level encryption (Wave 3C).

Sits on top of `siyadah_crypto` and reuses the OAuth master key
(SIYADAH_OAUTH_MK). For short-lived secrets like `oauth_sagas.pkce_verifier`
that are < 200 bytes and live < 10 minutes, envelope encryption (DEK + MK)
is overkill — we encrypt directly under the MK and store a single
self-describing string.

Output format (fits in a TEXT column):
    v1:<iv-b64>:<ciphertext-b64>

The AAD binds the ciphertext to a column-name context, so accidentally
re-using a pkce_verifier ciphertext in a different column (or in a
backup-swap forensic scenario) cannot decrypt — fail-loud.

Plaintext passthrough: `decrypt_field()` recognises legacy plaintext
(missing the `v1:` prefix) and returns it unchanged. This lets us
roll out the encryption to new sagas while existing in-flight sagas
that still hold plaintext finish naturally.

Usage:
    from siyadah_field_crypto import encrypt_field, decrypt_field

    # At INSERT:
    saga.pkce_verifier = encrypt_field(verifier, aad="oauth_sagas.pkce_verifier")

    # At SELECT:
    verifier = decrypt_field(saga.pkce_verifier, aad="oauth_sagas.pkce_verifier")
"""
from __future__ import annotations

import base64
import os
import secrets
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from siyadah_crypto import (
    CryptoConfigError,
    DecryptionError,
    _GCM_IV_BYTES,
    _MASTER_KEY_BYTES,
)

_CURRENT_VERSION = "v1"
_SUPPORTED_VERSIONS = frozenset({"v1"})

_mk_cache: Optional[AESGCM] = None


def _load_mk() -> AESGCM:
    global _mk_cache
    if _mk_cache is not None:
        return _mk_cache
    raw = os.environ.get("SIYADAH_OAUTH_MK", "").strip()
    if not raw:
        raise CryptoConfigError(
            "SIYADAH_OAUTH_MK is not set — field-level encryption disabled."
        )
    try:
        decoded = base64.b64decode(raw)
    except Exception as exc:
        raise CryptoConfigError(
            f"SIYADAH_OAUTH_MK is not valid base64: {exc}"
        ) from exc
    if len(decoded) != _MASTER_KEY_BYTES:
        raise CryptoConfigError(
            f"SIYADAH_OAUTH_MK must decode to {_MASTER_KEY_BYTES} bytes, "
            f"got {len(decoded)}"
        )
    _mk_cache = AESGCM(decoded)
    return _mk_cache


def is_field_crypto_enabled() -> bool:
    """True when the OAuth master key is configured. Callers should
    gate encrypted-write paths on this flag so a missing key fails
    loud at INSERT, not at the next pod restart."""
    return bool(os.environ.get("SIYADAH_OAUTH_MK", "").strip())


def encrypt_field(plaintext: str, *, aad: str) -> str:
    """Encrypt a UTF-8 string. Returns `v1:<iv-b64>:<ct-b64>`.

    The AAD is mandatory — pass a stable column-name string like
    "oauth_sagas.pkce_verifier". Cross-column ciphertext swaps fail
    decryption.
    """
    if not is_field_crypto_enabled():
        raise CryptoConfigError(
            "encrypt_field called but SIYADAH_OAUTH_MK is not set"
        )
    mk = _load_mk()
    iv = secrets.token_bytes(_GCM_IV_BYTES)
    ct = mk.encrypt(iv, plaintext.encode("utf-8"), aad.encode("utf-8"))
    return "{ver}:{iv}:{ct}".format(
        ver=_CURRENT_VERSION,
        iv=base64.b64encode(iv).decode("ascii"),
        ct=base64.b64encode(ct).decode("ascii"),
    )


def decrypt_field(value: Optional[str], *, aad: str) -> Optional[str]:
    """Decrypt a value produced by `encrypt_field`. Returns the UTF-8
    plaintext.

    Legacy plaintext (no version prefix) passes through unchanged so a
    rolling deploy doesn't corrupt in-flight sagas.

    Raises DecryptionError on tag-mismatch / wrong AAD / tampering.
    """
    if value is None:
        return None
    if not value.startswith("v"):
        return value  # legacy plaintext
    parts = value.split(":", 2)
    if len(parts) != 3:
        return value  # malformed → treat as plaintext
    version, iv_b64, ct_b64 = parts
    if version not in _SUPPORTED_VERSIONS:
        raise DecryptionError(f"unsupported field-crypto version: {version}")
    try:
        iv = base64.b64decode(iv_b64)
        ct = base64.b64decode(ct_b64)
    except Exception:
        return value  # not actually base64 → treat as plaintext
    if len(iv) != _GCM_IV_BYTES:
        return value
    mk = _load_mk()
    try:
        pt = mk.decrypt(iv, ct, aad.encode("utf-8"))
    except InvalidTag as exc:
        raise DecryptionError(
            "decrypt_field: tag mismatch — tampered ciphertext, "
            "wrong AAD, or wrong master key"
        ) from exc
    return pt.decode("utf-8")


def is_encrypted(value: Optional[str]) -> bool:
    """Heuristic: does this value look like a v1 ciphertext?"""
    if not value:
        return False
    parts = value.split(":", 2)
    return (
        len(parts) == 3
        and parts[0] in _SUPPORTED_VERSIONS
        and len(parts[1]) > 0
        and len(parts[2]) > 0
    )
