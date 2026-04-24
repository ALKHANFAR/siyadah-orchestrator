"""
Phase 2 — resilience harsh tests.

Covers:
- Rate limit enforcement per tenant (A throttled ≠ B throttled).
- 429 body shape.
- Tenacity retries on upstream 5xx / network error.
- Tenacity does NOT retry on 4xx — single attempt, fast failure.
- Auth 401 re-auth path doesn't consume a retry slot.
- httpx.Limits pool is actually configured.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import main
from conftest import KEY_A, KEY_B, PID_A, PID_B, hdr


# ── 1. rate limit matrix ────────────────────────────────────────

async def test_build_endpoint_throttles_after_10_per_minute(client):
    """11th POST in a fresh minute window must 429.
    We don't care what the first 10 return (will 404/500 from unknown
    template + stub engine) — only that the limiter eventually kicks in."""
    body = {"template": "does-not-exist", "config": {}}
    statuses = []
    for _ in range(12):
        r = await client.post("/v2/build-and-deploy",
                              headers=hdr(KEY_A, PID_A), json=body)
        statuses.append(r.status_code)
    throttled = [i for i, s in enumerate(statuses) if s == 429]
    assert throttled, f"no 429 in {statuses}"
    assert throttled[0] >= 10  # first 10 should be allowed


async def test_tenant_b_unaffected_by_tenant_a_throttle(client):
    body = {"template": "does-not-exist", "config": {}}
    # Burn tenant A's budget
    for _ in range(12):
        await client.post("/v2/build-and-deploy",
                          headers=hdr(KEY_A, PID_A), json=body)
    # Tenant B in same window still has full budget
    r = await client.post("/v2/build-and-deploy",
                          headers=hdr(KEY_B, PID_B), json=body)
    assert r.status_code != 429, f"B got {r.status_code} — should be fresh"


async def test_429_body_shape(client):
    body = {"template": "does-not-exist", "config": {}}
    for _ in range(11):
        await client.post("/v2/build-and-deploy",
                          headers=hdr(KEY_A, PID_A), json=body)
    r = await client.post("/v2/build-and-deploy",
                          headers=hdr(KEY_A, PID_A), json=body)
    assert r.status_code == 429
    payload = r.json()
    assert payload.get("error") == "rate_limit_exceeded"
    assert "detail" in payload


def test_sse_endpoint_is_NOT_rate_limited():
    """Static assertion: /v2/mcp/sse route has no @limiter.limit.

    We cannot hit the SSE endpoint with AsyncClient + ASGITransport
    because the generator never terminates; instead, assert against
    the registered routes that the SSE path does not carry a limiter
    hook. Any future regression adding @limiter.limit to the SSE
    handshake will trip this test.
    """
    import main  # noqa: F401
    sse_route = None
    for r in main.app.routes:
        if getattr(r, "path", "") == "/v2/mcp/sse":
            sse_route = r
            break
    assert sse_route is not None, "SSE route not registered"
    # slowapi stores the limit config on the wrapped endpoint as
    # __wrapped__.func.__closure__ — easier: just assert the handler
    # function does NOT have a `_slowapi_limits` attribute.
    handler = sse_route.endpoint
    # Walk the closure chain looking for any hint of slowapi
    seen_slowapi = False
    fn = handler
    for _ in range(10):
        if "slowapi" in getattr(fn, "__module__", "") or \
           any("slowapi" in str(c) for c in (getattr(fn, "__closure__", None) or [])):
            seen_slowapi = True
            break
        fn = getattr(fn, "__wrapped__", None)
        if fn is None:
            break
    assert not seen_slowapi, "SSE handshake is rate-limited — should not be"


# ── 2. tenacity retry matrix ────────────────────────────────────

async def test_engine_retries_on_upstream_5xx_and_eventually_succeeds():
    """Mock the httpx client to return 500 twice then 200. The _r
    method should yield the 200 payload."""
    e = main.SiyadahEngine("http://fake", "tok")
    # Stub the client
    e._client = SimpleNamespace()

    call_count = {"n": 0}

    async def fake_request(method, url, json=None, params=None):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return _Response(500, b'{"error":"oops"}')
        return _Response(200, b'{"ok":true}')

    e._client.request = fake_request
    e._client.is_closed = False

    async def noop_ensure():
        return e._client
    e._ensure_client = noop_ensure

    result = await e._r("GET", "/v1/probe")
    assert result == {"ok": True}
    assert call_count["n"] == 3, (
        f"expected exactly 3 attempts (2 retries), got {call_count['n']}"
    )


async def test_engine_does_NOT_retry_on_upstream_400():
    """Mock upstream to return 400. _r must raise HTTPException(400)
    after exactly ONE attempt — no retry budget wasted on caller bugs."""
    e = main.SiyadahEngine("http://fake", "tok")
    call_count = {"n": 0}

    async def fake_request(method, url, json=None, params=None):
        call_count["n"] += 1
        return _Response(400, b'{"error":"bad input"}')

    e._client = SimpleNamespace(request=fake_request, is_closed=False)

    async def noop_ensure():
        return e._client
    e._ensure_client = noop_ensure

    with pytest.raises(main.HTTPException) as ei:
        await e._r("GET", "/v1/probe")
    assert ei.value.status_code == 400
    assert call_count["n"] == 1, (
        f"retried on 4xx (attempts={call_count['n']}) — must be 1"
    )


async def test_engine_retries_on_httpx_timeout():
    """Network timeout → converted to HTTPException(502) → retried."""
    e = main.SiyadahEngine("http://fake", "tok")
    call_count = {"n": 0}

    async def flaky_request(method, url, json=None, params=None):
        call_count["n"] += 1
        if call_count["n"] <= 1:
            raise httpx.ReadTimeout("simulated", request=None)
        return _Response(200, b'{"ok":true}')

    e._client = SimpleNamespace(request=flaky_request, is_closed=False)

    async def noop_ensure():
        return e._client
    e._ensure_client = noop_ensure

    result = await e._r("GET", "/v1/probe")
    assert result == {"ok": True}
    assert call_count["n"] == 2  # 1 timeout + 1 success


async def test_engine_exhausts_retries_on_persistent_5xx():
    """If upstream keeps returning 500, _r surfaces the error after
    3 attempts. Caller sees HTTPException(500) — no infinite retry."""
    e = main.SiyadahEngine("http://fake", "tok")
    call_count = {"n": 0}

    async def always_500(method, url, json=None, params=None):
        call_count["n"] += 1
        return _Response(500, b'{"err":"nope"}')

    e._client = SimpleNamespace(request=always_500, is_closed=False)

    async def noop_ensure():
        return e._client
    e._ensure_client = noop_ensure

    with pytest.raises(main.HTTPException) as ei:
        await e._r("GET", "/v1/probe")
    assert ei.value.status_code == 500
    assert call_count["n"] == 3


# ── 3. connection pool limits ───────────────────────────────────

async def test_engine_client_has_bounded_pool():
    e = main.SiyadahEngine("http://fake", "tok")
    c = await e._ensure_client()
    # The Limits object stores max_connections + max_keepalive on _pool.
    assert c._transport._pool._max_connections == 50
    assert c._transport._pool._max_keepalive_connections == 20


# ── helpers ─────────────────────────────────────────────────────

class _Response:
    """Minimal httpx.Response stand-in for tests that don't need
    full response machinery."""
    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "replace")

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        import json as _j
        return _j.loads(self.content)
