"""
Phase-9 — siyadah_crypto.py (Envelope Encryption / Layer 2 of OAuth)
=====================================================================

These tests exercise the Sniper-style assertions on AES-GCM-256:

  * Round-trip correctness (DEK + token level)
  * Non-determinism (every encrypt yields a fresh IV)
  * Tamper detection on EVERY mutable field (ciphertext byte / IV byte /
    wrapped_dek byte / version field / AAD).
  * Crypto agility — version mismatch is rejected before any crypto runs.
  * Boundary conditions — empty plaintext, large plaintext, wrong key
    sizes, env-var failure modes.

No DB, no network. Pure cryptographic property tests.
"""
from __future__ import annotations

import base64
import os

import pytest

from siyadah_crypto import (
    CURRENT_ENCRYPTION_VERSION,
    SUPPORTED_DECRYPT_VERSIONS,
    CryptoConfigError,
    CryptoProvider,
    DecryptionError,
    Sealed,
    UnsupportedVersionError,
    WrappedDEK,
)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

MK = b"\x42" * 32       # deterministic 32-byte master key for tests
ALT_MK = b"\x99" * 32   # a *different* master key for "wrong key" tests


def _flip_byte(blob: bytes, idx: int = 0) -> bytes:
    """Flip the low bit of one byte — minimum tamper to trigger InvalidTag."""
    arr = bytearray(blob)
    arr[idx] ^= 0x01
    return bytes(arr)


def _provider(mk: bytes = MK) -> CryptoProvider:
    return CryptoProvider(mk)


# ═══════════════════════════════════════════════════════════════
# Construction / config
# ═══════════════════════════════════════════════════════════════

def test_construct_with_correct_key_succeeds():
    p = _provider()
    assert isinstance(p, CryptoProvider)


def test_short_master_key_rejected():
    with pytest.raises(CryptoConfigError, match="32 bytes"):
        CryptoProvider(b"\x01" * 16)


def test_long_master_key_rejected():
    with pytest.raises(CryptoConfigError, match="32 bytes"):
        CryptoProvider(b"\x01" * 64)


def test_non_bytes_master_key_rejected():
    with pytest.raises(CryptoConfigError, match="bytes"):
        CryptoProvider("string-key-not-bytes")  # type: ignore[arg-type]


def test_from_env_missing_var(monkeypatch):
    monkeypatch.delenv("SIYADAH_OAUTH_MK", raising=False)
    with pytest.raises(CryptoConfigError, match="not set"):
        CryptoProvider.from_env()


def test_from_env_invalid_base64(monkeypatch):
    monkeypatch.setenv("SIYADAH_OAUTH_MK", "this is not base64 ###")
    with pytest.raises(CryptoConfigError, match="base64"):
        CryptoProvider.from_env()


def test_from_env_wrong_length_after_decode(monkeypatch):
    # Valid base64 but decodes to 16 bytes, not 32
    short_key = base64.urlsafe_b64encode(b"\x01" * 16).decode()
    monkeypatch.setenv("SIYADAH_OAUTH_MK", short_key)
    with pytest.raises(CryptoConfigError, match="32 bytes"):
        CryptoProvider.from_env()


def test_from_env_correct_key(monkeypatch):
    good_key = base64.urlsafe_b64encode(b"\x07" * 32).decode()
    monkeypatch.setenv("SIYADAH_OAUTH_MK", good_key)
    p = CryptoProvider.from_env()
    assert isinstance(p, CryptoProvider)


# ═══════════════════════════════════════════════════════════════
# DEK lifecycle — wrap / unwrap
# ═══════════════════════════════════════════════════════════════

def test_dek_roundtrip():
    p = _provider()
    dek = p.gen_dek()
    assert len(dek) == 32
    wrapped = p.wrap_dek(dek)
    recovered = p.unwrap_dek(wrapped)
    assert recovered == dek


def test_dek_wrap_is_nondeterministic():
    """Same DEK + same MK + two wraps → two different ciphertexts."""
    p = _provider()
    dek = p.gen_dek()
    w1 = p.wrap_dek(dek)
    w2 = p.wrap_dek(dek)
    assert w1.ciphertext != w2.ciphertext, "GCM IV reuse — catastrophic"
    assert w1.iv != w2.iv, "IV must be fresh per call"
    # both still decrypt to the same plaintext
    assert p.unwrap_dek(w1) == p.unwrap_dek(w2) == dek


def test_dek_with_aad_roundtrip():
    p = _provider()
    dek = p.gen_dek()
    aad = b"tenant_X|google|saga_Y"
    wrapped = p.wrap_dek(dek, aad=aad)
    assert p.unwrap_dek(wrapped, aad=aad) == dek


def test_dek_aad_mismatch_rejected():
    p = _provider()
    dek = p.gen_dek()
    wrapped = p.wrap_dek(dek, aad=b"tenant_X|google")
    with pytest.raises(DecryptionError):
        p.unwrap_dek(wrapped, aad=b"tenant_Y|google")  # wrong tenant


def test_dek_wrong_key_size_rejected():
    p = _provider()
    with pytest.raises(CryptoConfigError, match="32 bytes"):
        p.wrap_dek(b"\x01" * 16)


# ═══════════════════════════════════════════════════════════════
# DEK tamper detection — flipping any byte invalidates auth tag
# ═══════════════════════════════════════════════════════════════

def test_tamper_dek_ciphertext_detected():
    p = _provider()
    dek = p.gen_dek()
    wrapped = p.wrap_dek(dek)
    # mutate one byte in the wrapped DEK ciphertext
    bad = WrappedDEK(
        iv=wrapped.iv,
        ciphertext=_flip_byte(wrapped.ciphertext, idx=0),
        version=wrapped.version,
    )
    with pytest.raises(DecryptionError):
        p.unwrap_dek(bad)


def test_tamper_dek_iv_detected():
    p = _provider()
    dek = p.gen_dek()
    wrapped = p.wrap_dek(dek)
    bad = WrappedDEK(
        iv=_flip_byte(wrapped.iv, idx=5),
        ciphertext=wrapped.ciphertext,
        version=wrapped.version,
    )
    with pytest.raises(DecryptionError):
        p.unwrap_dek(bad)


def test_tamper_dek_auth_tag_detected():
    """The last 16 bytes of GCM ciphertext are the auth tag — flipping
    them must trigger InvalidTag → DecryptionError."""
    p = _provider()
    dek = p.gen_dek()
    wrapped = p.wrap_dek(dek)
    last_idx = len(wrapped.ciphertext) - 1
    bad = WrappedDEK(
        iv=wrapped.iv,
        ciphertext=_flip_byte(wrapped.ciphertext, idx=last_idx),
        version=wrapped.version,
    )
    with pytest.raises(DecryptionError):
        p.unwrap_dek(bad)


def test_wrong_master_key_detected():
    """Wrap with one MK, try to unwrap with another → InvalidTag."""
    p1 = _provider(MK)
    p2 = _provider(ALT_MK)
    dek = p1.gen_dek()
    wrapped = p1.wrap_dek(dek)
    with pytest.raises(DecryptionError):
        p2.unwrap_dek(wrapped)


def test_dek_version_downgrade_rejected():
    """If we ever stamp a v0 ciphertext (or v999), unwrap refuses before
    even attempting the AESGCM call. Defends against downgrade attacks."""
    p = _provider()
    dek = p.gen_dek()
    real = p.wrap_dek(dek)
    forged = WrappedDEK(iv=real.iv, ciphertext=real.ciphertext, version=999)
    with pytest.raises(UnsupportedVersionError, match="999"):
        p.unwrap_dek(forged)


def test_dek_version_field_is_authenticated():
    """The version is part of AAD — claiming v=2 on a v=1 ciphertext (if
    we ever support v=2) must fail decryption, not silently succeed."""
    # We only have v=1 today. Force a hypothetical v=2 unwrap by adding
    # it to SUPPORTED_DECRYPT_VERSIONS for the duration of this test.
    import siyadah_crypto as sc
    saved = sc.SUPPORTED_DECRYPT_VERSIONS
    try:
        sc.SUPPORTED_DECRYPT_VERSIONS = frozenset({1, 2})  # type: ignore[misc]
        p = _provider()
        dek = p.gen_dek()
        real = p.wrap_dek(dek)               # stamped v=1
        spoofed = WrappedDEK(iv=real.iv, ciphertext=real.ciphertext, version=2)
        with pytest.raises(DecryptionError):
            p.unwrap_dek(spoofed)            # v=2 in AAD → tag mismatch
    finally:
        sc.SUPPORTED_DECRYPT_VERSIONS = saved  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════
# Token-level encryption (encrypt_with_dek / decrypt_with_dek)
# ═══════════════════════════════════════════════════════════════

def test_token_roundtrip():
    p = _provider()
    dek = p.gen_dek()
    plaintext = b"ya29.A0AfH6SMBxxxxxxxxxxxxxxxxxxxxxxxxxx"
    sealed = p.encrypt_with_dek(plaintext, dek)
    recovered = p.decrypt_with_dek(sealed, dek, CURRENT_ENCRYPTION_VERSION)
    assert recovered == plaintext


def test_token_roundtrip_with_aad():
    p = _provider()
    dek = p.gen_dek()
    plaintext = b"refresh-token-here"
    aad = b"tenant_X|google|access"
    sealed = p.encrypt_with_dek(plaintext, dek, aad=aad)
    out = p.decrypt_with_dek(sealed, dek, CURRENT_ENCRYPTION_VERSION, aad=aad)
    assert out == plaintext


def test_token_aad_swap_rejected():
    """A real attacker swap: take the access_token ciphertext and try to
    decrypt it as if it were the refresh_token (different AAD)."""
    p = _provider()
    dek = p.gen_dek()
    sealed = p.encrypt_with_dek(b"access-secret", dek, aad=b"tenant_X|google|access")
    with pytest.raises(DecryptionError):
        # swap field-tag in AAD from "access" to "refresh"
        p.decrypt_with_dek(
            sealed, dek, CURRENT_ENCRYPTION_VERSION,
            aad=b"tenant_X|google|refresh",
        )


def test_token_wrong_dek_rejected():
    p = _provider()
    dek1 = p.gen_dek()
    dek2 = p.gen_dek()
    sealed = p.encrypt_with_dek(b"secret", dek1)
    with pytest.raises(DecryptionError):
        p.decrypt_with_dek(sealed, dek2, CURRENT_ENCRYPTION_VERSION)


def test_token_tamper_ciphertext_detected():
    p = _provider()
    dek = p.gen_dek()
    sealed = p.encrypt_with_dek(b"important secret", dek)
    bad = Sealed(iv=sealed.iv, ciphertext=_flip_byte(sealed.ciphertext))
    with pytest.raises(DecryptionError):
        p.decrypt_with_dek(bad, dek, CURRENT_ENCRYPTION_VERSION)


def test_token_tamper_iv_detected():
    p = _provider()
    dek = p.gen_dek()
    sealed = p.encrypt_with_dek(b"important secret", dek)
    bad = Sealed(iv=_flip_byte(sealed.iv), ciphertext=sealed.ciphertext)
    with pytest.raises(DecryptionError):
        p.decrypt_with_dek(bad, dek, CURRENT_ENCRYPTION_VERSION)


def test_token_unsupported_version_rejected():
    p = _provider()
    dek = p.gen_dek()
    sealed = p.encrypt_with_dek(b"secret", dek)
    with pytest.raises(UnsupportedVersionError):
        p.decrypt_with_dek(sealed, dek, version=42)


def test_token_nondeterministic():
    p = _provider()
    dek = p.gen_dek()
    s1 = p.encrypt_with_dek(b"identical plaintext", dek)
    s2 = p.encrypt_with_dek(b"identical plaintext", dek)
    assert s1.ciphertext != s2.ciphertext, "GCM IV reuse — catastrophic"
    assert s1.iv != s2.iv


# ═══════════════════════════════════════════════════════════════
# Boundary conditions
# ═══════════════════════════════════════════════════════════════

def test_empty_plaintext_roundtrip():
    """Edge case: empty token (shouldn't happen but must not crash)."""
    p = _provider()
    dek = p.gen_dek()
    sealed = p.encrypt_with_dek(b"", dek)
    assert p.decrypt_with_dek(sealed, dek, CURRENT_ENCRYPTION_VERSION) == b""


def test_large_plaintext_roundtrip():
    """64KB plaintext — well above any realistic OAuth token size."""
    p = _provider()
    dek = p.gen_dek()
    big = os.urandom(65536)
    sealed = p.encrypt_with_dek(big, dek)
    assert p.decrypt_with_dek(sealed, dek, CURRENT_ENCRYPTION_VERSION) == big


def test_sealed_rejects_short_iv():
    """The dataclass guard catches malformed IV at construction time."""
    with pytest.raises(ValueError, match="iv must be 12"):
        Sealed(iv=b"\x00" * 8, ciphertext=b"\x00" * 32)


def test_sealed_rejects_too_short_ciphertext():
    """Ciphertext shorter than the auth tag is structurally impossible."""
    with pytest.raises(ValueError, match="auth tag"):
        Sealed(iv=b"\x00" * 12, ciphertext=b"\x00" * 4)


# ═══════════════════════════════════════════════════════════════
# Full end-to-end envelope: wrap DEK, encrypt token, decrypt round-trip
# ═══════════════════════════════════════════════════════════════

def test_full_envelope_roundtrip_with_aad():
    """Reproduce the exact pattern OAuth code will use:
        1. Build per-row AAD (tenant_id|provider|saga_id)
        2. Generate a fresh DEK
        3. Wrap DEK under MK with row-AAD
        4. Use DEK to encrypt access_token (row-AAD + |access)
        5. Use SAME DEK to encrypt refresh_token (row-AAD + |refresh)
        6. Persist all 3 ciphertexts + 3 IVs + version
        7. Read back: unwrap DEK, decrypt both tokens
    """
    p = _provider()
    row_aad = b"tenant_42|google|saga_abc"

    # WRITE
    dek = p.gen_dek()
    wrapped = p.wrap_dek(dek, aad=row_aad)
    sealed_access = p.encrypt_with_dek(
        b"access-token-XXX", dek, aad=row_aad + b"|access",
    )
    sealed_refresh = p.encrypt_with_dek(
        b"refresh-token-YYY", dek, aad=row_aad + b"|refresh",
    )
    version = wrapped.version  # store on row

    # zero DEK in caller memory (simulate)
    dek = b""  # noqa: F841

    # READ — reconstruct dataclasses from "DB columns"
    wrapped2 = WrappedDEK(
        iv=wrapped.iv, ciphertext=wrapped.ciphertext, version=version,
    )
    dek2 = p.unwrap_dek(wrapped2, aad=row_aad)
    access = p.decrypt_with_dek(
        Sealed(iv=sealed_access.iv, ciphertext=sealed_access.ciphertext),
        dek2, version, aad=row_aad + b"|access",
    )
    refresh = p.decrypt_with_dek(
        Sealed(iv=sealed_refresh.iv, ciphertext=sealed_refresh.ciphertext),
        dek2, version, aad=row_aad + b"|refresh",
    )
    assert access == b"access-token-XXX"
    assert refresh == b"refresh-token-YYY"


def test_full_envelope_cross_tenant_rejected():
    """Attacker copies a row from tenant_A's table into tenant_B's. When
    tenant_B's process tries to read it (with row_aad bound to B), the
    AAD mismatch must trigger DecryptionError."""
    p = _provider()
    aad_A = b"tenant_A|google|saga_X"
    aad_B = b"tenant_B|google|saga_X"

    dek = p.gen_dek()
    wrapped = p.wrap_dek(dek, aad=aad_A)
    with pytest.raises(DecryptionError):
        p.unwrap_dek(wrapped, aad=aad_B)


def test_supported_versions_invariant():
    """Sanity: CURRENT_ENCRYPTION_VERSION must be in SUPPORTED set, else
    every freshly-encrypted blob would be undecryptable."""
    assert CURRENT_ENCRYPTION_VERSION in SUPPORTED_DECRYPT_VERSIONS
