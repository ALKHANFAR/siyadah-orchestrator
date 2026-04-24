"""
Siyadah Models — Multi-Tenant Data Layer
==========================================
All tables are isolated by `project_id` for full multi-tenancy.
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    ARRAY, BigInteger, Boolean, Column, DateTime, ForeignKey, Index,
    SmallInteger, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB, INET
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


class TenantAuditLog(Base):
    """Every /v2/* request + every violation (dry-run or enforced).

    In dry-run (REQUIRE_TENANT_ENFORCE=false), violations are written
    with http_status=0 and the request is allowed through. In enforced
    mode, http_status reflects the 401/403 returned to the caller.
    """
    __tablename__ = "tenant_audit_log"
    __table_args__ = (
        Index("ix_tal_project_occurred", "project_id", "occurred_at"),
        Index("ix_tal_violation", "violation", postgresql_where=Column("violation").isnot(None)),
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
    # Phase 9 (Gap 2): flow is also advertised as an MCP tool in AP's
    # per-project MCP server. NULL = not registered (either not tried
    # yet or AP rejected).
    mcp_tool_name = Column(String(64), nullable=True, index=True)
    mcp_registered_at = Column(DateTime(timezone=True), nullable=True)
    # Phase 10 (Gap 1 — webhook security): opt-in HMAC proxy. Secret
    # itself is NEVER stored (derived from WEBHOOK_SIGNING_MASTER_KEY +
    # flow_id). These flags control behaviour only.
    secure_webhook = Column(Boolean, default=False, nullable=False)
    skip_webhook_auth = Column(Boolean, default=False, nullable=False)
    webhook_scheme = Column(String(32), default="siyadah", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
