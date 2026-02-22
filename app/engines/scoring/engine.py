"""
Scoring Engine - Aggregates all audit engine results into a unified SEO score.

Scoring Model:
- Each engine contributes a weighted score
- Weights are configurable via settings
- Confidence score accounts for data completeness
- Grade uses A-F scale with numeric sub-scores
- Revenue impact estimates based on traffic × conversion estimates
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from app.core.config import get_settings
from app.engines.base import (
    AuditEngine,
    AuditResult,
    CategoryScore,
    EngineStatus,
    Issue,
    IssueCategory,
    Severity,
    SiteData,
)

logger = structlog.get_logger(__name__)
settings = get_settings()


# ─────────────────────────────────────────────
# Category Weights
# ─────────────────────────────────────────────

CATEGORY_WEIGHTS: dict[IssueCategory, float] = {
    IssueCategory.TECHNICAL: settings.WEIGHT_TECHNICAL,
    IssueCategory.ON_PAGE: settings.WEIGHT_ON_PAGE,
    IssueCategory.CONTENT: settings.WEIGHT_CONTENT,
    IssueCategory.PERFORMANCE: settings.WEIGHT_PERFORMANCE,
    IssueCategory.CRAWLABILITY: settings.WEIGHT_CRAWLABILITY,
    IssueCategory.INTERNAL_LINKS: settings.WEIGHT_INTERNAL_LINKS,
    IssueCategory.SCHEMA: settings.WEIGHT_SCHEMA,
    IssueCategory.AUTHORITY: settings.WEIGHT_AUTHORITY,
}


# ─────────────────────────────────────────────
# Revenue Impact Model
# ─────────────────────────────────────────────

def estimate_revenue_impact(
    issues: list[Issue],
    monthly_traffic: int = 10_000,
    avg_conversion_rate: float = 0.02,
    avg_order_value: float = 100.0,
) -> float:
    """
    Estimate monthly revenue impact of SEO issues.

    Formula:
    Revenue Impact = Σ(traffic_lift_per_issue × conversion_rate × order_value)

    Traffic lift per issue estimated as:
    - CRITICAL: 15-20% traffic gain if fixed
    - HIGH: 8-12%
    - MEDIUM: 3-5%
    - LOW: 1-2%
    """
    traffic_lift_rates = {
        Severity.CRITICAL: 0.15,
        Severity.HIGH: 0.08,
        Severity.MEDIUM: 0.03,
        Severity.LOW: 0.01,
        Severity.INFO: 0.0,
    }

    total_lift = 0.0
    for issue in issues:
        coverage = min(1.0, issue.affected_count / max(1, 1000))
        base_lift = traffic_lift_rates.get(issue.severity, 0.0)
        lift_traffic = monthly_traffic * base_lift * coverage * (issue.impact_score / 100.0)
        revenue = lift_traffic * avg_conversion_rate * avg_order_value
        total_lift += revenue

    return round(total_lift, 2)


# ─────────────────────────────────────────────
# Confidence Score
# ─────────────────────────────────────────────

def calculate_confidence_score(
    engine_results: list[AuditResult],
    pages_crawled: int,
    expected_engines: int = 8,
) -> float:
    """
    Confidence score (0-100): how complete and reliable is this audit?

    Factors:
    - Percentage of engines that ran successfully
    - Pages crawled vs typical site size
    - Data completeness
    """
    successful = len([r for r in engine_results if r.status == EngineStatus.SUCCESS])
    engine_coverage = successful / max(1, expected_engines)

    # Pages coverage (assume 1000 as baseline for full confidence)
    page_coverage = min(1.0, pages_crawled / 1000)

    confidence = (engine_coverage * 0.6 + page_coverage * 0.4) * 100
    return round(confidence, 2)


# ─────────────────────────────────────────────
# Scoring Engine
# ─────────────────────────────────────────────

class ScoringEngine(AuditEngine):
    """
    Aggregates all audit results into an overall SEO score.
    This engine runs AFTER all other engines complete.
    """

    ENGINE_NAME = "scoring"
    CATEGORY = IssueCategory.TECHNICAL  # Placeholder - scoring spans all

    async def run(self, site_data: SiteData) -> AuditResult:
        # The scoring engine receives pre-computed results via site_data.settings
        engine_results: list[AuditResult] = site_data.settings.get("engine_results", [])

        if not engine_results:
            logger.warning("No engine results provided to scoring engine")

        category_scores: list[CategoryScore] = []
        all_issues: list[Issue] = []

        # ── Per-category scoring ─────────────────────────
        weighted_sum = 0.0
        total_weight = 0.0

        for result in engine_results:
            if result.status == EngineStatus.FAILED:
                continue

            weight = CATEGORY_WEIGHTS.get(result.category, 0.0)
            if weight == 0.0:
                continue

            cat_score = CategoryScore(
                category=result.category,
                score=result.score,
                grade=result.grade,
                issues_count=len(result.issues),
                critical_count=len([i for i in result.issues if i.severity == Severity.CRITICAL]),
                high_count=len([i for i in result.issues if i.severity == Severity.HIGH]),
                weight=weight,
            )
            category_scores.append(cat_score)
            all_issues.extend(result.issues)

            weighted_sum += result.score * weight
            total_weight += weight

        # Normalize if not all engines ran
        overall_score = (weighted_sum / total_weight) if total_weight > 0 else 0.0
        overall_score = round(overall_score, 2)

        # ── Confidence ───────────────────────────────────
        confidence = calculate_confidence_score(
            engine_results,
            pages_crawled=len(site_data.pages),
        )

        # ── Revenue Impact ────────────────────────────────
        monthly_traffic = site_data.settings.get("monthly_traffic", 10_000)
        revenue_impact = estimate_revenue_impact(
            issues=all_issues,
            monthly_traffic=monthly_traffic,
        )

        # ── Issue Summary ─────────────────────────────────
        critical_count = len([i for i in all_issues if i.severity == Severity.CRITICAL])
        high_count = len([i for i in all_issues if i.severity == Severity.HIGH])
        medium_count = len([i for i in all_issues if i.severity == Severity.MEDIUM])
        low_count = len([i for i in all_issues if i.severity == Severity.LOW])

        return AuditResult(
            engine_name=self.ENGINE_NAME,
            audit_id=site_data.audit_id,
            status=EngineStatus.SUCCESS,
            category=IssueCategory.TECHNICAL,
            score=overall_score,
            grade=self.calculate_grade(overall_score),
            issues=all_issues,
            pages_analyzed=len(site_data.pages),
            metadata={
                "overall_score": overall_score,
                "overall_grade": self.calculate_grade(overall_score),
                "confidence_score": confidence,
                "estimated_monthly_revenue_impact": revenue_impact,
                "category_scores": [cs.model_dump() for cs in category_scores],
                "issue_summary": {
                    "total": len(all_issues),
                    "critical": critical_count,
                    "high": high_count,
                    "medium": medium_count,
                    "low": low_count,
                },
                "engines_run": len(engine_results),
                "engines_successful": len([r for r in engine_results if r.status == EngineStatus.SUCCESS]),
            },
        )
