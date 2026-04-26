"""
Standalone runner for siyadah_crypto.py — bypasses pytest/conftest's
DB requirements. Runs every assertion in tests/test_phase_9_crypto.py
in-process and prints PASS/FAIL with a structured summary.

    .venv_test/bin/python tests/run_crypto_self_check.py

Each function is a single assertion or a tightly-scoped scenario; failures
print the offending test name and exception. Exit code = number of fails.
"""
from __future__ import annotations

import base64
import os
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

MK = b"\x42" * 32
ALT_MK = b"\x99" * 32


def _flip(blob: bytes, idx: int = 0) -> bytes:
    arr = bytearray(blob)
    arr[idx] ^= 0x01
    return bytes(arr)


def _provider(mk: bytes = MK) -> CryptoProvider:
    return CryptoProvider(mk)


@contextmanager
def _env(var: str, value):
    prev = os.environ.get(var)
    if value is None:
        os.environ.pop(var, None)
    else:
        os.environ[var] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = prev


def _expect_raises(exc_type, fn, *args, **kw):
    try:
        fn(*args, **kw)
    except exc_type:
        return True
    except BaseException as e:
        raise AssertionError(
            f"expected {exc_type.__name__}, got {type(e).__name__}: {e}"
        ) from None
    raise AssertionError(f"expected {exc_type.__name__}, no exception raised")


# ═══════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════

def t_construct_ok():
    assert isinstance(_provider(), CryptoProvider)


def t_short_mk_rejected():
    _expect_raises(CryptoConfigError, CryptoProvider, b"\x01" * 16)


def t_long_mk_rejected():
    _expect_raises(CryptoConfigError, CryptoProvider, b"\x01" * 64)


def t_non_bytes_mk_rejected():
    _expect_raises(CryptoConfigError, CryptoProvider, "not-bytes")


def t_from_env_missing():
    with _env("SIYADAH_OAUTH_MK", None):
        _expect_raises(CryptoConfigError, CryptoProvider.from_env)


def t_from_env_bad_b64():
    with _env("SIYADAH_OAUTH_MK", "not base64 ###"):
        _expect_raises(CryptoConfigError, CryptoProvider.from_env)


def t_from_env_short_decoded():
    short = base64.urlsafe_b64encode(b"\x01" * 16).decode()
    with _env("SIYADAH_OAUTH_MK", short):
        _expect_raises(CryptoConfigError, CryptoProvider.from_env)


def t_from_env_correct():
    good = base64.urlsafe_b64encode(b"\x07" * 32).decode()
    with _env("SIYADAH_OAUTH_MK", good):
        assert isinstance(CryptoProvider.from_env(), CryptoProvider)


def t_dek_roundtrip():
    p = _provider()
    dek = p.gen_dek()
    assert len(dek) == 32
    assert p.unwrap_dek(p.wrap_dek(dek)) == dek


def t_dek_nondeterministic():
    p = _provider()
    dek = p.gen_dek()
    w1, w2 = p.wrap_dek(dek), p.wrap_dek(dek)
    assert w1.ciphertext != w2.ciphertext
    assert w1.iv != w2.iv
    assert p.unwrap_dek(w1) == p.unwrap_dek(w2) == dek


def t_dek_aad_roundtrip():
    p = _provider()
    dek = p.gen_dek()
    aad = b"tenant_X|google|saga_Y"
    w = p.wrap_dek(dek, aad=aad)
    assert p.unwrap_dek(w, aad=aad) == dek


def t_dek_aad_mismatch():
    p = _provider()
    dek = p.gen_dek()
    w = p.wrap_dek(dek, aad=b"tenant_X|google")
    _expect_raises(DecryptionError, p.unwrap_dek, w, aad=b"tenant_Y|google")


def t_dek_wrong_size_rejected():
    p = _provider()
    _expect_raises(CryptoConfigError, p.wrap_dek, b"\x01" * 16)


def t_tamper_dek_ciphertext():
    p = _provider()
    dek = p.gen_dek()
    w = p.wrap_dek(dek)
    bad = WrappedDEK(iv=w.iv, ciphertext=_flip(w.ciphertext, 0), version=w.version)
    _expect_raises(DecryptionError, p.unwrap_dek, bad)


def t_tamper_dek_iv():
    p = _provider()
    dek = p.gen_dek()
    w = p.wrap_dek(dek)
    bad = WrappedDEK(iv=_flip(w.iv, 5), ciphertext=w.ciphertext, version=w.version)
    _expect_raises(DecryptionError, p.unwrap_dek, bad)


def t_tamper_auth_tag():
    p = _provider()
    dek = p.gen_dek()
    w = p.wrap_dek(dek)
    bad = WrappedDEK(
        iv=w.iv,
        ciphertext=_flip(w.ciphertext, len(w.ciphertext) - 1),
        version=w.version,
    )
    _expect_raises(DecryptionError, p.unwrap_dek, bad)


def t_wrong_mk():
    p1 = _provider(MK)
    p2 = _provider(ALT_MK)
    w = p1.wrap_dek(p1.gen_dek())
    _expect_raises(DecryptionError, p2.unwrap_dek, w)


def t_dek_version_downgrade():
    p = _provider()
    real = p.wrap_dek(p.gen_dek())
    forged = WrappedDEK(iv=real.iv, ciphertext=real.ciphertext, version=999)
    _expect_raises(UnsupportedVersionError, p.unwrap_dek, forged)


def t_dek_version_authenticated():
    """If we ever support v=2, claiming v=2 on a v=1 ciphertext must fail."""
    import siyadah_crypto as sc
    saved = sc.SUPPORTED_DECRYPT_VERSIONS
    try:
        sc.SUPPORTED_DECRYPT_VERSIONS = frozenset({1, 2})
        p = _provider()
        real = p.wrap_dek(p.gen_dek())
        spoofed = WrappedDEK(iv=real.iv, ciphertext=real.ciphertext, version=2)
        _expect_raises(DecryptionError, p.unwrap_dek, spoofed)
    finally:
        sc.SUPPORTED_DECRYPT_VERSIONS = saved


def t_token_roundtrip():
    p = _provider()
    dek = p.gen_dek()
    pt = b"ya29.A0AfH6SMBxxx"
    s = p.encrypt_with_dek(pt, dek)
    assert p.decrypt_with_dek(s, dek, CURRENT_ENCRYPTION_VERSION) == pt


def t_token_aad_roundtrip():
    p = _provider()
    dek = p.gen_dek()
    aad = b"tenant_X|google|access"
    s = p.encrypt_with_dek(b"refresh-tok", dek, aad=aad)
    assert p.decrypt_with_dek(s, dek, CURRENT_ENCRYPTION_VERSION, aad=aad) == b"refresh-tok"


def t_token_aad_swap():
    """Attacker swaps access ciphertext into refresh slot."""
    p = _provider()
    dek = p.gen_dek()
    s = p.encrypt_with_dek(b"sec", dek, aad=b"tenant_X|google|access")
    _expect_raises(
        DecryptionError, p.decrypt_with_dek, s, dek, CURRENT_ENCRYPTION_VERSION,
        aad=b"tenant_X|google|refresh",
    )


def t_token_wrong_dek():
    p = _provider()
    s = p.encrypt_with_dek(b"sec", p.gen_dek())
    _expect_raises(
        DecryptionError, p.decrypt_with_dek, s, p.gen_dek(),
        CURRENT_ENCRYPTION_VERSION,
    )


def t_token_tamper_ct():
    p = _provider()
    dek = p.gen_dek()
    s = p.encrypt_with_dek(b"sec", dek)
    bad = Sealed(iv=s.iv, ciphertext=_flip(s.ciphertext))
    _expect_raises(DecryptionError, p.decrypt_with_dek, bad, dek, CURRENT_ENCRYPTION_VERSION)


def t_token_tamper_iv():
    p = _provider()
    dek = p.gen_dek()
    s = p.encrypt_with_dek(b"sec", dek)
    bad = Sealed(iv=_flip(s.iv), ciphertext=s.ciphertext)
    _expect_raises(DecryptionError, p.decrypt_with_dek, bad, dek, CURRENT_ENCRYPTION_VERSION)


def t_token_bad_version():
    p = _provider()
    dek = p.gen_dek()
    s = p.encrypt_with_dek(b"sec", dek)
    _expect_raises(UnsupportedVersionError, p.decrypt_with_dek, s, dek, 42)


def t_token_nondeterministic():
    p = _provider()
    dek = p.gen_dek()
    s1 = p.encrypt_with_dek(b"same", dek)
    s2 = p.encrypt_with_dek(b"same", dek)
    assert s1.ciphertext != s2.ciphertext
    assert s1.iv != s2.iv


def t_empty_plaintext():
    p = _provider()
    dek = p.gen_dek()
    s = p.encrypt_with_dek(b"", dek)
    assert p.decrypt_with_dek(s, dek, CURRENT_ENCRYPTION_VERSION) == b""


def t_large_plaintext():
    p = _provider()
    dek = p.gen_dek()
    big = os.urandom(65536)
    s = p.encrypt_with_dek(big, dek)
    assert p.decrypt_with_dek(s, dek, CURRENT_ENCRYPTION_VERSION) == big


def t_sealed_short_iv():
    _expect_raises(ValueError, Sealed, iv=b"\x00" * 8, ciphertext=b"\x00" * 32)


def t_sealed_short_ct():
    _expect_raises(ValueError, Sealed, iv=b"\x00" * 12, ciphertext=b"\x00" * 4)


def t_full_envelope():
    """The exact pattern OAuth code will use end-to-end."""
    p = _provider()
    row_aad = b"tenant_42|google|saga_abc"
    dek = p.gen_dek()
    wrapped = p.wrap_dek(dek, aad=row_aad)
    sa = p.encrypt_with_dek(b"access-XXX", dek, aad=row_aad + b"|access")
    sr = p.encrypt_with_dek(b"refresh-YYY", dek, aad=row_aad + b"|refresh")
    version = wrapped.version
    # caller drops dek from memory
    dek = b""  # noqa: F841
    # READ
    dek2 = p.unwrap_dek(
        WrappedDEK(iv=wrapped.iv, ciphertext=wrapped.ciphertext, version=version),
        aad=row_aad,
    )
    a = p.decrypt_with_dek(Sealed(sa.iv, sa.ciphertext), dek2, version, aad=row_aad + b"|access")
    r = p.decrypt_with_dek(Sealed(sr.iv, sr.ciphertext), dek2, version, aad=row_aad + b"|refresh")
    assert a == b"access-XXX" and r == b"refresh-YYY"


def t_cross_tenant_rejected():
    p = _provider()
    aad_A = b"tenant_A|google|saga_X"
    aad_B = b"tenant_B|google|saga_X"
    w = p.wrap_dek(p.gen_dek(), aad=aad_A)
    _expect_raises(DecryptionError, p.unwrap_dek, w, aad=aad_B)


def t_invariant_current_in_supported():
    assert CURRENT_ENCRYPTION_VERSION in SUPPORTED_DECRYPT_VERSIONS


# ═══════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════

ALL_TESTS = [
    t_construct_ok, t_short_mk_rejected, t_long_mk_rejected, t_non_bytes_mk_rejected,
    t_from_env_missing, t_from_env_bad_b64, t_from_env_short_decoded, t_from_env_correct,
    t_dek_roundtrip, t_dek_nondeterministic, t_dek_aad_roundtrip,
    t_dek_aad_mismatch, t_dek_wrong_size_rejected,
    t_tamper_dek_ciphertext, t_tamper_dek_iv, t_tamper_auth_tag,
    t_wrong_mk, t_dek_version_downgrade, t_dek_version_authenticated,
    t_token_roundtrip, t_token_aad_roundtrip, t_token_aad_swap,
    t_token_wrong_dek, t_token_tamper_ct, t_token_tamper_iv,
    t_token_bad_version, t_token_nondeterministic,
    t_empty_plaintext, t_large_plaintext,
    t_sealed_short_iv, t_sealed_short_ct,
    t_full_envelope, t_cross_tenant_rejected,
    t_invariant_current_in_supported,
]

CATEGORIES = {
    "Construction / config":       (t_construct_ok, t_short_mk_rejected,
                                    t_long_mk_rejected, t_non_bytes_mk_rejected,
                                    t_from_env_missing, t_from_env_bad_b64,
                                    t_from_env_short_decoded, t_from_env_correct),
    "DEK lifecycle":               (t_dek_roundtrip, t_dek_nondeterministic,
                                    t_dek_aad_roundtrip, t_dek_aad_mismatch,
                                    t_dek_wrong_size_rejected),
    "DEK tamper detection":        (t_tamper_dek_ciphertext, t_tamper_dek_iv,
                                    t_tamper_auth_tag, t_wrong_mk,
                                    t_dek_version_downgrade,
                                    t_dek_version_authenticated),
    "Token-level encryption":      (t_token_roundtrip, t_token_aad_roundtrip,
                                    t_token_aad_swap, t_token_wrong_dek,
                                    t_token_tamper_ct, t_token_tamper_iv,
                                    t_token_bad_version, t_token_nondeterministic),
    "Boundary conditions":         (t_empty_plaintext, t_large_plaintext,
                                    t_sealed_short_iv, t_sealed_short_ct),
    "End-to-end envelope":         (t_full_envelope, t_cross_tenant_rejected,
                                    t_invariant_current_in_supported),
}


def main():
    fails = 0
    total = 0
    for cat, tests in CATEGORIES.items():
        print(f"\n── {cat} ──")
        for fn in tests:
            total += 1
            name = fn.__name__.removeprefix("t_")
            try:
                fn()
                print(f"   ✓ {name}")
            except BaseException as e:
                fails += 1
                print(f"   ✗ {name}")
                print(f"        {type(e).__name__}: {e}")
                tb = traceback.format_exc()
                # Last 4 lines of tb for context
                print("        " + tb.strip().splitlines()[-1])
    print(f"\n{'═' * 60}")
    print(f"  {total - fails}/{total} crypto assertions passed")
    print(f"{'═' * 60}")
    return fails


if __name__ == "__main__":
    sys.exit(main())
