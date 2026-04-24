"""
Phase 10 — webhook security harsh tests (Gap 1 remediation).

Split into three layers:
1. Pure crypto unit tests (webhook_security module).
2. /v2/webhook/{flow_id} endpoint tests (GET handshake + POST verify + forward).
3. register-employee integration (flags stored, secret returned once).

External webhook providers (GitHub, Stripe, Slack) hit this endpoint
directly with no X-API-Key. Verification is HMAC-only — tenant
enforcement is bypassed (the /v2/webhook/ prefix is in EXEMPT_PREFIXES).
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import select

import main
import models
from conftest import KEY_A, KEY_B, PID_A, PID_B, hdr
from webhook_security import (
    compute_signature, derive_webhook_secret, extract_handshake_challenge,
    sign_header_value, verify_signature, SIGNATURE_SCHEMES,
)


# ══════════════════════════════════════════════════════════════
# 1. Pure crypto unit tests
# ══════════════════════════════════════════════════════════════

class TestSignatureCrypto:
    """No fixtures needed — tests the pure helpers."""

    def test_verify_roundtrip_siyadah(self):
        body = b'{"event":"order.created","amount":42}'
        secret = "super-secret-key"
        header = sign_header_value(body, secret, scheme="siyadah")
        assert header.startswith("sha256=")
        ok, reason = verify_signature(body, header, secret, scheme="siyadah")
        assert ok and reason == ""

    def test_verify_rejects_tampered_body(self):
        body = b'{"amount":100}'
        secret = "k"
        header = sign_header_value(body, secret)
        ok, reason = verify_signature(
            b'{"amount":999}',  # different body
            header, secret,
        )
        assert not ok
        assert reason == "signature_mismatch"

    def test_verify_rejects_tampered_signature(self):
        body = b"payload"
        secret = "k"
        header = sign_header_value(body, secret)
        # Flip one hex char — still valid length, still same prefix
        tampered = header[:-1] + ("a" if header[-1] != "a" else "b")
        ok, reason = verify_signature(body, tampered, secret)
        assert not ok
        assert reason == "signature_mismatch"

    def test_verify_rejects_missing_header(self):
        ok, reason = verify_signature(b"x", "", "secret")
        assert not ok
        assert reason == "missing_signature_header"

    def test_verify_rejects_empty_secret(self):
        ok, reason = verify_signature(b"x", "sha256=deadbeef", "")
        assert not ok
        assert reason == "no_secret_configured"

    def test_github_scheme_prefix(self):
        body = b"ping"
        secret = "gh"
        header = sign_header_value(body, secret, scheme="github")
        assert header.startswith("sha256=")
        ok, _ = verify_signature(body, header, secret, scheme="github")
        assert ok

    def test_slack_scheme_prefix(self):
        body = b"slk"
        secret = "sl"
        header = sign_header_value(body, secret, scheme="slack")
        assert header.startswith("v0=")
        ok, _ = verify_signature(body, header, secret, scheme="slack")
        assert ok

    def test_compute_signature_refuses_empty_secret(self):
        with pytest.raises(ValueError):
            compute_signature(b"x", "")

    def test_derive_is_deterministic(self):
        os.environ["WEBHOOK_SIGNING_MASTER_KEY"] = "fixed-master"
        s1 = derive_webhook_secret("flow-42")
        s2 = derive_webhook_secret("flow-42")
        assert s1 == s2
        assert len(s1) == 64  # hex sha256

    def test_derive_differs_per_flow(self):
        os.environ["WEBHOOK_SIGNING_MASTER_KEY"] = "fixed-master"
        s_a = derive_webhook_secret("flow-A")
        s_b = derive_webhook_secret("flow-B")
        assert s_a != s_b

    def test_derive_returns_none_without_master(self, monkeypatch):
        monkeypatch.delenv("WEBHOOK_SIGNING_MASTER_KEY", raising=False)
        assert derive_webhook_secret("flow-x") is None

    def test_derive_returns_none_for_empty_flow_id(self):
        os.environ["WEBHOOK_SIGNING_MASTER_KEY"] = "m"
        assert derive_webhook_secret("") is None

    def test_handshake_challenge_extraction(self):
        # GitHub / Meta use 'hub.challenge'
        assert extract_handshake_challenge(
            {"hub.challenge": "g-42"}
        ) == "g-42"
        # Generic 'challenge'
        assert extract_handshake_challenge(
            {"challenge": "c-99"}
        ) == "c-99"
        # Absent → empty string, never None
        assert extract_handshake_challenge({}) == ""


# ══════════════════════════════════════════════════════════════
# 2. /v2/webhook/{flow_id} endpoint tests
# ══════════════════════════════════════════════════════════════

async def _insert_flow_row(
    db_session, flow_id, tenant_id, *,
    secure=True, skip_auth=False, scheme="siyadah",
):
    """Insert a FlowRegistry row directly; bypasses register-employee
    so tests can control flags precisely without a full AP roundtrip."""
    row = models.FlowRegistry(
        tenant_id=tenant_id, flow_id=flow_id,
        display_name=f"Test {flow_id}",
        trigger_type="WEBHOOK",
        webhook_url=f"http://testclient/v2/webhook/{flow_id}",
        piece_manifest={},
        secure_webhook=secure,
        skip_webhook_auth=skip_auth,
        webhook_scheme=scheme,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def test_handshake_get_returns_200_and_echoes_challenge(client):
    """GitHub/Meta/Stripe send GET with challenge param before activating."""
    r = await client.get(
        "/v2/webhook/any-flow-id?hub.challenge=abc-42",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["handshake"] == "ok"
    assert body["challenge"] == "abc-42"
    assert body["flow_id"] == "any-flow-id"
    # webhook_id stamped (always)
    assert body["webhook_id"]


async def test_handshake_no_auth_required(client):
    """Handshake endpoint must work with zero headers — GitHub doesn't
    authenticate its ping."""
    r = await client.get("/v2/webhook/whatever")
    assert r.status_code == 200
    assert r.json()["handshake"] == "ok"


async def test_post_unknown_flow_returns_404(client):
    """Unknown flow_id → 404 (no existence leak)."""
    body = b'{}'
    secret = derive_webhook_secret("unknown-flow") or ""
    r = await client.post(
        "/v2/webhook/unknown-flow",
        content=body,
        headers={
            "x-siyadah-signature": sign_header_value(body, secret),
            "content-type": "application/json",
        },
    )
    assert r.status_code == 404
    assert "flow_not_found" in r.text


async def test_post_flow_not_secured_returns_410(client, db_session):
    """Flow exists but secure_webhook=False → 410 Gone (wrong URL)."""
    await _insert_flow_row(
        db_session, "flow-legacy-1", PID_A, secure=False,
    )
    r = await client.post(
        "/v2/webhook/flow-legacy-1", content=b"{}",
    )
    assert r.status_code == 410
    assert "flow_not_secured_for_proxy" in r.text


async def test_post_missing_signature_on_secure_flow_returns_401(
    client, db_session,
):
    await _insert_flow_row(
        db_session, "flow-secure-1", PID_A, secure=True,
    )
    r = await client.post(
        "/v2/webhook/flow-secure-1",
        content=b'{"test":1}',
    )
    assert r.status_code == 401
    assert "invalid_signature" in r.text
    assert "missing_signature_header" in r.text


async def test_post_valid_signature_forwards_to_ap(
    client, db_session, monkeypatch,
):
    """Happy path: valid HMAC → forward_webhook called → return AP body."""
    flow_id = "flow-ok-1"
    await _insert_flow_row(db_session, flow_id, PID_A, secure=True)
    fake_ap_body = b'{"accepted":true,"run_id":"r-42"}'
    forward_calls = []

    async def fake_forward(self, fid, method, body, headers, query_params):
        forward_calls.append((fid, method, body, headers))
        return 200, fake_ap_body, {"content-type": "application/json"}

    monkeypatch.setattr(main.SiyadahEngine, "forward_webhook", fake_forward)

    body = b'{"event":"order.paid","amount":100}'
    secret = derive_webhook_secret(flow_id)
    header = sign_header_value(body, secret)

    r = await client.post(
        f"/v2/webhook/{flow_id}",
        content=body,
        headers={"x-siyadah-signature": header,
                 "content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.content == fake_ap_body
    assert len(forward_calls) == 1
    fid, method, fwd_body, fwd_headers = forward_calls[0]
    assert fid == flow_id
    assert method == "POST"
    assert fwd_body == body
    # Forwarded headers must NOT include host/auth
    assert "host" not in {k.lower() for k in fwd_headers}
    assert "authorization" not in {k.lower() for k in fwd_headers}
    # Added correlation header
    assert "x-forwarded-webhook-id" in fwd_headers


async def test_post_wrong_signature_returns_401_no_forward(
    client, db_session, monkeypatch,
):
    flow_id = "flow-wrong-sig"
    await _insert_flow_row(db_session, flow_id, PID_A, secure=True)
    forward_calls = []

    async def fake_forward(self, fid, method, body, headers, query_params):
        forward_calls.append((fid, method))
        return 200, b"should not happen", {}

    monkeypatch.setattr(main.SiyadahEngine, "forward_webhook", fake_forward)

    body = b'{"foo":1}'
    r = await client.post(
        f"/v2/webhook/{flow_id}",
        content=body,
        headers={"x-siyadah-signature": "sha256=deadbeef" * 8},
    )
    assert r.status_code == 401
    assert forward_calls == [], "forward happened on failed verification"


async def test_post_skip_auth_allows_missing_signature(
    client, db_session, monkeypatch,
):
    """skip_webhook_auth=True marks the flow as legacy — POSTs are
    forwarded without HMAC, with a WARNING log."""
    flow_id = "flow-skip-1"
    await _insert_flow_row(
        db_session, flow_id, PID_A, secure=True, skip_auth=True,
    )
    forward_called = []

    async def fake_forward(self, fid, method, body, headers, query_params):
        forward_called.append(fid)
        return 200, b'{"ok":true}', {}

    monkeypatch.setattr(main.SiyadahEngine, "forward_webhook", fake_forward)

    r = await client.post(
        f"/v2/webhook/{flow_id}",
        content=b'{"legacy":true}',
        # No signature header at all
    )
    assert r.status_code == 200
    assert forward_called == [flow_id]


async def test_forward_failure_returns_502(client, db_session, monkeypatch):
    flow_id = "flow-ap-down"
    await _insert_flow_row(db_session, flow_id, PID_A, secure=True)

    async def boom_forward(self, fid, method, body, headers, query_params):
        raise RuntimeError("AP network unreachable")

    monkeypatch.setattr(main.SiyadahEngine, "forward_webhook", boom_forward)

    body = b"{}"
    secret = derive_webhook_secret(flow_id)
    r = await client.post(
        f"/v2/webhook/{flow_id}",
        content=body,
        headers={"x-siyadah-signature": sign_header_value(body, secret)},
    )
    assert r.status_code == 502
    assert "upstream_forward_failed" in r.text


async def test_x_webhook_id_preserved_when_provided(
    client, db_session, monkeypatch,
):
    """Caller-provided x-webhook-id must round-trip into both the
    forwarded headers and the response header."""
    flow_id = "flow-corr-1"
    await _insert_flow_row(db_session, flow_id, PID_A, secure=True)
    forwarded_id = []

    async def fake_forward(self, fid, method, body, headers, query_params):
        forwarded_id.append(headers.get("x-forwarded-webhook-id"))
        return 200, b"{}", {}

    monkeypatch.setattr(main.SiyadahEngine, "forward_webhook", fake_forward)

    body = b"{}"
    secret = derive_webhook_secret(flow_id)
    caller_id = "my-trace-id-xyz"

    r = await client.post(
        f"/v2/webhook/{flow_id}",
        content=body,
        headers={
            "x-siyadah-signature": sign_header_value(body, secret),
            "x-webhook-id": caller_id,
        },
    )
    assert r.status_code == 200
    # Forward carried it
    assert forwarded_id == [caller_id]
    # Response echoes it
    assert r.headers.get("x-webhook-id") == caller_id


# ══════════════════════════════════════════════════════════════
# 3. register-employee integration
# ══════════════════════════════════════════════════════════════

async def test_register_employee_secure_returns_secret_once(client, db_session):
    """BFF opts a flow into secure mode; response carries the secret
    so it can be handed to the external provider. Secret NEVER stored
    in DB (zero-knowledge invariant)."""
    r = await client.post(
        "/v2/flows/flow-A-1/register-employee",
        headers=hdr(KEY_A, PID_A),
        json={"secure_webhook": True, "webhook_scheme": "siyadah"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["webhook"]["secure"] is True
    assert body["webhook"]["scheme"] == "siyadah"
    assert "secret" in body["webhook"]
    # Secret matches what the verifier will expect
    expected = derive_webhook_secret("flow-A-1")
    assert body["webhook"]["secret"] == expected
    # Returned proxy URL points at orchestrator not AP
    assert "/v2/webhook/flow-A-1" in body["webhook"]["url"]

    # DB must NOT contain the secret — only the flag
    row = (await db_session.execute(
        select(models.FlowRegistry).where(
            models.FlowRegistry.flow_id == "flow-A-1"
        )
    )).scalar_one()
    assert row.secure_webhook is True
    # No column exists named "webhook_secret" — prove it by attribute:
    assert not hasattr(row, "webhook_secret")


async def test_register_employee_default_is_non_secure(client, db_session):
    """Legacy path: omit the new flag → existing behaviour unchanged,
    webhook URL still points at AP direct."""
    r = await client.post(
        "/v2/flows/flow-A-2/register-employee",
        headers=hdr(KEY_A, PID_A),
        json={},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["webhook"]["secure"] is False
    assert "secret" not in body["webhook"]
    # URL is AP direct
    assert "/api/v1/webhooks/flow-A-2" in body["webhook"]["url"]


async def test_secure_webhook_requires_master_key(
    client, db_session, monkeypatch,
):
    """If WEBHOOK_SIGNING_MASTER_KEY is unset on this deploy, asking
    for secure_webhook=True degrades gracefully: flag stored False,
    URL falls back to AP direct, no secret returned."""
    monkeypatch.delenv("WEBHOOK_SIGNING_MASTER_KEY", raising=False)

    r = await client.post(
        "/v2/flows/flow-A-orphan/register-employee",
        headers=hdr(KEY_A, PID_A),
        json={"secure_webhook": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["webhook"]["secure"] is False
    assert "secret" not in body["webhook"]

    row = (await db_session.execute(
        select(models.FlowRegistry).where(
            models.FlowRegistry.flow_id == "flow-A-orphan"
        )
    )).scalar_one()
    assert row.secure_webhook is False
