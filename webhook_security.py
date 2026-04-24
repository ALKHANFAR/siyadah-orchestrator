"""
Wave-10 Phase 10 — Webhook signature verification (Gap 1 remediation).

Pure crypto helpers. Stateless, no I/O, no DB. Split out so callers can
test signature logic independently of the FastAPI app / AP proxy.

Design choices
--------------
- **Master-key derivation (Zero-Knowledge DB)**: no webhook secret is
  ever written to Postgres. `derive_webhook_secret(flow_id)` mixes
  ``WEBHOOK_SIGNING_MASTER_KEY`` (env) with the flow_id via HMAC-SHA256
  to produce a deterministic per-flow secret. Rotating the master key
  invalidates every flow's signature (deliberate — forces re-sharing).
- **Constant-time compare** via ``hmac.compare_digest`` so we don't
  leak timing side-channels on signature mismatch.
- **Multi-scheme support** (siyadah | github | stripe | slack) so flows
  can point external providers at our proxy URL without forcing a
  Siyadah-specific signing contract.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Tuple


# ─── signature schemes ─────────────────────────────────────────

# (header_name, algorithm, expected_prefix)
# expected_prefix is what the HEADER VALUE is prefixed with; the
# hex digest follows immediately.  Empty string → header is just the
# raw hex digest (Stripe's own scheme is more complex; see NOTE).
SIGNATURE_SCHEMES: dict[str, Tuple[str, str, str]] = {
    "siyadah":  ("x-siyadah-signature",  "sha256", "sha256="),
    "github":   ("x-hub-signature-256",  "sha256", "sha256="),
    "slack":    ("x-slack-signature",    "sha256", "v0="),
    # NOTE: Stripe's real scheme is "t=<ts>,v1=<hex>" and signs
    # "<ts>.<body>" not just body. First implementation treats Stripe
    # as raw hex; upgrade to their format in a follow-up if real Stripe
    # webhooks are onboarded.
    "stripe":   ("stripe-signature",     "sha256", ""),
}


def _get_scheme(name: str) -> Tuple[str, str, str]:
    return SIGNATURE_SCHEMES.get(name, SIGNATURE_SCHEMES["siyadah"])


# ─── signing ───────────────────────────────────────────────────

def compute_signature(body: bytes, secret: str, algorithm: str = "sha256") -> str:
    """Raw hex digest — no prefix. Callers add scheme-specific prefix."""
    if not secret:
        raise ValueError("compute_signature requires a non-empty secret")
    h = hmac.new(secret.encode("utf-8"), body, getattr(hashlib, algorithm))
    return h.hexdigest()


def sign_header_value(body: bytes, secret: str, scheme: str = "siyadah") -> str:
    """Produce the full header value (prefix + digest) a caller should send."""
    _name, algo, prefix = _get_scheme(scheme)
    return f"{prefix}{compute_signature(body, secret, algo)}"


# ─── verification ──────────────────────────────────────────────

def verify_signature(
    body: bytes,
    signature_header: str,
    secret: str,
    scheme: str = "siyadah",
) -> Tuple[bool, str]:
    """Verify that ``signature_header`` is a valid HMAC of ``body`` under ``secret``.

    Returns ``(ok, reason)``. ``reason`` is:
      - ``""`` on success
      - ``"missing_signature_header"`` if header is empty / None
      - ``"no_secret_configured"`` if we have no secret to compare against
      - ``"signature_mismatch"`` on any byte difference (constant-time)

    The function never raises on bad input so callers can log the reason
    without worrying about exception handling on the hot path.
    """
    if not signature_header:
        return False, "missing_signature_header"
    if not secret:
        return False, "no_secret_configured"
    _name, algo, prefix = _get_scheme(scheme)
    try:
        expected = f"{prefix}{compute_signature(body, secret, algo)}"
    except Exception:
        return False, "compute_failed"
    # constant-time compare of identical-length byte strings
    got = signature_header.strip()
    if not hmac.compare_digest(expected.encode("utf-8"), got.encode("utf-8")):
        return False, "signature_mismatch"
    return True, ""


# ─── secret derivation (Zero-Knowledge DB) ─────────────────────

def derive_webhook_secret(flow_id: str) -> str | None:
    """Deterministic per-flow secret from ``WEBHOOK_SIGNING_MASTER_KEY``.

    Returns ``None`` if the master key env var is unset — callers treat
    this as "secure webhook not available on this deploy" and decline
    to mark flows as secure.

    The derived string is a 64-char hex HMAC-SHA256 digest. Rotating
    ``WEBHOOK_SIGNING_MASTER_KEY`` invalidates every flow's secret
    simultaneously — this is intentional: one env change forces a full
    signature rotation, which is the safest posture on key compromise.
    """
    master = os.getenv("WEBHOOK_SIGNING_MASTER_KEY", "").strip()
    if not master:
        return None
    if not flow_id:
        return None
    return hmac.new(
        master.encode("utf-8"),
        f"flow-webhook:{flow_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ─── handshake helper ─────────────────────────────────────────

# Param keys that various providers use when challenging the endpoint:
#   GitHub:   'hub.challenge'
#   Meta/Facebook: 'hub.challenge'
#   Slack:    'challenge'  (in body, not URL — still supported)
#   generic:  'challenge'
HANDSHAKE_PARAM_KEYS = ("hub.challenge", "challenge", "validation_token")


def extract_handshake_challenge(query_params: dict) -> str:
    """Return whichever challenge token the provider sent, else empty str."""
    for key in HANDSHAKE_PARAM_KEYS:
        v = query_params.get(key)
        if v:
            return v
    return ""
