"""
Database Models - Full relational schema for the SEO audit platform.

Design decisions:
- UUID primary keys (no sequential int exposure)
- JSON columns for flexible metadata storage
- Proper indexes for all foreign keys and query patterns
- Soft deletes via deleted_at
- Full audit trail with created_at/updated_at
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


# ─────────────────────────────────────────────
# Mixins
# ─────────────────────────────────────────────

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False)


# ─────────────────────────────────────────────
# Organizations / Teams
# ─────────────────────────────────────────────

class Organization(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(String(50), default="free", nullable=False)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    sites: Mapped[list["Site"]] = relationship("Site", back_populates="organization")


# ─────────────────────────────────────────────
# Sites
# ─────────────────────────────────────────────

class Site(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A website being audited."""
    __tablename__ = "sites"

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    root_url: Mapped[str] = mapped_column(String(500), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=True)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)  # Crawl settings
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Latest cached stats for quick access
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_grade: Mapped[str | None] = mapped_column(String(2), nullable=True)

    organization: Mapped[Organization] = relationship("Organization", back_populates="sites")
    audits: Mapped[list["Audit"]] = relationship("Audit", back_populates="site", order_by="Audit.created_at.desc()")

    __table_args__ = (
        Index("ix_sites_organization_id", "organization_id"),
        Index("ix_sites_domain", "domain"),
        UniqueConstraint("organization_id", "domain", name="uq_sites_org_domain"),
    )


# ─────────────────────────────────────────────
# Audits
# ─────────────────────────────────────────────

class Audit(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A complete SEO audit run for a site."""
    __tablename__ = "audits"

    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sites.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    # pending | crawling | analyzing | complete | failed

    # Celery task tracking
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Configuration snapshot at time of audit
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Results
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    overall_grade: Mapped[str | None] = mapped_column(String(2), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_revenue_impact: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Crawl stats
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    issues_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    critical_issues: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Timing
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    site: Mapped[Site] = relationship("Site", back_populates="audits")
    pages: Mapped[list["Page"]] = relationship("Page", back_populates="audit")
    issues: Mapped[list["AuditIssue"]] = relationship("AuditIssue", back_populates="audit")
    engine_results: Mapped[list["EngineResult"]] = relationship("EngineResult", back_populates="audit")
    scores: Mapped[list["CategoryScoreRecord"]] = relationship("CategoryScoreRecord", back_populates="audit")

    __table_args__ = (
        Index("ix_audits_site_id", "site_id"),
        Index("ix_audits_status", "status"),
        Index("ix_audits_created_at", "created_at"),
    )


# ─────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────

class Page(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single crawled page within an audit."""
    __tablename__ = "pages"

    audit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("audits.id"), nullable=False)
    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sites.id"), nullable=False)

    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(200), default="text/html", nullable=False)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Page signals
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    meta_description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    h1: Mapped[str | None] = mapped_column(String(512), nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Performance
    load_time_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    page_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Counts
    internal_links_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    external_links_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    images_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    images_missing_alt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Flags
    is_indexable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    has_canonical: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Full metadata (flexible)
    meta_tags: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    structured_data: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    headers: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    audit: Mapped[Audit] = relationship("Audit", back_populates="pages")
    issues: Mapped[list["AuditIssue"]] = relationship("AuditIssue", secondary="page_issues", back_populates="pages")

    __table_args__ = (
        Index("ix_pages_audit_id", "audit_id"),
        Index("ix_pages_site_id", "site_id"),
        Index("ix_pages_status_code", "status_code"),
        Index("ix_pages_url", "url"),
    )


# ─────────────────────────────────────────────
# Issues
# ─────────────────────────────────────────────

class AuditIssue(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single SEO issue identified during an audit."""
    __tablename__ = "audit_issues"

    audit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("audits.id"), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    impact_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    effort_score: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    affected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    affected_urls: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    documentation_url: Mapped[str] = mapped_column(String(500), nullable=True)
    extra_data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Resolution tracking
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ignored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    audit: Mapped[Audit] = relationship("Audit", back_populates="issues")
    pages: Mapped[list[Page]] = relationship("Page", secondary="page_issues", back_populates="issues")

    __table_args__ = (
        Index("ix_audit_issues_audit_id", "audit_id"),
        Index("ix_audit_issues_rule_id", "rule_id"),
        Index("ix_audit_issues_severity", "severity"),
        Index("ix_audit_issues_category", "category"),
    )


# Junction table: Page ↔ Issue (many-to-many)
from sqlalchemy import Table, Column
page_issues = Table(
    "page_issues",
    Base.metadata,
    Column("page_id", UUID(as_uuid=True), ForeignKey("pages.id"), primary_key=True),
    Column("issue_id", UUID(as_uuid=True), ForeignKey("audit_issues.id"), primary_key=True),
)


# ─────────────────────────────────────────────
# Engine Results
# ─────────────────────────────────────────────

class EngineResult(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Per-engine audit result."""
    __tablename__ = "engine_results"

    audit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("audits.id"), nullable=False)
    engine_name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    grade: Mapped[str] = mapped_column(String(2), nullable=False, default="F")
    execution_time_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pages_analyzed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    issues_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extra_data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    audit: Mapped[Audit] = relationship("Audit", back_populates="engine_results")

    __table_args__ = (
        Index("ix_engine_results_audit_id", "audit_id"),
        UniqueConstraint("audit_id", "engine_name", name="uq_engine_results_audit_engine"),
    )


# ─────────────────────────────────────────────
# Category Scores
# ─────────────────────────────────────────────

class CategoryScoreRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Score for each category within an audit. Used for trend analysis."""
    __tablename__ = "category_scores"

    audit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("audits.id"), nullable=False)
    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sites.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    grade: Mapped[str] = mapped_column(String(2), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    issues_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    critical_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    audit: Mapped[Audit] = relationship("Audit", back_populates="scores")

    __table_args__ = (
        Index("ix_category_scores_audit_id", "audit_id"),
        Index("ix_category_scores_site_id_category", "site_id", "category"),
    )


# ─────────────────────────────────────────────
# Recommendations
# ─────────────────────────────────────────────

class AuditRecommendation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Prioritized recommendations from the prioritization engine."""
    __tablename__ = "audit_recommendations"

    audit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("audits.id"), nullable=False)
    issue_id: Mapped[str] = mapped_column(String(100), nullable=False)
    priority_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    effort: Mapped[str] = mapped_column(String(20), nullable=False)
    impact: Mapped[str] = mapped_column(String(20), nullable=False)
    estimated_traffic_gain: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    estimated_revenue_impact: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    implementation_steps: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)

    __table_args__ = (
        Index("ix_audit_recommendations_audit_id", "audit_id"),
        Index("ix_audit_recommendations_rank", "audit_id", "priority_rank"),
    )
