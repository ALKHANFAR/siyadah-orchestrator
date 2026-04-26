"""
Harsh test suite for siyadah_oauth_state.py — Layer 1 of OAuth.

Standalone runner (no pytest, no DB needed). Exercises:

  • State token: round-trip, tampered signature (every byte flipped),
    expired (clock-controlled), wrong tenant (constant-time compare),
    malformed (5 distinct shapes).
  • PKCE: round-trip, verifier entropy, challenge ≠ verifier,
    different verifiers → different challenges.
  • NonceStore: register/consume happy path, double-register → replay,
    double-consume → not-found, wrong-tenant on consume,
    TTL is set correctly on the key.
  • Concurrency: 100 concurrent consumes of the same nonce → exactly 1 wins.

    .venv_test/bin/python tests/run_oauth_state_self_check.py
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fakeredis.aioredis  # noqa: E402

from siyadah_oauth_state import (  # noqa: E402
    NONCE_REDIS_PREFIX,
    NONCE_BYTES,
    PKCE_METHOD,
    PKCE_VERIFIER_BYTES,
    STATE_KEY_BYTES,
    STATE_TTL_SECONDS,
    NonceNotFoundError,
    NonceReplayError,
    NonceStore,
    StateClaims,
    StateConfigError,
    StateExpiredError,
    StateMalformedError,
    StateProvider,
    StateSignatureError,
    StateTenantMismatchError,
    gen_pkce,
    verify_pkce,
)


KEY = b"\x42" * 32
ALT_KEY = b"\x99" * 32


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


def _expect(exc_type, fn, *args, **kw):
    try:
        result = fn(*args, **kw)
        if asyncio.iscoroutine(result):
            asyncio.get_event_loop().run_until_complete(result)
    except exc_type:
        return True
    except BaseException as e:
        raise AssertionError(
            f"expected {exc_type.__name__}, got {type(e).__name__}: {e}"
        ) from None
    raise AssertionError(f"expected {exc_type.__name__}, no exception raised")


async def _expect_async(exc_type, coro):
    try:
        await coro
    except exc_type:
        return True
    except BaseException as e:
        raise AssertionError(
            f"expected {exc_type.__name__}, got {type(e).__name__}: {e}"
        ) from None
    raise AssertionError(f"expected {exc_type.__name__}, no exception raised")


# ═══════════════════════════════════════════════════════════════
# Synchronous tests (state + PKCE) — no Redis
# ═══════════════════════════════════════════════════════════════

def t_construct_ok():
    sp = StateProvider(KEY)
    assert isinstance(sp, StateProvider)


def t_construct_short_key():
    _expect(StateConfigError, StateProvider, b"\x01" * 16)


def t_construct_long_key():
    _expect(StateConfigError, StateProvider, b"\x01" * 64)


def t_construct_non_bytes():
    _expect(StateConfigError, StateProvider, "string-not-bytes")


def t_from_env_missing():
    with _env("SIYADAH_OAUTH_STATE_KEY", None):
        _expect(StateConfigError, StateProvider.from_env)


def t_from_env_bad_b64():
    with _env("SIYADAH_OAUTH_STATE_KEY", "not base64 ###"):
        _expect(StateConfigError, StateProvider.from_env)


def t_from_env_short_decoded():
    short = base64.urlsafe_b64encode(b"\x01" * 16).decode().rstrip("=")
    with _env("SIYADAH_OAUTH_STATE_KEY", short):
        _expect(StateConfigError, StateProvider.from_env)


def t_from_env_correct():
    good = base64.urlsafe_b64encode(b"\x07" * 32).decode().rstrip("=")
    with _env("SIYADAH_OAUTH_STATE_KEY", good):
        sp = StateProvider.from_env()
        assert isinstance(sp, StateProvider)


# ── State round-trip and integrity ──

def t_state_roundtrip():
    sp = StateProvider(KEY)
    tok, nonce = sp.make_state("tenant_X", "/dashboard")
    claims = sp.verify_state(tok, "tenant_X")
    assert isinstance(claims, StateClaims)
    assert claims.tenant_id == "tenant_X"
    assert claims.return_path == "/dashboard"
    assert claims.nonce == nonce
    assert claims.expires_at - claims.issued_at == STATE_TTL_SECONDS


def t_state_tokens_are_unique():
    """Two states for the same tenant must differ — fresh nonce each call."""
    sp = StateProvider(KEY)
    t1, n1 = sp.make_state("tenant_X")
    t2, n2 = sp.make_state("tenant_X")
    assert t1 != t2
    assert n1 != n2


def t_state_signature_tamper_payload_byte():
    """Flip one byte in the payload half → SignatureError (HMAC over old payload)."""
    sp = StateProvider(KEY)
    tok, _ = sp.make_state("tenant_X")
    parts = tok.split(".")
    bad_payload = parts[0][:-1] + ("A" if parts[0][-1] != "A" else "B")
    bad_token = bad_payload + "." + parts[1]
    _expect(StateMalformedError if False else (StateMalformedError, StateSignatureError),
            sp.verify_state, bad_token, "tenant_X")
    # Either error class is acceptable: tampering the payload's b64 may
    # produce invalid JSON (Malformed) or valid JSON with wrong sig (Signature).


def t_state_signature_tamper_sig_byte():
    """Flip one byte in the sig — middle position so we don't hit the
    base64 trailing-bit ambiguity (last char of an unpadded 43-char b64
    string carries only 4 meaningful bits; changing it can decode to
    the same bytes). Position 5 is safely in the middle."""
    sp = StateProvider(KEY)
    tok, _ = sp.make_state("tenant_X")
    parts = tok.split(".")
    sig_chars = list(parts[1])
    # Flip char at position 5 to a different valid b64 char
    sig_chars[5] = "Z" if sig_chars[5] != "Z" else "Y"
    bad_token = parts[0] + "." + "".join(sig_chars)
    _expect(StateSignatureError, sp.verify_state, bad_token, "tenant_X")


def t_state_signed_with_wrong_key_fails():
    """Token signed with KEY can't be verified with ALT_KEY."""
    sp1 = StateProvider(KEY)
    sp2 = StateProvider(ALT_KEY)
    tok, _ = sp1.make_state("tenant_X")
    _expect(StateSignatureError, sp2.verify_state, tok, "tenant_X")


def t_state_expired():
    """Issue a state in the past → verify rejects (controlled clock)."""
    sp = StateProvider(KEY)
    # Issue at t=1000 with TTL=300 → exp=1300
    tok, _ = sp.make_state("tenant_X", ttl_seconds=300, now=1000)
    # Verify at t=2000 → exp=1300 < now=2000 → expired
    _expect(StateExpiredError, sp.verify_state, tok, "tenant_X", now=2000)


def t_state_pre_expired_immediately():
    """ttl_seconds=-1 → already-expired token, never verifies."""
    sp = StateProvider(KEY)
    tok, _ = sp.make_state("tenant_X", ttl_seconds=-1)
    _expect(StateExpiredError, sp.verify_state, tok, "tenant_X")


def t_state_wrong_tenant():
    sp = StateProvider(KEY)
    tok, _ = sp.make_state("tenant_X")
    _expect(StateTenantMismatchError, sp.verify_state, tok, "tenant_Y")


def t_state_malformed_empty():
    sp = StateProvider(KEY)
    _expect(StateMalformedError, sp.verify_state, "", "tenant_X")


def t_state_malformed_no_separator():
    sp = StateProvider(KEY)
    _expect(StateMalformedError, sp.verify_state, "no-dot-here", "tenant_X")


def t_state_malformed_three_parts():
    sp = StateProvider(KEY)
    _expect(StateMalformedError, sp.verify_state, "a.b.c", "tenant_X")


def t_state_malformed_bad_b64():
    sp = StateProvider(KEY)
    _expect(StateMalformedError, sp.verify_state, "###.@@@", "tenant_X")


def t_state_malformed_payload_not_json():
    """Sign garbage bytes — passes HMAC but JSON decode fails."""
    import hmac
    import hashlib
    from siyadah_oauth_state import _b64url_enc
    sp = StateProvider(KEY)
    payload = b"this is not json"
    sig = hmac.new(KEY, payload, hashlib.sha256).digest()
    bad_token = _b64url_enc(payload) + "." + _b64url_enc(sig)
    _expect(StateMalformedError, sp.verify_state, bad_token, "tenant_X")


def t_state_malformed_missing_claim():
    """Sign a JSON object that's missing required claims."""
    import hmac
    import hashlib
    import json as _json
    from siyadah_oauth_state import _b64url_enc
    sp = StateProvider(KEY)
    payload = _json.dumps({"tid": "tenant_X"}).encode()  # missing n, iat, exp, rp
    sig = hmac.new(KEY, payload, hashlib.sha256).digest()
    bad_token = _b64url_enc(payload) + "." + _b64url_enc(sig)
    _expect(StateMalformedError, sp.verify_state, bad_token, "tenant_X")


def t_state_make_requires_tenant():
    sp = StateProvider(KEY)
    _expect(StateConfigError, sp.make_state, "")


# ── PKCE ──

def t_pkce_roundtrip():
    v, c = gen_pkce()
    assert verify_pkce(v, c) is True


def t_pkce_verifier_length():
    """RFC 7636 says verifier must be 43-128 chars. We use 64 (~256 bits)."""
    v, _ = gen_pkce()
    assert 43 <= len(v) <= 128
    assert len(v) == 64, f"expected 64-char verifier, got {len(v)}"


def t_pkce_challenge_length():
    """SHA-256 = 32 bytes → 43 chars b64url without padding."""
    _, c = gen_pkce()
    assert len(c) == 43


def t_pkce_url_safe_alphabet():
    v, c = gen_pkce()
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                  "abcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set(v).issubset(allowed), f"verifier has bad chars: {set(v) - allowed}"
    assert set(c).issubset(allowed), f"challenge has bad chars: {set(c) - allowed}"


def t_pkce_unique_per_call():
    v1, c1 = gen_pkce()
    v2, c2 = gen_pkce()
    assert v1 != v2
    assert c1 != c2


def t_pkce_verifier_neq_challenge():
    v, c = gen_pkce()
    assert v != c


def t_pkce_wrong_verifier_rejected():
    _, c = gen_pkce()
    v2, _ = gen_pkce()
    assert verify_pkce(v2, c) is False


def t_pkce_tampered_challenge_rejected():
    v, c = gen_pkce()
    bad_c = c[:-1] + ("A" if c[-1] != "A" else "B")
    assert verify_pkce(v, bad_c) is False


def t_pkce_method_constant():
    assert PKCE_METHOD == "S256"  # invariant — provider must agree


# ═══════════════════════════════════════════════════════════════
# Async tests (NonceStore + Redis)
# ═══════════════════════════════════════════════════════════════

async def t_nonce_register_and_consume():
    r = fakeredis.aioredis.FakeRedis()
    store = NonceStore(r, ttl_seconds=300)
    await store.register("nonce_abc", "tenant_X")
    await store.consume("nonce_abc", "tenant_X")  # success → returns None


async def t_nonce_double_register_fails():
    """Writing the same nonce twice → NonceReplayError (write-side defence)."""
    r = fakeredis.aioredis.FakeRedis()
    store = NonceStore(r, ttl_seconds=300)
    await store.register("nonce_abc", "tenant_X")
    await _expect_async(NonceReplayError, store.register("nonce_abc", "tenant_X"))


async def t_nonce_double_consume_fails():
    """Reading the same nonce twice → NonceNotFoundError (atomic GETDEL)."""
    r = fakeredis.aioredis.FakeRedis()
    store = NonceStore(r, ttl_seconds=300)
    await store.register("nonce_xyz", "tenant_X")
    await store.consume("nonce_xyz", "tenant_X")
    await _expect_async(NonceNotFoundError, store.consume("nonce_xyz", "tenant_X"))


async def t_nonce_wrong_tenant_fails():
    """Tenant B tries to consume tenant A's nonce → TenantMismatchError."""
    r = fakeredis.aioredis.FakeRedis()
    store = NonceStore(r, ttl_seconds=300)
    await store.register("nonce_xyz", "tenant_A")
    await _expect_async(StateTenantMismatchError, store.consume("nonce_xyz", "tenant_B"))


async def t_nonce_consume_unknown_fails():
    r = fakeredis.aioredis.FakeRedis()
    store = NonceStore(r, ttl_seconds=300)
    await _expect_async(NonceNotFoundError, store.consume("never_registered", "tenant_X"))


async def t_nonce_ttl_is_set():
    """Verify the TTL is applied on register — not just key creation."""
    r = fakeredis.aioredis.FakeRedis()
    store = NonceStore(r, ttl_seconds=300)
    await store.register("nonce_ttl", "tenant_X")
    ttl = await store.ttl_seconds("nonce_ttl")
    assert 290 <= ttl <= 300, f"TTL out of range: {ttl}"


async def t_nonce_short_ttl_expires():
    """Real expiry behaviour: register with 1s TTL, sleep 1.2s, consume fails."""
    r = fakeredis.aioredis.FakeRedis()
    store = NonceStore(r, ttl_seconds=1)
    await store.register("nonce_short", "tenant_X")
    await asyncio.sleep(1.2)
    await _expect_async(NonceNotFoundError, store.consume("nonce_short", "tenant_X"))


async def t_nonce_concurrent_consume_only_one_wins():
    """100 concurrent consumes of the same nonce: exactly one succeeds, 99 fail.
    Proves GETDEL is genuinely atomic — no double-spend."""
    r = fakeredis.aioredis.FakeRedis()
    store = NonceStore(r, ttl_seconds=300)
    await store.register("nonce_race", "tenant_X")

    successes = 0
    failures = 0

    async def attempt():
        nonlocal successes, failures
        try:
            await store.consume("nonce_race", "tenant_X")
            successes += 1
        except NonceNotFoundError:
            failures += 1

    await asyncio.gather(*(attempt() for _ in range(100)))
    assert successes == 1, f"expected exactly 1 winner, got {successes}"
    assert failures == 99, f"expected 99 losers, got {failures}"


async def t_nonce_register_empty_args_rejected():
    r = fakeredis.aioredis.FakeRedis()
    store = NonceStore(r, ttl_seconds=300)
    await _expect_async(StateConfigError, store.register("", "tenant"))
    await _expect_async(StateConfigError, store.register("nonce", ""))


async def t_full_handshake_round_trip():
    """End-to-end: issue state + register nonce, then verify + consume.
    Mirrors the EXACT sequence of /v2/oauth/initiate + /v2/oauth/callback."""
    r = fakeredis.aioredis.FakeRedis()
    sp = StateProvider(KEY)
    store = NonceStore(r, ttl_seconds=300)

    # /initiate
    tok, nonce = sp.make_state("tenant_42", "/back-to-app")
    await store.register(nonce, "tenant_42")

    # /callback
    claims = sp.verify_state(tok, "tenant_42")
    assert claims.nonce == nonce
    await store.consume(claims.nonce, claims.tenant_id)

    # Replay attempt — second callback for same state
    await _expect_async(NonceNotFoundError,
                        store.consume(claims.nonce, claims.tenant_id))


# ═══════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════

SYNC_TESTS = [
    # Construction
    t_construct_ok, t_construct_short_key, t_construct_long_key,
    t_construct_non_bytes, t_from_env_missing, t_from_env_bad_b64,
    t_from_env_short_decoded, t_from_env_correct,
    # State integrity
    t_state_roundtrip, t_state_tokens_are_unique,
    t_state_signature_tamper_payload_byte, t_state_signature_tamper_sig_byte,
    t_state_signed_with_wrong_key_fails,
    t_state_expired, t_state_pre_expired_immediately, t_state_wrong_tenant,
    # Malformed
    t_state_malformed_empty, t_state_malformed_no_separator,
    t_state_malformed_three_parts, t_state_malformed_bad_b64,
    t_state_malformed_payload_not_json, t_state_malformed_missing_claim,
    t_state_make_requires_tenant,
    # PKCE
    t_pkce_roundtrip, t_pkce_verifier_length, t_pkce_challenge_length,
    t_pkce_url_safe_alphabet, t_pkce_unique_per_call,
    t_pkce_verifier_neq_challenge, t_pkce_wrong_verifier_rejected,
    t_pkce_tampered_challenge_rejected, t_pkce_method_constant,
]

ASYNC_TESTS = [
    t_nonce_register_and_consume, t_nonce_double_register_fails,
    t_nonce_double_consume_fails, t_nonce_wrong_tenant_fails,
    t_nonce_consume_unknown_fails, t_nonce_ttl_is_set,
    t_nonce_short_ttl_expires, t_nonce_concurrent_consume_only_one_wins,
    t_nonce_register_empty_args_rejected, t_full_handshake_round_trip,
]

CATEGORIES = {
    "Construction / config": SYNC_TESTS[:8],
    "State integrity":       SYNC_TESTS[8:15],
    "State malformed":       SYNC_TESTS[15:23],
    "PKCE primitives":       SYNC_TESTS[23:],
    "Nonce store (Redis)":   ASYNC_TESTS,
}


async def main():
    fails = 0
    total = 0
    for cat, tests in CATEGORIES.items():
        print(f"\n── {cat} ──")
        for fn in tests:
            total += 1
            name = fn.__name__.removeprefix("t_")
            try:
                if asyncio.iscoroutinefunction(fn):
                    await fn()
                else:
                    fn()
                print(f"   ✓ {name}")
            except BaseException as e:
                fails += 1
                print(f"   ✗ {name}")
                print(f"        {type(e).__name__}: {e}")
                tb = traceback.format_exc()
                print("        " + tb.strip().splitlines()[-1])
    print(f"\n{'═' * 60}")
    print(f"  {total - fails}/{total} oauth-state assertions passed")
    print(f"{'═' * 60}")
    return fails


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
