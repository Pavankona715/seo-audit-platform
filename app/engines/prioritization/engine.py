"""
Prioritization Engine

Ranks SEO fixes using a multi-factor ROI formula.

Priority Score Formula:
  P = (Impact × 0.40) + (Traffic_Potential × 0.25) + (Effort_Inverse × 0.20) + (Severity_Weight × 0.15)

Where:
  Impact         = Normalized issue impact score (0-100)
  Traffic_Potential = Estimated traffic gain from fixing (0-100)
  Effort_Inverse = (10 - effort_score) × 10  → Higher score = easier to fix
  Severity_Weight = CRITICAL=100, HIGH=75, MEDIUM=50, LOW=25

Output: Ordered list of recommendations with implementation steps.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.engines.base import (
    AuditEngine,
    AuditResult,
    EngineStatus,
    Issue,
    IssueCategory,
    Recommendation,
    Severity,
    SiteData,
)

logger = structlog.get_logger(__name__)


SEVERITY_WEIGHTS = {
    Severity.CRITICAL: 100.0,
    Severity.HIGH: 75.0,
    Severity.MEDIUM: 50.0,
    Severity.LOW: 25.0,
    Severity.INFO: 0.0,
}

TRAFFIC_POTENTIAL_BY_SEVERITY = {
    Severity.CRITICAL: 80.0,
    Severity.HIGH: 60.0,
    Severity.MEDIUM: 35.0,
    Severity.LOW: 15.0,
    Severity.INFO: 0.0,
}

IMPLEMENTATION_STEPS: dict[str, list[str]] = {
    "onpage-missing-title": [
        "Identify all pages without title tags using the affected URLs list",
        "Research target keywords for each page category",
        "Write unique titles following the formula: Primary Keyword | Secondary Keyword | Brand",
        "Keep titles between 30-60 characters",
        "Deploy via CMS or template modification",
        "Verify with a re-crawl within 48 hours",
    ],
    "onpage-missing-meta-description": [
        "Export the list of affected pages from the audit report",
        "Write compelling meta descriptions that include the primary keyword",
        "Target 70-160 characters with a clear value proposition",
        "Include a soft call-to-action where appropriate",
        "Update via CMS or developer template",
    ],
    "tech-http-pages": [
        "Purchase and install an SSL certificate (Let's Encrypt for free, or premium CA)",
        "Configure web server to redirect HTTP → HTTPS (301)",
        "Update internal links to use HTTPS",
        "Update canonical tags to HTTPS versions",
        "Verify in Google Search Console that HTTPS version is preferred",
        "Monitor for mixed content warnings after switch",
    ],
    "crawl-4xx-pages": [
        "Export all 4xx URLs from the audit report",
        "For 404s with inbound links: implement 301 redirects to the most relevant page",
        "For 404s with no external links: update or remove internal links pointing to them",
        "Set up monitoring to catch future 404s early",
        "Submit a recrawl request via Google Search Console after fixes",
    ],
    "crawl-duplicate-content": [
        "Identify which version of the duplicate should be canonical",
        "Add rel=canonical tags pointing to the preferred URL",
        "Alternatively, implement 301 redirects from duplicate to canonical",
        "Consolidate PageRank by removing internal links to non-canonical versions",
        "For e-commerce sites, review faceted navigation as a common cause",
    ],
    "onpage-missing-h1": [
        "Audit each affected page for its primary content theme",
        "Write a clear H1 that reflects the page's primary keyword focus",
        "Ensure the H1 is different from the page title (complementary, not identical)",
        "Add via CMS or template change",
    ],
    "onpage-thin-content": [
        "Prioritize high-traffic and high-value pages first",
        "Research what users are looking for on each page (search intent)",
        "Expand content by adding FAQs, examples, tables, or detailed explanations",
        "Target at minimum 500-1000 words for competitive keywords",
        "Add relevant internal links to related content",
        "Monitor rankings after content updates",
    ],
}


def calculate_priority_score(issue: Issue) -> float:
    """
    Multi-factor priority score for issue ordering.

    P = (Impact × 0.40) + (Traffic × 0.25) + (Effort_Ease × 0.20) + (Severity × 0.15)
    """
    impact = issue.impact_score  # 0-100
    traffic_potential = TRAFFIC_POTENTIAL_BY_SEVERITY.get(issue.severity, 0.0)
    effort_ease = (10.0 - issue.effort_score) * 10.0  # Invert: lower effort = higher priority
    severity_score = SEVERITY_WEIGHTS.get(issue.severity, 0.0)

    priority = (
        impact * 0.40
        + traffic_potential * 0.25
        + effort_ease * 0.20
        + severity_score * 0.15
    )
    return round(priority, 2)


def effort_label(score: float) -> str:
    if score <= 3:
        return "low"
    elif score <= 7:
        return "medium"
    return "high"


def impact_label(score: float) -> str:
    if score >= 70:
        return "high"
    elif score >= 40:
        return "medium"
    return "low"


class PrioritizationEngine(AuditEngine):
    """
    Generates ordered, actionable recommendations from all audit issues.
    Runs after all other engines complete.
    """

    ENGINE_NAME = "prioritization"
    CATEGORY = IssueCategory.TECHNICAL  # Spans all categories

    async def run(self, site_data: SiteData) -> AuditResult:
        engine_results = site_data.settings.get("engine_results", [])
        monthly_traffic = site_data.settings.get("monthly_traffic", 10_000)

        # Collect all issues from all engines
        all_issues: list[Issue] = []
        for result in engine_results:
            all_issues.extend(result.issues)

        if not all_issues:
            return AuditResult(
                engine_name=self.ENGINE_NAME,
                audit_id=site_data.audit_id,
                status=EngineStatus.SUCCESS,
                category=self.CATEGORY,
                score=100.0,
                grade="A",
                issues=[],
                recommendations=[],
            )

        # Calculate priority scores and sort
        scored_issues = [
            (issue, calculate_priority_score(issue))
            for issue in all_issues
        ]
        scored_issues.sort(key=lambda x: x[1], reverse=True)

        # Build recommendations (top 50)
        recommendations: list[Recommendation] = []
        for rank, (issue, priority_score) in enumerate(scored_issues[:50], start=1):
            steps = IMPLEMENTATION_STEPS.get(issue.rule_id, [
                "Review the affected URLs listed in the audit report",
                "Implement the recommended fix on the highest-traffic pages first",
                "Validate the fix using Google Search Console or re-crawl",
                "Monitor rankings for affected pages over the next 4-8 weeks",
            ])

            # Estimate traffic gain
            traffic_gain_pct = TRAFFIC_POTENTIAL_BY_SEVERITY.get(issue.severity, 0.0) / 100.0
            estimated_traffic = monthly_traffic * traffic_gain_pct * (issue.impact_score / 100.0)
            estimated_revenue = estimated_traffic * 0.02 * 100.0  # Default CVR and AOV

            rec = Recommendation(
                issue_id=issue.rule_id,
                priority_rank=rank,
                title=issue.title,
                description=issue.recommendation or issue.description,
                effort=effort_label(issue.effort_score),
                impact=impact_label(issue.impact_score),
                estimated_traffic_gain=round(estimated_traffic, 0),
                estimated_revenue_impact=round(estimated_revenue, 2),
                implementation_steps=steps,
            )
            recommendations.append(rec)

        self.logger.info(
            "Prioritization complete",
            total_issues=len(all_issues),
            recommendations=len(recommendations),
        )

        return AuditResult(
            engine_name=self.ENGINE_NAME,
            audit_id=site_data.audit_id,
            status=EngineStatus.SUCCESS,
            category=self.CATEGORY,
            score=100.0,  # Prioritization doesn't have its own score
            grade="N/A",
            issues=all_issues,
            recommendations=recommendations,
            pages_analyzed=len(site_data.pages),
            metadata={
                "total_issues_prioritized": len(all_issues),
                "quick_wins": len([r for r in recommendations if r.effort == "low" and r.impact in ("medium", "high")]),
                "high_effort_high_impact": len([r for r in recommendations if r.effort == "high" and r.impact == "high"]),
            },
        )
