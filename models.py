"""
Siyadah Models — Multi-Tenant Data Layer
==========================================
All tables are isolated by `project_id` for full multi-tenancy.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
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
