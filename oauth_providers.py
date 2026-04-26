"""
OAuth provider registry — per-provider configuration in one place.

Adding a provider = appending one entry to PROVIDERS. The route layer
(oauth_routes.py) is provider-agnostic; provider differences (PKCE
support, scope joining convention, extra URL params, response shape)
live here.
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
# Slack-specific parser — Slack returns ok:bool, not standard 4xx
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
    # ── Phase 4.3 — AP connection contract ──
    # Activepieces accepts several connection types. We sidestep AP's
    # "give me the code, I'll exchange it" flow (we already have tokens)
    # by injecting via CUSTOM_AUTH where the piece accepts a plain bearer.
    # The value-builder turns our decrypted access token into AP's
    # piece-specific value object.
    ap_connection_type: str = "CUSTOM_AUTH"
    ap_value_builder: Optional[Callable[[str, ParsedTokenResponse], dict]] = None

    def client_id(self) -> str:
        return os.getenv(self.client_id_env, "")

    def redirect_uri(self) -> str:
        return os.getenv(self.redirect_uri_env, "")


PROVIDERS: dict[str, ProviderConfig] = {
    "slack": ProviderConfig(
        name="slack",
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
