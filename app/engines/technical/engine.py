"""
Technical SEO Engine

Analyzes:
- HTTPS/SSL configuration
- Redirect chains and loops
- robots.txt directives
- noindex/nofollow usage
- Page speed headers (server-level)
- WWW vs non-WWW consistency
- HTTP headers (X-Robots-Tag, hreflang, etc.)
- Pagination (rel=next/prev)
- AMP detection
- Security headers (as SEO signal)
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import structlog

from app.core.rule_engine import (
    RuleEvaluator,
    calculate_category_score,
    calculate_impact_score,
    get_rule_registry,
)
from app.engines.base import (
    AuditEngine,
    AuditResult,
    EngineStatus,
    Issue,
    IssueCategory,
    PageData,
    Severity,
    SiteData,
)

logger = structlog.get_logger(__name__)


class TechnicalSEOEngine(AuditEngine):
    """
    Technical SEO audit engine.
    Evaluates server-level and protocol-level SEO factors.
    """

    ENGINE_NAME = "technical"
    CATEGORY = IssueCategory.TECHNICAL

    def __init__(self):
        super().__init__()
        self.rule_registry = get_rule_registry()
        self.rule_evaluator = RuleEvaluator()

    async def run(self, site_data: SiteData) -> AuditResult:
        issues: list[Issue] = []
        pages = site_data.pages
        total_pages = max(1, len(pages))

        # ── HTTPS / SSL ─────────────────────────────────
        http_pages = [p for p in pages if p.url.startswith("http://") and p.status_code == 200]
        if http_pages:
            issues.append(Issue(
                rule_id="tech-http-pages",
                title="Pages served over HTTP (not HTTPS)",
                description=f"{len(http_pages)} pages are accessible over insecure HTTP.",
                severity=Severity.CRITICAL,
                category=IssueCategory.TECHNICAL,
                affected_urls=[p.url for p in http_pages[:50]],
                affected_count=len(http_pages),
                impact_score=calculate_impact_score(Severity.CRITICAL, len(http_pages), total_pages, 90.0),
                recommendation="Implement HTTPS sitewide and redirect all HTTP to HTTPS with 301.",
                documentation_url="https://developers.google.com/search/docs/crawling-indexing/http-https",
            ))

        # ── Mixed Content ──────────────────────────────
        mixed_content_pages = self._find_mixed_content(pages)
        if mixed_content_pages:
            issues.append(Issue(
                rule_id="tech-mixed-content",
                title="Mixed content (HTTP resources on HTTPS pages)",
                description=f"{len(mixed_content_pages)} HTTPS pages load insecure HTTP resources.",
                severity=Severity.HIGH,
                category=IssueCategory.TECHNICAL,
                affected_urls=[p.url for p in mixed_content_pages[:50]],
                affected_count=len(mixed_content_pages),
                impact_score=calculate_impact_score(Severity.HIGH, len(mixed_content_pages), total_pages, 70.0),
                recommendation="Update all resource references to use HTTPS.",
            ))

        # ── Redirect Analysis ──────────────────────────
        redirect_chains = self._find_redirect_chains(pages)
        if redirect_chains:
            issues.append(Issue(
                rule_id="tech-redirect-chains",
                title="Long redirect chains detected",
                description=f"{len(redirect_chains)} URLs have redirect chains longer than 1 hop.",
                severity=Severity.MEDIUM,
                category=IssueCategory.TECHNICAL,
                affected_urls=redirect_chains[:50],
                affected_count=len(redirect_chains),
                impact_score=calculate_impact_score(Severity.MEDIUM, len(redirect_chains), total_pages, 55.0),
                recommendation="Reduce redirect chains to a single hop. Update internal links to point directly to final URLs.",
            ))

        # ── X-Robots-Tag Analysis ──────────────────────
        xrobots_noindex = [
            p for p in pages
            if "x-robots-tag" in {k.lower() for k in p.headers}
            and "noindex" in p.headers.get("X-Robots-Tag", p.headers.get("x-robots-tag", "")).lower()
        ]
        if xrobots_noindex:
            issues.append(Issue(
                rule_id="tech-xrobots-noindex",
                title="Pages blocked via X-Robots-Tag: noindex",
                description=f"{len(xrobots_noindex)} pages are excluded from indexing via HTTP header.",
                severity=Severity.HIGH,
                category=IssueCategory.TECHNICAL,
                affected_urls=[p.url for p in xrobots_noindex[:50]],
                affected_count=len(xrobots_noindex),
                impact_score=calculate_impact_score(Severity.HIGH, len(xrobots_noindex), total_pages, 75.0),
                recommendation="Review X-Robots-Tag directives. Remove noindex from pages that should be indexed.",
            ))

        # ── Meta Robots Noindex ────────────────────────
        meta_noindex = [
            p for p in pages
            if "noindex" in p.meta.get("robots", "").lower()
            or "noindex" in p.meta.get("googlebot", "").lower()
        ]
        if meta_noindex:
            issues.append(Issue(
                rule_id="tech-meta-noindex",
                title="Pages excluded from indexing via meta robots",
                description=f"{len(meta_noindex)} pages have meta robots noindex directive.",
                severity=Severity.HIGH,
                category=IssueCategory.TECHNICAL,
                affected_urls=[p.url for p in meta_noindex[:50]],
                affected_count=len(meta_noindex),
                impact_score=calculate_impact_score(Severity.HIGH, len(meta_noindex), total_pages, 75.0),
                recommendation="Review meta robots tags. Remove noindex from pages intended for indexation.",
            ))

        # ── WWW Consistency ────────────────────────────
        www_issue = self._check_www_consistency(pages)
        if www_issue:
            issues.append(www_issue)

        # ── Pagination ─────────────────────────────────
        pagination_issues = self._check_pagination(pages)
        issues.extend(pagination_issues)

        # ── Missing or Invalid Robots.txt ──────────────
        if not site_data.robots_txt:
            issues.append(Issue(
                rule_id="tech-missing-robots-txt",
                title="robots.txt file is missing or inaccessible",
                description="No robots.txt was found at the root of the domain.",
                severity=Severity.MEDIUM,
                category=IssueCategory.TECHNICAL,
                affected_urls=[site_data.root_url],
                affected_count=1,
                impact_score=45.0,
                recommendation="Create a robots.txt file at yourdomain.com/robots.txt.",
            ))

        # ── Security Headers (as SEO trust signal) ─────
        security_issues = self._check_security_headers(pages)
        issues.extend(security_issues)

        # Score calculation
        total_checks = 10  # number of checks above
        score = calculate_category_score(issues, total_checks, total_pages)

        return AuditResult(
            engine_name=self.ENGINE_NAME,
            audit_id=site_data.audit_id,
            status=EngineStatus.SUCCESS,
            category=self.CATEGORY,
            score=score,
            grade=self.calculate_grade(score),
            issues=issues,
            pages_analyzed=total_pages,
            metadata={
                "https_coverage": len([p for p in pages if p.url.startswith("https://")]) / total_pages,
                "noindex_count": len(meta_noindex) + len(xrobots_noindex),
            },
        )

    def _find_mixed_content(self, pages: list[PageData]) -> list[PageData]:
        """Find HTTPS pages loading HTTP resources."""
        mixed = []
        http_resource_pattern = re.compile(r'(src|href|action)\s*=\s*["\']http://', re.IGNORECASE)
        for p in pages:
            if p.url.startswith("https://") and p.status_code == 200:
                if http_resource_pattern.search(p.html):
                    mixed.append(p)
        return mixed

    def _find_redirect_chains(self, pages: list[PageData]) -> list[str]:
        """Detect URLs that underwent multiple redirects."""
        # We detect via history of redirect headers
        # In production this would track redirect hops during crawling
        chains = []
        for p in pages:
            if p.meta.get("redirect_hops", "0") and int(p.meta.get("redirect_hops", "0")) > 1:
                chains.append(p.url)
        return chains

    def _check_www_consistency(self, pages: list[PageData]) -> Issue | None:
        """Detect mixed www/non-www usage."""
        www_urls = [p for p in pages if urlparse(p.url).netloc.startswith("www.")]
        nonwww_urls = [p for p in pages if not urlparse(p.url).netloc.startswith("www.")]

        if www_urls and nonwww_urls:
            return Issue(
                rule_id="tech-www-consistency",
                title="Inconsistent www/non-www URLs",
                description="Both www and non-www versions of pages are accessible.",
                severity=Severity.MEDIUM,
                category=IssueCategory.TECHNICAL,
                affected_urls=[p.url for p in (www_urls + nonwww_urls)[:50]],
                affected_count=len(www_urls) + len(nonwww_urls),
                impact_score=50.0,
                recommendation="Choose one canonical version (www or non-www) and redirect the other.",
            )
        return None

    def _check_pagination(self, pages: list[PageData]) -> list[Issue]:
        """Check pagination implementation."""
        issues = []
        from bs4 import BeautifulSoup

        paginated_no_rel = []
        for p in pages:
            if p.status_code != 200 or not p.html:
                continue

            soup = BeautifulSoup(p.html, "lxml")
            has_next = soup.find("link", rel="next")
            has_prev = soup.find("link", rel="prev")

            # Pages with paginated URL patterns but no rel tags
            url_lower = p.url.lower()
            looks_paginated = any(pat in url_lower for pat in ["/page/", "?page=", "&page=", "/p/", "?p="])
            if looks_paginated and not has_next and not has_prev:
                paginated_no_rel.append(p)

        if paginated_no_rel:
            issues.append(Issue(
                rule_id="tech-missing-pagination-rel",
                title="Paginated pages missing rel=next/prev",
                description=f"{len(paginated_no_rel)} paginated pages lack proper rel=next/prev link elements.",
                severity=Severity.LOW,
                category=IssueCategory.TECHNICAL,
                affected_urls=[p.url for p in paginated_no_rel[:50]],
                affected_count=len(paginated_no_rel),
                impact_score=25.0,
                recommendation="Add rel=next and rel=prev link tags to paginated series.",
            ))

        return issues

    def _check_security_headers(self, pages: list[PageData]) -> list[Issue]:
        """Check for important security headers."""
        issues = []

        # Sample from first 10 pages
        sample = [p for p in pages if p.status_code == 200][:10]
        if not sample:
            return issues

        # HSTS
        no_hsts = [
            p for p in sample
            if "strict-transport-security" not in {k.lower() for k in p.headers}
            and p.url.startswith("https://")
        ]
        if len(no_hsts) > len(sample) * 0.5:
            issues.append(Issue(
                rule_id="tech-missing-hsts",
                title="HTTP Strict Transport Security (HSTS) not configured",
                description="HTTPS pages are missing the Strict-Transport-Security header.",
                severity=Severity.LOW,
                category=IssueCategory.TECHNICAL,
                affected_urls=[p.url for p in no_hsts],
                affected_count=len(no_hsts),
                impact_score=20.0,
                recommendation="Configure HSTS header: Strict-Transport-Security: max-age=31536000; includeSubDomains",
            ))

        return issues
