"""
Siyadah Models — Multi-Tenant Data Layer
==========================================
All tables are isolated by `project_id` for full multi-tenancy.
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    ARRAY, BigInteger, Boolean, Column, DateTime, ForeignKey, Index,
    Integer, LargeBinary, SmallInteger, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, INET, UUID as PG_UUID
from sqlalchemy.orm import relationship

from database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Project(Base):
    __tablename__ = "projects"

    id = Column(String(36), primary_key=True, default=_uuid)
    project_id = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    identity = relationship("ProjectIdentity", back_populates="project", uselist=False, cascade="all, delete-orphan")
    knowledge = relationship("KnowledgeAsset", back_populates="project", uselist=False, cascade="all, delete-orphan")
    settings = relationship("AutonomousSetting", back_populates="project", uselist=False, cascade="all, delete-orphan")


class ProjectIdentity(Base):
    __tablename__ = "project_identities"
    __table_args__ = (Index("ix_pi_project_id", "project_id"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    project_id = Column(String(64), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False)
    sector = Column(String(128))
    language = Column(String(10), default="en")
    business_description = Column(Text)
    website_url = Column(String(512))
    absorbed_at = Column(DateTime(timezone=True))

    project = relationship("Project", back_populates="identity")


class KnowledgeAsset(Base):
    __tablename__ = "knowledge_assets"
    __table_args__ = (Index("ix_ka_project_id", "project_id"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    project_id = Column(String(64), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False)
    faqs = Column(JSONB, default=list)
    tone_of_voice = Column(String(64))
    brand_keywords = Column(JSONB, default=list)

    project = relationship("Project", back_populates="knowledge")


class AutonomousSetting(Base):
    __tablename__ = "autonomous_settings"
    __table_args__ = (Index("ix_as_project_id", "project_id"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    project_id = Column(String(64), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False)
    client_settings = Column(JSONB, default=dict)
    smart_rules = Column(JSONB, default=list)
    auto_respond = Column(String(10), default="off")

    project = relationship("Project", back_populates="settings")


# ═══════════════════════════════════════════════════════════════
# WAVE-1 MULTI-TENANCY ENFORCEMENT
# Per docs/WAVE-1-DESIGN.md §3 — tenant_api_keys + tenant_audit_log.
# These tables back the require_tenant middleware (auth.py).
# ═══════════════════════════════════════════════════════════════

class TenantApiKey(Base):
    """One row per issued API key. Binds an sha256(raw_key) to one project.

    Raw keys are NEVER stored. Comparison is constant-time on the hash
    (auth.require_tenant). Key rotation = insert new row + set revoked_at
    on the old one.
    """
    __tablename__ = "tenant_api_keys"
    __table_args__ = (
        Index("ix_tak_project_id", "project_id"),
        Index("ix_tak_active", "key_hash", postgresql_where=Column("revoked_at").is_(None)),
        # Wave-2 / Pattern B — partial index on tenant_uuid for forward-compat
        # reverse lookups ("list keys for tenant X"). Kept partial so the 11
        # legacy phase-4.x keys with tenant_uuid IS NULL don't bloat it.
        Index("ix_tak_tenant_uuid", "tenant_uuid",
              postgresql_where=Column("tenant_uuid").isnot(None)),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    project_id = Column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    key_hash = Column(String(64), nullable=False, unique=True)  # sha256 hex
    label = Column(String(128), nullable=False)
    scopes = Column(ARRAY(String), nullable=False, default=lambda: ["read", "write"])
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    # Wave-2 / Pattern B — Siyadah tenant identity (decoupled from AP routing).
    # NULL during the transition window for legacy keys; require_tenant
    # falls back to project_id comparison when this is NULL.
    tenant_uuid = Column(PG_UUID(as_uuid=False), nullable=True)


class TenantAuditLog(Base):
    """Every /v2/* request + every violation (dry-run or enforced).

    In dry-run (REQUIRE_TENANT_ENFORCE=false), violations are written
    with http_status=0 and the request is allowed through. In enforced
    mode, http_status reflects the 401/403 returned to the caller.

    Phase-9 (OAuth) adds `event_type` + `event_meta` so OAuth heartbeats
    (initiated/completed/failed/refreshed/revoked) are auditable through
    the same single funnel — one source of truth for forensics.
    """
    __tablename__ = "tenant_audit_log"
    __table_args__ = (
        Index("ix_tal_project_occurred", "project_id", "occurred_at"),
        Index("ix_tal_violation", "violation", postgresql_where=Column("violation").isnot(None)),
        # Phase-9 partial index — only OAuth rows. Cheap because most rows
        # are HTTP-request audits with NULL event_type.
        Index("ix_tal_oauth_events", "project_id", "event_type",
              postgresql_where=Column("event_type").like("oauth.%")),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    project_id = Column(String(64), nullable=True)  # null if unauthenticated
    api_key_hash = Column(String(64), nullable=True)
    endpoint = Column(String(255), nullable=False)  # e.g. "POST /v2/build-complex"
    http_status = Column(SmallInteger, nullable=False)  # 0 = dry-run log only
    payload_digest = Column(String(64), nullable=True)  # sha256(json.dumps(body))
    request_id = Column(String(36), nullable=True)
    remote_ip = Column(INET, nullable=True)
    user_agent = Column(Text, nullable=True)
    violation = Column(String(64), nullable=True)  # null for clean writes
    # ── Phase-9 additions ──
    # event_type is a closed enum; the values are tracked in piece_validator-style
    # constants but enforced at the application layer (not DB enum) so we can
    # add events without migration. Examples: 'oauth.initiated',
    # 'oauth.completed', 'oauth.refreshed', 'oauth.revoked',
    # 'oauth.saga_compensated', 'oauth.webhook_rejected'.
    event_type = Column(String(64), nullable=True)
    # event_meta carries provider/saga_id/scope/error_class etc. — never
    # tokens, never secrets. The logging_config scrubber catches accidents
    # but defence-in-depth: callers MUST scrub before insert.
    event_meta = Column(JSONB, nullable=True)


# ═══════════════════════════════════════════════════════════════
# WAVE-4 / PHASE-4 — FLOW REGISTRY (Orphan Bridge)
# Maps every Activepieces flow_id that the BFF built to its owning
# tenant + piece manifest. Solves the "orphan flow" problem where a
# built flow doesn't appear in the frontend's digital_employees table
# because the BFF never learned about it.
#
# Writes happen via POST /v2/flows/{flow_id}/register-employee.
# Reads via GET /v2/flows?orphan=true (reconciliation).
# ═══════════════════════════════════════════════════════════════

class FlowRegistry(Base):
    """One row per registered digital-employee flow.

    - `flow_id` is the Activepieces flow uuid; unique across all tenants
      because AP itself guarantees uniqueness, but we still scope reads
      by tenant_id so one tenant can't even discover another's ids.
    - `piece_manifest` is the enriched JSON payload the BFF needs to
      write its own `siyadah.digital_employees` row.
    """
    __tablename__ = "flow_registry"
    __table_args__ = (
        Index("ix_fr_tenant_created", "tenant_id", "created_at"),
        Index("ix_fr_trigger_type", "tenant_id", "trigger_type"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    flow_id = Column(String(64), nullable=False, unique=True)
    display_name = Column(String(255), nullable=False)
    trigger_type = Column(String(64), nullable=True)       # e.g. 'webhook', 'schedule'
    webhook_url = Column(Text, nullable=True)
    piece_manifest = Column(JSONB, default=dict)          # pieces + mcp_tool count
    schema_version = Column(String(8), nullable=True)      # AP flow schemaVersion, e.g. "16"
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )


# ═══════════════════════════════════════════════════════════════
# PHASE-8 — PIECE REGISTRY (Sovereign Piece Vault)
# Postgres-persisted cache of every Activepieces piece schema, keyed
# by (name, piece_version) so a future AP piece upgrade creates a new
# row instead of overwriting a version still referenced by live flows.
#
# Populated by scripts/sync_pieces.py (CLI-only; never startup).
# Consumed by piece_validator.validate_trigger_against_registry()
# which hard-stops golden_build() on unknown piece / unknown action /
# missing required field — replaces the "Siyadah Auto-Fill" path.
# ═══════════════════════════════════════════════════════════════

class PieceRegistry(Base):
    """One row per (piece, version) observed from Activepieces.

    - `name` is the canonical AP id, e.g. "@activepieces/piece-gmail".
    - `piece_version` is the raw semver without the `~` prefix, e.g.
      "0.12.1". Flows in the wild store `pieceVersion: "~0.12.1"` —
      the validator strips the leading `~` before lookup.
    - `full_schema` is the complete AP `/v1/pieces/{name}?version=X`
      response. Authoritative source for actions/triggers/props/auth.
    - `actions_index` / `triggers_index` are pre-computed derivations
      used by the hot-path validator so we don't re-walk `full_schema`
      on every flow build. Shape:
        { action_name: { "required_props": [str, ...], "prop_types": {name: type_str} } }
    - `auth_type` mirrors schema.auth.type (OAUTH2 / CUSTOM_AUTH /
      BASIC_AUTH / SECRET_TEXT / None) for the Auth-Compatibility hook.
    - Dynamic metadata (`tier`, `siyadah_tags`, `is_verified`) is
      Siyadah-owned, hot-swappable policy — changing these must not
      require a redeploy. Tier examples: "core" | "premium" | "niche".
    """
    __tablename__ = "piece_registry"
    __table_args__ = (
        Index("ix_pr_name", "name"),
        Index("ix_pr_tier", "tier"),
        Index("ix_pr_verified", "is_verified"),
    )

    name = Column(String(255), primary_key=True)
    piece_version = Column(String(32), primary_key=True)
    display_name = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    logo_url = Column(Text, nullable=True)
    categories = Column(ARRAY(String), nullable=False, default=list)
    auth_type = Column(String(32), nullable=True)
    full_schema = Column(JSONB, nullable=False)
    actions_index = Column(JSONB, nullable=False, default=dict)
    triggers_index = Column(JSONB, nullable=False, default=dict)
    # Siyadah-owned policy metadata — hot-swappable, no code change
    tier = Column(String(16), nullable=True)               # core | premium | niche
    siyadah_tags = Column(ARRAY(String), nullable=False, default=list)
    is_verified = Column(SmallInteger, nullable=False, default=0)  # 0=unverified, 1=verified
    # Phase-12a — Dahae algorithm (§15). Computed offline from
    # actions_index + triggers_index + full_schema.projectUsage.
    # All values 0-100. effective_dahae is what the ranker consumes.
    dahae_score = Column(SmallInteger, nullable=True)
    laziness_score = Column(SmallInteger, nullable=True)
    effective_dahae = Column(SmallInteger, nullable=True)
    dahae_breakdown = Column(JSONB, nullable=True)         # {"breadth":…, "richness":…, …}
    first_synced = Column(DateTime(timezone=True), server_default=func.now())
    last_synced = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )


# ═══════════════════════════════════════════════════════════════
# PHASE-9 — OAUTH ENVELOPE STORAGE + SAGA
# Two tables that together form the persistence layer for Sovereign-
# Grade OAuth (Layers 2 & 5 of the threat model).
#
# encrypted_tokens — bank-level token storage. Every row carries its
#   own DEK (encrypted under the Master Key). Compromise of one DEK
#   leaks one token; compromise of MK forces re-wrap of every DEK
#   without re-encrypting the underlying tokens.
#
# oauth_sagas — distributed-transaction state machine. Tracks each
#   OAuth handshake from INITIATED → TOKEN_OBTAINED →
#   AP_CONNECTION_CREATED → COMPLETED. On any post-token failure, the
#   compensating rollback wipes the encrypted_tokens row and marks
#   the saga COMPENSATED — no orphaned tokens.
# ═══════════════════════════════════════════════════════════════

class EncryptedToken(Base):
    """One row per active (tenant, provider, account) OAuth credential.

    Envelope-encrypted (siyadah_crypto.py):
      • DEK = 32 random bytes per row
      • encrypted_access_token  = AES-GCM-256(access_token, DEK, AAD)
      • encrypted_refresh_token = AES-GCM-256(refresh_token, DEK, AAD)
      • wrapped_dek             = AES-GCM-256(DEK, MK, AAD)

    The 12-byte IV is stored alongside each ciphertext (iv_access,
    iv_refresh, iv_dek). encryption_version is stamped per row so a
    future cipher upgrade can co-exist with old rows during migration.

    NEVER store plaintext tokens — application code reads via
    siyadah_crypto.CryptoProvider; if you find yourself wanting plaintext
    in a query, the architecture has drifted.
    """
    __tablename__ = "encrypted_tokens"
    __table_args__ = (
        Index("ix_et_tenant_provider_active", "tenant_id", "provider",
              postgresql_where=Column("status").like("ACTIVE")),
        Index("ix_et_refresh_due", "refresh_at",
              postgresql_where=Column("status").like("ACTIVE")),
        # One ACTIVE row per (tenant, provider, provider_account_id).
        # If a user re-auths same Google account, we soft-revoke the old
        # row first and insert a new one — never two ACTIVEs side-by-side.
        UniqueConstraint(
            "tenant_id", "provider", "provider_account_id",
            "status", name="uq_et_tenant_provider_account_status",
        ),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    provider = Column(String(32), nullable=False)              # 'google', 'slack', 'hubspot'
    provider_account_id = Column(String(255), nullable=True)   # e.g. Google sub claim
    # Ciphertexts + IVs (LargeBinary maps to Postgres bytea)
    encrypted_access_token = Column(LargeBinary, nullable=False)
    encrypted_refresh_token = Column(LargeBinary, nullable=True)  # not all providers refresh
    wrapped_dek = Column(LargeBinary, nullable=False)
    iv_access = Column(LargeBinary, nullable=False)
    iv_refresh = Column(LargeBinary, nullable=True)
    iv_dek = Column(LargeBinary, nullable=False)
    encryption_version = Column(SmallInteger, nullable=False, default=1)
    # Token metadata (not the secrets themselves — safe to query)
    scopes = Column(ARRAY(String), nullable=False, default=list)
    expires_at = Column(DateTime(timezone=True), nullable=True)    # access-token expiry
    refresh_at = Column(DateTime(timezone=True), nullable=True)    # = expires_at - 5min
    refresh_count = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="ACTIVE")  # ACTIVE|REVOKED|EXPIRED|CORRUPTED
    # AP-side mirror — the externalId of the Activepieces connection
    # this row backs. Filled at AP_CONNECTION_CREATED saga step.
    ap_connection_external_id = Column(String(255), nullable=True)
    # ── Phase 4.6 hardening (Q4) ──
    # On a successful provider refresh, the worker rotates ciphertexts
    # in DB and pushes the new access_token to AP. If the AP push fails,
    # we don't want to wait for `refresh_at` to come around again (12h
    # later) — that creates a long staleness window AND wastes a refresh
    # token call. Instead, ap_sync_pending=true makes the next worker
    # cycle pick this row up via a SHORT path: decrypt the (already-
    # fresh) tokens and retry AP upsert ONLY, no provider call.
    ap_sync_pending = Column(Boolean, nullable=False, default=False)
    # ── Phase 4.6 hardening (Q1) ──
    # Cross-replica safety. The worker takes a lease on each row before
    # processing — any other replica's SELECT FOR UPDATE SKIP LOCKED
    # filters this row out until the lease expires. Set in the same
    # transaction as the SELECT. Cleared by every terminal handler.
    # Crashed-worker recovery: if a worker dies mid-refresh, the lease
    # naturally expires and the next cycle picks the row up.
    processing_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )


class OAuthSaga(Base):
    """Distributed-transaction state machine for one OAuth handshake.

    State transitions (enforced at application layer):

        INITIATED ─► TOKEN_OBTAINED ─► AP_CONNECTION_CREATED ─► COMPLETED
            │             │                      │
            │             │                      └─► (failure) COMPENSATED → wipe encrypted_token
            │             │
            │             └─► (failure) COMPENSATED
            │
            └─► (no callback in 5 min) FAILED

    A REVOKED state is reached out-of-band via Layer-4 webhooks.

    pkce_verifier is stored plaintext during the saga's 5-minute window.
    Risk window: a DB read during that window could let an attacker
    complete the handshake. Mitigation: aggressive expiry-cleanup +
    consider envelope-encrypting in v2 if threat model tightens.
    """
    __tablename__ = "oauth_sagas"
    __table_args__ = (
        Index("ix_sg_tenant_status", "tenant_id", "status"),
        # For the cleanup worker: all sagas still INITIATED past their TTL
        Index("ix_sg_initiated_expiry", "expires_at",
              postgresql_where=Column("status").like("INITIATED")),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    provider = Column(String(32), nullable=False)
    scope = Column(ARRAY(String), nullable=False, default=list)
    # state_nonce mirrors the one in Redis. Unique constraint here means
    # two sagas can't share a nonce — a second register would fail loudly
    # rather than silently overwriting. Cross-process consistency w/ Redis.
    state_nonce = Column(String(64), nullable=False, unique=True)
    pkce_verifier = Column(Text, nullable=False)              # 64-char b64url
    status = Column(String(32), nullable=False, default="INITIATED")
    # Filled at AP_CONNECTION_CREATED — links saga to the AP-side connection.
    ap_connection_external_id = Column(String(255), nullable=True)
    # Filled at TOKEN_OBTAINED — links saga to the encrypted_tokens row.
    encrypted_token_id = Column(
        String(36),
        ForeignKey("encrypted_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    initiated_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)   # initiated_at + 5min
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # Failure forensics — what step failed, why
    failure_reason = Column(Text, nullable=True)
    failure_step = Column(String(32), nullable=True)               # 'callback'|'token_exchange'|'encrypt'|'ap_create'
