"""
Phase 3 — logging harsh tests.

Covers:
- Every stdout line from the app is valid JSON.
- request_id + tenant_id attached via contextvars.
- Secret scrubbing: sk-ant-*, Bearer tokens, sha256 hex never leak.
- Sentry before_send scrub also redacts.
"""
from __future__ import annotations

import io
import json
import logging
import sys

import pytest

from conftest import KEY_A, PID_A, hdr
from logging_config import _scrub, _scrub_event, _sentry_before_send


# ── 1. stdout JSON ──────────────────────────────────────────────

async def test_structlog_render_produces_valid_json(client):
    """Render a log event manually via the configured wrapper and
    assert it parses as JSON with our required fields. Bypasses the
    file-capture flakiness of PrintLoggerFactory under pytest."""
    import structlog
    from logging_config import bind_request_context, clear_request_context

    bind_request_context(request_id="rid-xyz", tenant_id=PID_A)
    try:
        # Render manually using the same processor chain configure_logging
        # installed. This would print to stdout in prod — here we just
        # want to confirm the chain doesn't blow up and produces JSON.
        cfg = structlog.get_config()
        ev = {"event": "probe message", "level": "info",
              "timestamp": "2026-04-23T00:00:00Z"}
        # Walk the chain manually (last processor is the renderer).
        for proc in cfg["processors"][:-1]:
            ev = proc(None, "info", ev)
        rendered = cfg["processors"][-1](None, "info", ev)
        parsed = json.loads(rendered)
        assert parsed["event"] == "probe message"
        assert parsed["request_id"] == "rid-xyz"
        assert parsed["tenant_id"] == PID_A
    finally:
        clear_request_context()


async def test_structlog_chain_scrubs_secrets_in_rendered_output():
    """End-to-end: a log event carrying an sk-ant-… secret must emerge
    from the processor chain with that secret redacted."""
    import structlog
    cfg = structlog.get_config()
    evil_ev = {"event": "leaked sk-ant-api03-ABCDEFGHIJKLMNOPQR0123456",
               "level": "warning", "timestamp": "2026-04-23T00:00:00Z"}
    for proc in cfg["processors"][:-1]:
        evil_ev = proc(None, "warning", evil_ev)
    rendered = cfg["processors"][-1](None, "warning", evil_ev)
    assert "sk-ant-" not in rendered, rendered
    assert "redacted" in rendered


def test_structlog_is_configured_with_json_renderer():
    """After lifespan ran during conftest import, structlog's active
    processor chain must include JSONRenderer AND merge_contextvars.
    This verifies the wire-up without relying on pytest's capture
    stealing stdout from structlog's PrintLoggerFactory."""
    import structlog
    cfg = structlog.get_config()
    chain_names = []
    for p in cfg["processors"]:
        if hasattr(p, "__name__"):
            chain_names.append(p.__name__)
        else:
            chain_names.append(type(p).__name__)
    assert any("JSONRenderer" in c for c in chain_names), f"chain={chain_names}"
    assert any("merge_contextvars" in c for c in chain_names), f"chain={chain_names}"
    assert any("_scrub_event" in c for c in chain_names), f"chain={chain_names}"


async def test_contextvars_binding_roundtrip(client):
    """bind_request_context + clear_request_context must wipe the
    task-local context on clear."""
    from structlog.contextvars import get_contextvars
    from logging_config import bind_request_context, clear_request_context

    bind_request_context(request_id="test-rid-1", tenant_id=PID_A)
    ctx = get_contextvars()
    assert ctx.get("request_id") == "test-rid-1"
    assert ctx.get("tenant_id") == PID_A

    clear_request_context()
    ctx = get_contextvars()
    assert "request_id" not in ctx
    assert "tenant_id" not in ctx


async def test_require_tenant_binds_and_clears_tenant_id_per_request(client):
    """After require_tenant returns for a successful request, the
    context vars are cleared by its finally block."""
    from structlog.contextvars import get_contextvars
    r = await client.get("/v2/templates", headers=hdr(KEY_A, PID_A))
    assert r.status_code == 200
    ctx = get_contextvars()
    assert "request_id" not in ctx, f"leaked ctx: {ctx}"
    assert "tenant_id" not in ctx


# ── 2. secret scrubbing unit tests ──────────────────────────────

def test_scrub_redacts_anthropic_key():
    msg = "user said sk-ant-api03-ABC123DEFGHIJKLMNOP_QRSTUV when asking for help"
    scrubbed = _scrub(msg)
    assert "sk-ant-" not in scrubbed
    assert "<redacted>" in scrubbed


def test_scrub_redacts_bearer_token():
    msg = "Authorization: Bearer abc123xyz456-ABCDEF789012345678 plus tail"
    scrubbed = _scrub(msg)
    assert "abc123xyz456-ABCDEF789012345678" not in scrubbed
    # Either the whole "Bearer TOKEN" phrase is redacted or at least the token.
    assert "<redacted>" in scrubbed


def test_scrub_redacts_sha256_hex():
    h = "a" * 64
    scrubbed = _scrub(f"api_key_hash={h} path=/v2/build")
    assert h not in scrubbed
    assert "<redacted>" in scrubbed


def test_scrub_leaves_normal_text_alone():
    msg = "regular log message without any secret"
    assert _scrub(msg) == msg


def test_scrub_event_processor_handles_nested_fields():
    ev = {"event": "Auth failed for sk-ant-TESTKEY123456789012345",
          "tenant_id": "tenant-A",
          "api_key_hash": "b" * 64,
          "level": "warning"}
    out = _scrub_event(None, "warning", ev)
    assert "sk-ant-" not in out["event"]
    assert out["tenant_id"] == "tenant-A"  # not redacted
    assert "<redacted>" in out["api_key_hash"]


# ── 3. Sentry before_send scrub ─────────────────────────────────

def test_sentry_before_send_redacts_top_level_message():
    ev = {"message": "crashed with Bearer " + "x" * 30 + " attached"}
    out = _sentry_before_send(ev, {})
    assert "Bearer " + "x" * 30 not in out["message"]


def test_sentry_before_send_redacts_breadcrumb_message():
    ev = {"breadcrumbs": {"values": [
        {"message": "called with sk-ant-api03-XYZABCDEF" + "1" * 20},
    ]}}
    out = _sentry_before_send(ev, {})
    assert "sk-ant-" not in out["breadcrumbs"]["values"][0]["message"]


def test_sentry_before_send_no_op_on_empty_event():
    out = _sentry_before_send({}, {})
    assert out == {}
