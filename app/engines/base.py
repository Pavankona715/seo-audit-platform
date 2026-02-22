"""
Base class and type contracts for all SEO audit engines.
Every engine MUST inherit from AuditEngine and implement run().

Design principles:
- Engines are stateless: all state comes from site_data
- Engines are independent: no engine imports another
- Engines return a standardized AuditResult
- Engines handle their own errors and return partial results on failure
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any
from uuid import UUID

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


# ─────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────

class Severity(str, Enum):
    CRITICAL = "critical"   # Blocking issue - fix immediately
    HIGH = "high"           # Significant impact - fix soon
    MEDIUM = "medium"       # Moderate impact - fix this sprint
    LOW = "low"             # Minor - fix when convenient
    INFO = "info"           # Informational - no action required


class IssueCategory(str, Enum):
    CRAWLABILITY = "crawlability"
    TECHNICAL = "technical"
    ON_PAGE = "on_page"
    CONTENT = "content"
    PERFORMANCE = "performance"
    INTERNAL_LINKS = "internal_links"
    SCHEMA = "schema"
    AUTHORITY = "authority"
    COMPETITOR = "competitor"
    INTERNATIONAL = "international"
    LOCAL = "local"


class EngineStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"       # Ran but with some failures
    FAILED = "failed"
    SKIPPED = "skipped"       # Not applicable for this site


# ─────────────────────────────────────────────
# Core data types
# ─────────────────────────────────────────────

class PageData(BaseModel):
    """Normalized page data passed to engines."""
    url: str
    canonical_url: str | None = None
    status_code: int = 200
    content_type: str = "text/html"
    html: str = ""
    text_content: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    meta: dict[str, str] = Field(default_factory=dict)
    links: list[str] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    structured_data: list[dict[str, Any]] = Field(default_factory=list)
    load_time_ms: float = 0.0
    page_size_bytes: int = 0
    depth: int = 0
    crawled_at: float = Field(default_factory=time.time)

    class Config:
        frozen = False


class SiteData(BaseModel):
    """Aggregated site-level data passed to all engines."""
    audit_id: UUID
    site_id: UUID
    domain: str
    root_url: str
    pages: list[PageData] = Field(default_factory=list)
    sitemap_urls: list[str] = Field(default_factory=list)
    robots_txt: str = ""
    crawl_stats: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


class Issue(BaseModel):
    """A single SEO issue found by an engine."""
    rule_id: str
    title: str
    description: str
    severity: Severity
    category: IssueCategory
    affected_urls: list[str] = Field(default_factory=list)
    affected_count: int = 0
    impact_score: float = Field(ge=0.0, le=100.0, default=0.0)
    effort_score: float = Field(ge=0.0, le=10.0, default=5.0)  # 1-10 scale
    recommendation: str = ""
    documentation_url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class Recommendation(BaseModel):
    """A prioritized fix recommendation."""
    issue_id: str
    priority_rank: int
    title: str
    description: str
    effort: str    # "low" | "medium" | "high"
    impact: str    # "low" | "medium" | "high"
    estimated_traffic_gain: float = 0.0
    estimated_revenue_impact: float = 0.0
    implementation_steps: list[str] = Field(default_factory=list)


class CategoryScore(BaseModel):
    """Score for a single audit category."""
    category: IssueCategory
    score: float = Field(ge=0.0, le=100.0)
    grade: str         # A, B, C, D, F
    issues_count: int = 0
    critical_count: int = 0
    high_count: int = 0
    passed_checks: int = 0
    total_checks: int = 0
    weight: float = 0.0


class AuditResult(BaseModel):
    """Standardized output from every engine."""
    engine_name: str
    audit_id: UUID
    status: EngineStatus
    category: IssueCategory
    score: float = Field(ge=0.0, le=100.0, default=0.0)
    grade: str = "F"
    issues: list[Issue] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    execution_time_ms: float = 0.0
    pages_analyzed: int = 0
    error_message: str | None = None

    class Config:
        use_enum_values = True


# ─────────────────────────────────────────────
# Base Engine
# ─────────────────────────────────────────────

class AuditEngine(ABC):
    """
    Abstract base class for all SEO audit engines.

    All engines MUST:
    1. Implement run(site_data) -> AuditResult
    2. Handle exceptions internally and return partial results
    3. Be stateless - store nothing on self between calls
    4. Return results within CELERY_TASK_SOFT_TIME_LIMIT
    """

    ENGINE_NAME: str = "base"
    CATEGORY: IssueCategory = IssueCategory.TECHNICAL

    def __init__(self):
        self.logger = structlog.get_logger(self.__class__.__name__)

    @abstractmethod
    async def run(self, site_data: SiteData) -> AuditResult:
        """
        Execute the audit engine against site data.

        Args:
            site_data: Aggregated site crawl data

        Returns:
            AuditResult with issues, scores, and recommendations
        """
        ...

    async def execute(self, site_data: SiteData) -> AuditResult:
        """
        Wrapper around run() that adds timing, logging, and error handling.
        Call this instead of run() directly.
        """
        start = time.perf_counter()
        self.logger.info(
            "Engine starting",
            engine=self.ENGINE_NAME,
            audit_id=str(site_data.audit_id),
            domain=site_data.domain,
            page_count=len(site_data.pages),
        )

        try:
            result = await self.run(site_data)
            elapsed = (time.perf_counter() - start) * 1000
            result.execution_time_ms = elapsed
            self.logger.info(
                "Engine complete",
                engine=self.ENGINE_NAME,
                audit_id=str(site_data.audit_id),
                score=result.score,
                issue_count=len(result.issues),
                elapsed_ms=round(elapsed, 2),
            )
            return result

        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            self.logger.error(
                "Engine failed",
                engine=self.ENGINE_NAME,
                audit_id=str(site_data.audit_id),
                error=str(exc),
                elapsed_ms=round(elapsed, 2),
                exc_info=True,
            )
            return AuditResult(
                engine_name=self.ENGINE_NAME,
                audit_id=site_data.audit_id,
                status=EngineStatus.FAILED,
                category=self.CATEGORY,
                score=0.0,
                grade="F",
                execution_time_ms=elapsed,
                error_message=str(exc),
            )

    @staticmethod
    def calculate_grade(score: float) -> str:
        """Convert numeric score to letter grade."""
        if score >= 90:
            return "A"
        elif score >= 80:
            return "B"
        elif score >= 65:
            return "C"
        elif score >= 50:
            return "D"
        return "F"

    @staticmethod
    def normalize_score(raw: float, min_val: float, max_val: float) -> float:
        """Normalize a raw value to 0-100 scale."""
        if max_val == min_val:
            return 100.0
        normalized = ((raw - min_val) / (max_val - min_val)) * 100
        return max(0.0, min(100.0, normalized))
