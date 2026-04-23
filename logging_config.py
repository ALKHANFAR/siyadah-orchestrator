"""
Wave-1 Phase 3 — structured logging + Sentry.

Replaces the stdlib text formatter with a JSON renderer that carries
tenant_id / flow_id / request_id on every log line via structlog's
contextvars processor. Existing ~150 `log.info(...)` / `log.warning(...)`
call sites are NOT rewritten — structlog's stdlib bridge upgrades them
automatically. New code can bind extra fields with
`log.bind(flow_id=...)` for richer output.

Sentry init is opt-in via the SENTRY_DSN env var. PII is disabled
(send_default_pii=False) and we scrub API keys / bearer tokens in
before_send so a stray log line can't leak a credential. Sample rate
for traces is 10% by default — raise via SENTRY_TRACES_SAMPLE_RATE.

Entry point: ``configure_logging(level)`` called once at app startup
from main.py's lifespan.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    merge_contextvars,
    unbind_contextvars,
)

_SECRET_PATTERNS = [
    # Anthropic keys
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),
    # Generic Bearer tokens (trimmed in logs)
    re.compile(r"Bearer\s+[A-Za-z0-9\-_.]{20,}", re.IGNORECASE),
    # sha256-ish hex blobs (our tenant_api_keys.key_hash — harmless but
    # still not worth shipping to Sentry)
    re.compile(r"\b[a-f0-9]{64}\b"),
]


def _scrub(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("<redacted>", text)
    return text


def _scrub_event(event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: run scrubbing on the rendered message + args."""
    msg = event_dict.get("event")
    if isinstance(msg, str):
        event_dict["event"] = _scrub(msg)
    for k, v in list(event_dict.items()):
        if isinstance(v, str):
            event_dict[k] = _scrub(v)
    return event_dict


def _sentry_before_send(event, hint):
    """Sentry before_send: last-line defence scrub for breadcrumbs + message."""
    try:
        if event.get("message"):
            event["message"] = _scrub(event["message"])
        for bc in (event.get("breadcrumbs", {}) or {}).get("values", []) or []:
            if isinstance(bc.get("message"), str):
                bc["message"] = _scrub(bc["message"])
    except Exception:  # nosec B110 — never fail the send
        pass
    return event


def configure_logging(level: str = "INFO") -> None:
    """Wire structlog, redirect stdlib logging through it, init Sentry.

    Safe to call multiple times — structlog is idempotent and Sentry
    init is guarded by a module-level flag inside sentry_sdk.
    """
    # --- structlog ---
    shared_processors = [
        merge_contextvars,                    # inject request_id / tenant_id
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _scrub_event,
    ]
    structlog.configure(
        processors=shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # --- stdlib bridge: all logging.getLogger(...).info(...) → structlog JSON ---
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    # Replace any pre-existing handlers (uvicorn/gunicorn install their own).
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Quiet noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)

    # --- Sentry (opt-in) ---
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if dsn:
        try:
            import sentry_sdk
            from sentry_sdk.integrations.fastapi import FastApiIntegration
            from sentry_sdk.integrations.starlette import StarletteIntegration

            sample_rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"))
            sentry_sdk.init(
                dsn=dsn,
                environment=os.getenv("SENTRY_ENV", "production"),
                release=os.getenv("SENTRY_RELEASE", ""),
                traces_sample_rate=sample_rate,
                send_default_pii=False,
                before_send=_sentry_before_send,
                integrations=[
                    StarletteIntegration(transaction_style="endpoint"),
                    FastApiIntegration(transaction_style="endpoint"),
                ],
            )
            logging.getLogger("siyadah.logging").info(
                "Sentry initialised (env=%s, traces=%.2f)",
                os.getenv("SENTRY_ENV", "production"), sample_rate,
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("siyadah.logging").error(
                "Sentry init failed: %s", exc,
            )
    else:
        logging.getLogger("siyadah.logging").info(
            "SENTRY_DSN unset — error reporting disabled (dev mode)."
        )


def bind_request_context(
    request_id: str | None = None,
    tenant_id: str | None = None,
    **extra: Any,
) -> None:
    """Bind fields onto the current asyncio task's log context.

    Called by auth.require_tenant on every /v2/* request. Subsequent
    log.info/error from ANY module inherit these fields until the
    request ends (clear_contextvars is called in a finally block).
    """
    ctx: dict[str, Any] = {}
    if request_id:
        ctx["request_id"] = request_id
    if tenant_id:
        ctx["tenant_id"] = tenant_id
    if extra:
        ctx.update(extra)
    if ctx:
        bind_contextvars(**ctx)


def clear_request_context() -> None:
    clear_contextvars()


def bind_extra(**extra: Any) -> None:
    """Add fields mid-request (e.g. flow_id after a build-* call resolves)."""
    if extra:
        bind_contextvars(**extra)


def unbind_extra(*keys: str) -> None:
    if keys:
        unbind_contextvars(*keys)
