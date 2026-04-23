"""
Siyadah Models — Multi-Tenant Data Layer
==========================================
All tables are isolated by `project_id` for full multi-tenancy.
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    ARRAY, BigInteger, Column, DateTime, ForeignKey, Index,
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
