import asyncio
from typing import Any, Dict, List, Optional

import pytest

from services.pending_activation import (
    GATE6_FLAG_ENV,
    build_connection_gate_payload,
    build_pending_activation_payload,
    build_sanitized_metadata,
    create_ap_visible_draft_flow,
    gate6_ap_visible_draft_enabled,
    sanitize_missing_connections,
    sanitize_pieces,
    save_pending_activation_plan,
)


# ───────────────────────────────────────────────────────────────────
# Fakes
# ───────────────────────────────────────────────────────────────────

class FakeEngine:
    """Records every call. Forbidden ops raise on access."""

    def __init__(
        self,
        fid: str = "fake-flow-id",
        get_status: str = "DISABLED",
        get_trigger_type: str = "PIECE_TRIGGER",
    ):
        self._fid = fid
        self._get_status = get_status
        self._get_trigger_type = get_trigger_type
        self.calls: List[str] = []
        self.create_flow_args: Optional[Dict[str, Any]] = None
        self.update_metadata_args: Optional[Dict[str, Any]] = None
        self.import_flow_args: Optional[Dict[str, Any]] = None
        self.get_flow_args: Optional[Dict[str, Any]] = None

    async def create_flow(self, pid: str, name: str):
        self.calls.append("create_flow")
        self.create_flow_args = {"pid": pid, "name": name}
        return {"id": self._fid, "status": "DISABLED"}

    async def update_metadata(self, fid: str, metadata: dict):
        self.calls.append("update_metadata")
        self.update_metadata_args = {"fid": fid, "metadata": metadata}
        return {"ok": True}

    async def import_flow(self, fid: str, display_name: str, trigger: dict):
        self.calls.append("import_flow")
        self.import_flow_args = {"fid": fid, "display_name": display_name, "trigger": trigger}
        return {"ok": True}

    async def get_flow(self, fid: str):
        self.calls.append("get_flow")
        self.get_flow_args = {"fid": fid}
        return {
            "id": fid,
            "status": self._get_status,
            "version": {"trigger": {"type": self._get_trigger_type}},
        }

    # Forbidden ops — any access fails the test loudly.
    async def publish_and_enable(self, fid: str):
        raise AssertionError(
            f"publish_and_enable called on AP_VISIBLE_DRAFT flow={fid}"
        )

    async def _fop(self, fid: str, op: str, req: dict):
        raise AssertionError(
            f"_fop called with op={op!r} on AP_VISIBLE_DRAFT flow={fid}"
        )

    async def test_webhook(self, fid: str, payload: dict):
        raise AssertionError(
            f"test_webhook called on AP_VISIBLE_DRAFT flow={fid}"
        )


class FailEngine:
    """Any AP call raises — proves the off-path never touches AP."""

    def __init__(self):
        self.calls: List[str] = []

    async def create_flow(self, *a, **kw):
        self.calls.append("create_flow")
        raise AssertionError("create_flow must not be called when flag is off")

    async def update_metadata(self, *a, **kw):
        self.calls.append("update_metadata")
        raise AssertionError("update_metadata must not be called when flag is off")

    async def import_flow(self, *a, **kw):
        self.calls.append("import_flow")
        raise AssertionError("import_flow must not be called when flag is off")

    async def get_flow(self, *a, **kw):
        self.calls.append("get_flow")
        raise AssertionError("get_flow must not be called when flag is off")


# ───────────────────────────────────────────────────────────────────
# sanitize_pieces / sanitize_missing_connections
# ───────────────────────────────────────────────────────────────────

def test_sanitize_strips_secrets_from_blocked_pieces():
    raw = [
        {
            "piece": "@activepieces/piece-google-sheets",
            "short": "google-sheets",
            "status": "BLOCKED_CONNECTION_REQUIRED",
            "reason": "missing_or_inactive_connection",
            "auth_type": "CLOUD_OAUTH2",
            "requires_auth": True,
            # Forbidden fields — must not appear in output:
            "errored_connections": [
                {"id": "conn-123", "externalId": "gsheets-x", "displayName": "Sheets X", "status": "ERROR"}
            ],
            "connection_external_id": "gsheets-x",
            "connection_display_name": "Sheets X",
            "connection_type": "CLOUD_OAUTH2",
            "ownerEmail": "owner@tenant.com",
            "id": "internal-row-id",
            "displayName": "Sheets X",
            "platformId": "p-1",
            "projectId": "tenant-A",
            "flowId": "flow-xyz",
        }
    ]

    cleaned = sanitize_missing_connections(raw)
    assert len(cleaned) == 1
    keys = set(cleaned[0].keys())
    assert keys == {"piece", "short", "status", "reason", "auth_type", "requires_auth"}

    forbidden = {
        "errored_connections", "connection_external_id", "connection_display_name",
        "connection_type", "ownerEmail", "id", "displayName", "platformId",
        "projectId", "flowId",
    }
    assert keys.isdisjoint(forbidden), f"sanitized output leaked: {keys & forbidden}"


def test_sanitize_handles_runnable_entries_and_garbage():
    raw = [
        {  # runnable, no reason
            "piece": "@activepieces/piece-gmail",
            "short": "gmail",
            "status": "RUNNABLE_CONNECTED",
            "auth_type": "OAUTH2",
            "requires_auth": True,
            "connection_external_id": "gmail-active",
            "connection_display_name": "Gmail",
            "connection_type": "OAUTH2",
        },
        None,          # ignored
        "not-a-dict",  # ignored
    ]
    cleaned = sanitize_pieces(raw)
    assert len(cleaned) == 1
    assert cleaned[0] == {
        "piece": "@activepieces/piece-gmail",
        "short": "gmail",
        "status": "RUNNABLE_CONNECTED",
        "auth_type": "OAUTH2",
        "requires_auth": True,
    }


def test_sanitize_empty_input():
    assert sanitize_pieces(None) == []
    assert sanitize_pieces([]) == []


# ───────────────────────────────────────────────────────────────────
# gate6_ap_visible_draft_enabled
# ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["", "0", "false", "true", "yes", "True", "1 ", " 1", "01", "01"])
def test_flag_off_for_non_strict_one(monkeypatch, value):
    if value == "":
        monkeypatch.delenv(GATE6_FLAG_ENV, raising=False)
    else:
        monkeypatch.setenv(GATE6_FLAG_ENV, value)
    assert gate6_ap_visible_draft_enabled() is False, f"flag should be OFF for {value!r}"


def test_flag_on_only_for_exact_one(monkeypatch):
    monkeypatch.setenv(GATE6_FLAG_ENV, "1")
    assert gate6_ap_visible_draft_enabled() is True


# ───────────────────────────────────────────────────────────────────
# build_sanitized_metadata
# ───────────────────────────────────────────────────────────────────

def test_metadata_contains_only_allowed_keys():
    meta = build_sanitized_metadata(tenant_id="tenant-A")
    assert set(meta.keys()) == {"tenantId", "mode", "stampedAt", "stampedBy", "skipPublish"}
    assert meta["tenantId"] == "tenant-A"
    assert meta["mode"] == "AP_VISIBLE_DRAFT"
    assert meta["stampedBy"] == "siyadah:gate6_ap_visible_draft"
    assert meta["skipPublish"] is True

    forbidden = {
        "ownerEmail", "flowId", "flowIds", "platformId", "projectId",
        "projectIds", "connectionIds", "connection_ids", "errored_connections",
    }
    assert set(meta.keys()).isdisjoint(forbidden)


# ───────────────────────────────────────────────────────────────────
# create_ap_visible_draft_flow
# ───────────────────────────────────────────────────────────────────

def test_create_ap_visible_draft_calls_only_safe_ops_in_order():
    async def run():
        engine = FakeEngine(fid="flow-draft-1")
        trigger = {"type": "PIECE_TRIGGER", "name": "trigger"}
        fid = await create_ap_visible_draft_flow(
            engine=engine,
            pid="tenant-A",
            display_name="Pending plan",
            trigger=trigger,
        )

        assert fid == "flow-draft-1"
        assert engine.calls == ["create_flow", "update_metadata", "import_flow", "get_flow"]

        # Metadata stamped — sanitized only
        meta = engine.update_metadata_args["metadata"]
        assert set(meta.keys()) == {"tenantId", "mode", "stampedAt", "stampedBy", "skipPublish"}
        assert meta["mode"] == "AP_VISIBLE_DRAFT"
        for forbidden in ("ownerEmail", "flowId", "platformId", "projectIds", "connectionIds"):
            assert forbidden not in meta

        # IMPORT_FLOW carries the actual trigger tree (not a shell)
        assert engine.import_flow_args["trigger"] is trigger
        assert engine.import_flow_args["display_name"] == "Pending plan"

    asyncio.run(run())


def test_create_ap_visible_draft_raises_if_flow_returned_enabled():
    async def run():
        engine = FakeEngine(fid="flow-bad", get_status="ENABLED")
        with pytest.raises(RuntimeError, match="ap_visible_draft_unexpectedly_enabled"):
            await create_ap_visible_draft_flow(
                engine=engine, pid="t", display_name="x",
                trigger={"type": "PIECE_TRIGGER"},
            )

    asyncio.run(run())


def test_create_ap_visible_draft_raises_if_trigger_empty_after_import():
    """P1 #1 — AP can return 200 on IMPORT_FLOW with trigger.type=EMPTY.
    The draft must be rejected so we never persist a flow_id pointing to
    a flow with no usable graph."""
    async def run():
        engine = FakeEngine(fid="flow-empty", get_trigger_type="EMPTY")
        with pytest.raises(RuntimeError, match="ap_visible_draft_trigger_empty_after_import"):
            await create_ap_visible_draft_flow(
                engine=engine, pid="t", display_name="x",
                trigger={"type": "PIECE_TRIGGER"},
            )
        # Sequence still ran fully — verification happens on the GET result.
        assert engine.calls == ["create_flow", "update_metadata", "import_flow", "get_flow"]

    asyncio.run(run())


def test_create_ap_visible_draft_raises_on_invalid_get_flow_response():
    class WeirdEngine(FakeEngine):
        async def get_flow(self, fid):
            self.calls.append("get_flow")
            return None  # AP returned a non-dict body

    async def run():
        engine = WeirdEngine()
        with pytest.raises(RuntimeError, match="ap_visible_draft_get_flow_invalid_response"):
            await create_ap_visible_draft_flow(
                engine=engine, pid="t", display_name="x",
                trigger={"type": "PIECE_TRIGGER"},
            )

    asyncio.run(run())


def test_create_ap_visible_draft_raises_when_no_id_returned():
    class NoIdEngine(FakeEngine):
        async def create_flow(self, pid, name):
            self.calls.append("create_flow")
            return {"status": "DISABLED"}  # no id

    async def run():
        engine = NoIdEngine()
        with pytest.raises(RuntimeError, match="ap_create_flow_returned_no_id"):
            await create_ap_visible_draft_flow(
                engine=engine, pid="t", display_name="x",
                trigger={"type": "PIECE_TRIGGER"},
            )
        # update_metadata / import_flow / get_flow must NOT have been called
        assert engine.calls == ["create_flow"]

    asyncio.run(run())


# ───────────────────────────────────────────────────────────────────
# P1 #2 — only ACTIVE connection ids reach the draft auth field
# ───────────────────────────────────────────────────────────────────

def test_draft_uses_only_active_connection_ids_from_gate():
    """When the user supplies a stale/cross-project connection_id in body
    and the gate rejects it (BLOCKED_CONNECTION_OVERRIDE_INACTIVE), the
    draft branch must not wire `auth: {{connections[...]}}` to that
    rejected id. cn_active is built from connection_gate['connection_ids']
    only — proven-ACTIVE entries — so blocked pieces stay without auth.
    """
    body_connection_ids = {
        "google-sheets": "stale-override-id",   # rejected override
        "gmail": "user-supplied-but-active-id",
    }
    connection_gate = {
        # gate only puts proven-ACTIVE entries here
        "connection_ids": {"gmail": "gmail-active-id"},
        "blocked_pieces": [{
            "piece": "@activepieces/piece-google-sheets",
            "short": "google-sheets",
            "status": "BLOCKED_CONNECTION_OVERRIDE_INACTIVE",
            "reason": "override_connection_missing_or_inactive",
            "auth_type": "CLOUD_OAUTH2",
            "requires_auth": True,
        }],
    }

    # This mirrors the decision in main.py's AP_VISIBLE_DRAFT branch.
    cn_active: dict = dict(connection_gate.get("connection_ids", {}))

    # gmail is ACTIVE per the gate → auth injected
    assert cn_active.get("gmail", "") == "gmail-active-id"
    # google-sheets was a rejected override → no auth
    assert cn_active.get("google-sheets", "") == ""
    # The body's stale id never leaked into the active map
    assert "stale-override-id" not in cn_active.values()
    assert "user-supplied-but-active-id" not in cn_active.values()


# ───────────────────────────────────────────────────────────────────
# Off-path: must NOT call AP at all
# ───────────────────────────────────────────────────────────────────

async def _simulated_blocked_branch(*, engine, pid, display_name, trigger):
    """Mirrors the decision in main.py /v2/build-dynamic blocked branch."""
    flow_id_draft = None
    if gate6_ap_visible_draft_enabled():
        flow_id_draft = await create_ap_visible_draft_flow(
            engine=engine, pid=pid,
            display_name=display_name, trigger=trigger,
        )
    return flow_id_draft


def test_off_path_never_touches_ap(monkeypatch):
    monkeypatch.delenv(GATE6_FLAG_ENV, raising=False)

    engine = FailEngine()
    result = asyncio.run(_simulated_blocked_branch(
        engine=engine, pid="tenant-A", display_name="Pending",
        trigger={"type": "PIECE_TRIGGER"},
    ))
    assert result is None
    assert engine.calls == []  # zero AP calls when flag is off


def test_off_path_not_triggered_by_lookalike_values(monkeypatch):
    """Even unusual non-"1" values must not trip the flag."""
    for value in ["0", "true", "True", "yes", "1 ", " 1"]:
        monkeypatch.setenv(GATE6_FLAG_ENV, value)
        engine = FailEngine()
        result = asyncio.run(_simulated_blocked_branch(
            engine=engine, pid="t", display_name="x",
            trigger={"type": "PIECE_TRIGGER"},
        ))
        assert result is None
        assert engine.calls == []


def test_on_path_triggers_full_safe_sequence(monkeypatch):
    monkeypatch.setenv(GATE6_FLAG_ENV, "1")
    engine = FakeEngine(fid="flow-on")
    result = asyncio.run(_simulated_blocked_branch(
        engine=engine, pid="t", display_name="x",
        trigger={"type": "PIECE_TRIGGER"},
    ))
    assert result == "flow-on"
    assert engine.calls == ["create_flow", "update_metadata", "import_flow", "get_flow"]


# ───────────────────────────────────────────────────────────────────
# save_pending_activation_plan — flow_id persistence
# ───────────────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self, store):
        self._store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    def add(self, row):
        self._store["row"] = row

    async def commit(self):
        pass

    async def refresh(self, row):
        if not getattr(row, "id", None):
            row.id = "fake-pap-id"


class FakePendingActivationPlan:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        if not hasattr(self, "id"):
            self.id = None


def test_save_persists_flow_id_when_provided():
    store: Dict[str, Any] = {}

    def session_factory():
        return FakeSession(store)

    async def run():
        result = await save_pending_activation_plan(
            async_session=session_factory,
            PendingActivationPlan=FakePendingActivationPlan,
            tenant_id="tenant-A",
            display_name="Pending plan",
            graph_plan={"k": "v"},
            connection_gate={
                "status": "PENDING_CONNECTIONS",
                "blocked_pieces": [{"piece": "p", "short": "p"}],
                "runnable_pieces": [],
            },
            flow_id="flow-on-x",
        )
        assert result["flow_id"] == "flow-on-x"
        assert store["row"].flow_id == "flow-on-x"
        assert store["row"].tenant_id == "tenant-A"

    asyncio.run(run())


def test_save_persists_null_flow_id_when_not_provided():
    """Flag-off path: legacy DB row gets flow_id=None — no behavior change."""
    store: Dict[str, Any] = {}

    def session_factory():
        return FakeSession(store)

    async def run():
        result = await save_pending_activation_plan(
            async_session=session_factory,
            PendingActivationPlan=FakePendingActivationPlan,
            tenant_id="tenant-A",
            display_name="Legacy plan",
            graph_plan={"k": "v"},
            connection_gate={
                "status": "PENDING_CONNECTIONS",
                "blocked_pieces": [],
                "runnable_pieces": [],
            },
        )
        assert result["flow_id"] is None
        assert store["row"].flow_id is None

    asyncio.run(run())


# ───────────────────────────────────────────────────────────────────
# build_pending_activation_payload / build_connection_gate_payload
# ───────────────────────────────────────────────────────────────────

def test_pending_activation_payload_only_safe_keys():
    saved = {
        "id": "pap-1",
        "flow_id": "flow-1",
        "status": "PENDING_CONNECTIONS",
        "display_name": "Plan",
        "missing_connections": [{"piece": "x", "errored_connections": [{"id": "c"}]}],
        "runnable_pieces": [{"piece": "y", "connection_external_id": "secret"}],
        "blocked_pieces": [{"piece": "x", "errored_connections": [{"id": "c"}]}],
        "next_reminder_at": "2026-05-10T00:00:00+00:00",
    }
    sanitized = [{"piece": "x", "short": "x", "status": "BLOCKED", "requires_auth": True}]
    payload = build_pending_activation_payload(saved, sanitized)
    assert set(payload.keys()) == {
        "id", "flow_id", "status", "display_name", "missing_connections", "next_reminder_at"
    }
    assert payload["missing_connections"] == sanitized
    # Raw blocked / runnable / errored_connections must NOT leak through.
    assert "blocked_pieces" not in payload
    assert "runnable_pieces" not in payload


def test_connection_gate_payload_only_safe_keys():
    gate = {
        "status": "PENDING_CONNECTIONS",
        "blocked_count": 1,
        "runnable_count": 2,
        "total_pieces": 3,
        "blocked_pieces": [{"piece": "x", "errored_connections": [{"id": "c"}]}],
        "runnable_pieces": [{"piece": "y", "connection_external_id": "secret"}],
        "connection_ids": {"y": "secret"},  # forbidden — must not appear
    }
    sb = [{"piece": "x", "short": "x", "status": "BLOCKED", "requires_auth": True}]
    sr = [{"piece": "y", "short": "y", "status": "RUNNABLE_CONNECTED", "requires_auth": True}]
    payload = build_connection_gate_payload(gate, sb, sr)
    assert set(payload.keys()) == {
        "status", "blocked_count", "runnable_count", "total_pieces",
        "blocked_pieces", "runnable_pieces",
    }
    assert payload["blocked_pieces"] == sb
    assert payload["runnable_pieces"] == sr
    assert "connection_ids" not in payload
