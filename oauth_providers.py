"""
OAuth provider registry — per-provider configuration in one place.

Adding a provider = appending one entry to PROVIDERS. The route layer
(oauth_routes.py) is provider-agnostic; provider differences (PKCE
support, scope joining convention, extra URL params, response shape)
live here.

PHASE 4.5 — DELEGATED OAUTH
─────────────────────────────────────────────────────────────────────
Some providers (Google, Microsoft, …) declare `delegate_oauth_to_ap=True`.
For these, the orchestrator does NOT exchange the authorization code
itself. Instead, it forwards the raw code to Activepieces along with
our client_id/secret and redirect_url. AP performs the token exchange
using its own HTTP client, stores the resulting tokens encrypted in
its own DB, and owns the refresh-token lifecycle from then on.

This avoids the impedance mismatch between AP's connection schemas
(which want a `code` field for OAUTH2, or platform-level config for
PLATFORM_OAUTH2) and our pre-baked token model. AP becomes the
canonical source of truth for OAuth tokens.

For non-delegated providers (Slack, …) the legacy flow still applies:
we exchange tokens, encrypt them locally, and inject into AP via
CUSTOM_AUTH (which accepts a plain bearer token).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional


# ═══════════════════════════════════════════════════════════════
# Parsed token response — uniform shape after provider quirks
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ParsedTokenResponse:
    """Provider-agnostic shape callbacks operate on. Each provider's
    `parse_response` callable maps its native JSON to this struct."""
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    scopes: list[str] = field(default_factory=list)
    provider_account_id: Optional[str] = None


class TokenExchangeError(Exception):
    """Raised when a provider rejects the code exchange."""
    def __init__(self, provider: str, code: str, message: str = ""):
        self.provider = provider
        self.code = code
        super().__init__(
            f"{provider}: {code} — {message}" if message else f"{provider}: {code}"
        )


# ═══════════════════════════════════════════════════════════════
# Slack — non-delegated (we exchange + inject via CUSTOM_AUTH)
# ═══════════════════════════════════════════════════════════════

def _slack_ap_value(access_token: str, parsed: ParsedTokenResponse) -> dict:
    """Build the AP `value` object for a Slack CUSTOM_AUTH connection.

    Slack's piece schema declares `props: { token: SHORT_TEXT }` for its
    CUSTOM_AUTH path. AP's createConnection runs an `auth_test` against
    Slack BEFORE accepting — invalid tokens are rejected at this point
    with INVALID_APP_CONNECTION (a useful natural-failure trigger for
    L5 compensation testing).
    """
    return {
        "type": "CUSTOM_AUTH",
        "props": {"token": access_token},
    }


def _parse_slack(j: dict) -> ParsedTokenResponse:
    if not isinstance(j, dict):
        raise TokenExchangeError("slack", "malformed_response", str(type(j)))
    if not j.get("ok", False):
        err = j.get("error", "unknown_error")
        raise TokenExchangeError("slack", err)
    bot_token = j.get("access_token")
    if not bot_token:
        raise TokenExchangeError("slack", "no_access_token_in_response")
    scope_str = j.get("scope", "") or ""
    team_id = (j.get("team") or {}).get("id")
    return ParsedTokenResponse(
        access_token=bot_token,
        refresh_token=j.get("refresh_token"),
        expires_in=j.get("expires_in"),
        scopes=[s.strip() for s in scope_str.split(",") if s.strip()],
        provider_account_id=team_id,
    )


# ═══════════════════════════════════════════════════════════════
# Google — DELEGATED (AP performs the token exchange)
# ═══════════════════════════════════════════════════════════════
#
# We deliberately DO NOT define an ap_value_builder for Google. The
# delegated path in oauth_routes.py builds the OAUTH2 value inline,
# because it needs the raw `code` (which the legacy ap_value_builder
# signature `(access_token, parsed)` doesn't carry). The route layer
# checks `cfg.delegate_oauth_to_ap` and branches accordingly.
#
# We KEEP `_parse_google` because it's still useful for any future
# diagnostics or refresh-monitoring scripts that may exchange tokens
# directly against Google for verification purposes.

def _parse_google(j: dict) -> ParsedTokenResponse:
    """Parse Google's token-exchange JSON. Currently unused on the
    delegated path (AP does the exchange) but kept for diagnostics."""
    if not isinstance(j, dict):
        raise TokenExchangeError("google", "malformed_response", str(type(j)))
    if "error" in j:
        raise TokenExchangeError(
            "google",
            j.get("error", "unknown_error"),
            j.get("error_description", ""),
        )
    access = j.get("access_token")
    if not access:
        raise TokenExchangeError("google", "no_access_token_in_response")
    scope_str = j.get("scope", "") or ""
    return ParsedTokenResponse(
        access_token=access,
        refresh_token=j.get("refresh_token"),
        expires_in=j.get("expires_in"),
        scopes=[s.strip() for s in scope_str.split(" ") if s.strip()],
        provider_account_id=None,
    )


# ═══════════════════════════════════════════════════════════════
# Provider config + registry
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ProviderConfig:
    name: str
    authorize_url: str
    token_url: str = ""
    default_scopes: list[str] = field(default_factory=list)
    uses_pkce: bool = True
    scope_separator: str = " "
    extra_authorize_params: dict = field(default_factory=dict)
    client_id_env: str = ""
    client_secret_env: str = ""
    redirect_uri_env: str = ""
    parse_response: Optional[Callable[[dict], ParsedTokenResponse]] = None

    # ── Phase 4.3 — AP connection contract (legacy non-delegated) ──
    # For providers we DON'T delegate to AP, this controls how we
    # inject the pre-exchanged tokens into AP. Slack uses CUSTOM_AUTH.
    ap_connection_type: str = "CUSTOM_AUTH"
    ap_value_builder: Optional[Callable[[str, ParsedTokenResponse], dict]] = None

    # ── Phase 4.5 — multi-piece providers ──
    # AP connections are per-piece. Google has many pieces (gmail,
    # google-sheets, google-drive, …). For now we tie one provider
    # to one default piece. Multi-piece-per-provider would require
    # passing piece_name through the saga (Phase 4.6).
    piece_name: str = ""

    # ── Phase 4.5 — delegated OAuth ──
    # If True, the orchestrator does NOT call _exchange_code or
    # _persist_tokens. Instead, the callback forwards the raw code
    # to AP via OAUTH2 type. AP exchanges, stores, and refreshes.
    delegate_oauth_to_ap: bool = False

    def client_id(self) -> str:
        return os.getenv(self.client_id_env, "")

    def redirect_uri(self) -> str:
        return os.getenv(self.redirect_uri_env, "")


PROVIDERS: dict[str, ProviderConfig] = {
    "slack": ProviderConfig(
        name="slack",
        piece_name="slack",
        authorize_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",
        default_scopes=["chat:write", "channels:read", "users:read"],
        uses_pkce=False,
        scope_separator=",",
        extra_authorize_params={"user_scope": ""},
        client_id_env="SLACK_CLIENT_ID",
        client_secret_env="SLACK_CLIENT_SECRET",
        redirect_uri_env="SLACK_REDIRECT_URI",
        parse_response=_parse_slack,
        ap_connection_type="CUSTOM_AUTH",
        ap_value_builder=_slack_ap_value,
        delegate_oauth_to_ap=False,  # legacy non-delegated path
    ),
    "google": ProviderConfig(
        name="google",
        piece_name="gmail",  # MVP: tie to Gmail. Multi-piece in Phase 4.6.
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        default_scopes=[
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
            "openid",
            "email",
        ],
        uses_pkce=False,
        scope_separator=" ",
        extra_authorize_params={
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        },
        client_id_env="GOOGLE_CLIENT_ID",
        client_secret_env="GOOGLE_CLIENT_SECRET",
        redirect_uri_env="GOOGLE_REDIRECT_URI",
        parse_response=_parse_google,
        # Delegated: we don't build the AP value ourselves.
        # The route layer constructs it inline using the raw code.
        ap_connection_type="OAUTH2",
        ap_value_builder=None,
        delegate_oauth_to_ap=True,
    ),
}


class UnknownProviderError(Exception):
    """Unsupported provider in the URL path."""


def get_provider(name: str) -> ProviderConfig:
    cfg = PROVIDERS.get(name.lower())
    if cfg is None:
        raise UnknownProviderError(
            f"Unknown OAuth provider {name!r}. Supported: {sorted(PROVIDERS)}"
        )
    return cfg