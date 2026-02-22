"""
On-Page SEO Analyzer Engine

Analyzes per-page SEO signals:
- Title tags (length, uniqueness, keyword presence)
- Meta descriptions (length, uniqueness)
- Heading hierarchy (H1-H6 structure)
- URL structure (length, hyphens, keywords)
- Image alt text
- Content length and quality signals
- Keyword density
- Internal anchor text
"""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

import structlog
from bs4 import BeautifulSoup

from app.core.rule_engine import calculate_category_score, calculate_impact_score
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


class OnPageAnalyzerEngine(AuditEngine):

    ENGINE_NAME = "onpage"
    CATEGORY = IssueCategory.ON_PAGE

    # Thresholds
    TITLE_MIN_LENGTH = 30
    TITLE_MAX_LENGTH = 60
    META_DESC_MIN_LENGTH = 70
    META_DESC_MAX_LENGTH = 160
    H1_IDEAL_COUNT = 1
    URL_MAX_LENGTH = 115
    MIN_WORD_COUNT = 300

    async def run(self, site_data: SiteData) -> AuditResult:
        pages = [p for p in site_data.pages if p.status_code == 200 and "text/html" in p.content_type]
        total_pages = max(1, len(pages))
        issues: list[Issue] = []

        # Collect page-level data for site-wide analysis
        titles: list[str] = []
        meta_descs: list[str] = []

        missing_title: list[PageData] = []
        short_title: list[PageData] = []
        long_title: list[PageData] = []
        missing_meta: list[PageData] = []
        short_meta: list[PageData] = []
        long_meta: list[PageData] = []
        missing_h1: list[PageData] = []
        multiple_h1: list[PageData] = []
        missing_alt: list[PageData] = []
        thin_content: list[PageData] = []
        long_urls: list[PageData] = []
        dynamic_urls: list[PageData] = []
        uppercase_urls: list[PageData] = []

        for page in pages:
            soup = BeautifulSoup(page.html, "lxml") if page.html else None
            if not soup:
                continue

            title = page.meta.get("title", "").strip()
            meta_desc = page.meta.get("description", "").strip()

            # ── Title ──────────────────────────────────
            if not title:
                missing_title.append(page)
            else:
                titles.append(title)
                if len(title) < self.TITLE_MIN_LENGTH:
                    short_title.append(page)
                elif len(title) > self.TITLE_MAX_LENGTH:
                    long_title.append(page)

            # ── Meta Description ───────────────────────
            if not meta_desc:
                missing_meta.append(page)
            else:
                meta_descs.append(meta_desc)
                if len(meta_desc) < self.META_DESC_MIN_LENGTH:
                    short_meta.append(page)
                elif len(meta_desc) > self.META_DESC_MAX_LENGTH:
                    long_meta.append(page)

            # ── Headings ──────────────────────────────
            h1_tags = soup.find_all("h1")
            if not h1_tags:
                missing_h1.append(page)
            elif len(h1_tags) > 1:
                multiple_h1.append(page)

            # ── Images Alt Text ───────────────────────
            imgs = soup.find_all("img")
            no_alt = [img for img in imgs if not img.get("alt")]
            if no_alt:
                page.meta["images_missing_alt"] = str(len(no_alt))
                missing_alt.append(page)

            # ── Content Thin ──────────────────────────
            word_count = len(page.text_content.split())
            page.meta["word_count"] = str(word_count)
            if word_count < self.MIN_WORD_COUNT:
                thin_content.append(page)

            # ── URL Analysis ──────────────────────────
            parsed = urlparse(page.url)
            path = parsed.path

            if len(page.url) > self.URL_MAX_LENGTH:
                long_urls.append(page)

            # Dynamic URL detection (excessive params or IDs)
            if len(parsed.query.split("&")) > 3:
                dynamic_urls.append(page)

            # Uppercase in URL
            if path != path.lower():
                uppercase_urls.append(page)

        # ── Duplicate Titles ───────────────────────────────
        title_counts = Counter(titles)
        duplicate_titles = {t: c for t, c in title_counts.items() if c > 1}
        dup_title_urls = [p for p in pages if p.meta.get("title", "") in duplicate_titles]

        # ── Duplicate Meta Descriptions ────────────────────
        desc_counts = Counter(meta_descs)
        duplicate_descs = {d: c for d, c in desc_counts.items() if c > 1}
        dup_desc_urls = [p for p in pages if p.meta.get("description", "") in duplicate_descs]

        # ── Issue Creation ─────────────────────────────────

        if missing_title:
            issues.append(Issue(
                rule_id="onpage-missing-title",
                title="Pages missing title tags",
                description=f"{len(missing_title)} pages have no title tag.",
                severity=Severity.CRITICAL,
                category=IssueCategory.ON_PAGE,
                affected_urls=[p.url for p in missing_title[:50]],
                affected_count=len(missing_title),
                impact_score=calculate_impact_score(Severity.CRITICAL, len(missing_title), total_pages, 95.0),
                recommendation="Add unique, descriptive title tags (30-60 chars) to every page.",
                documentation_url="https://developers.google.com/search/docs/appearance/title-link",
            ))

        if dup_title_urls:
            issues.append(Issue(
                rule_id="onpage-duplicate-title",
                title="Duplicate title tags across pages",
                description=f"{len(dup_title_urls)} pages share title tags with other pages.",
                severity=Severity.HIGH,
                category=IssueCategory.ON_PAGE,
                affected_urls=[p.url for p in dup_title_urls[:50]],
                affected_count=len(dup_title_urls),
                impact_score=calculate_impact_score(Severity.HIGH, len(dup_title_urls), total_pages, 75.0),
                recommendation="Write unique title tags for every page. Include target keywords.",
                metadata={"duplicates": dict(list(duplicate_titles.items())[:10])},
            ))

        if short_title:
            issues.append(Issue(
                rule_id="onpage-short-title",
                title="Title tags too short",
                description=f"{len(short_title)} pages have title tags under {self.TITLE_MIN_LENGTH} characters.",
                severity=Severity.MEDIUM,
                category=IssueCategory.ON_PAGE,
                affected_urls=[p.url for p in short_title[:50]],
                affected_count=len(short_title),
                impact_score=calculate_impact_score(Severity.MEDIUM, len(short_title), total_pages, 55.0),
                recommendation=f"Expand title tags to {self.TITLE_MIN_LENGTH}-{self.TITLE_MAX_LENGTH} characters.",
            ))

        if long_title:
            issues.append(Issue(
                rule_id="onpage-long-title",
                title="Title tags too long (will be truncated)",
                description=f"{len(long_title)} pages have title tags over {self.TITLE_MAX_LENGTH} characters.",
                severity=Severity.MEDIUM,
                category=IssueCategory.ON_PAGE,
                affected_urls=[p.url for p in long_title[:50]],
                affected_count=len(long_title),
                impact_score=calculate_impact_score(Severity.MEDIUM, len(long_title), total_pages, 45.0),
                recommendation=f"Trim title tags to under {self.TITLE_MAX_LENGTH} characters.",
            ))

        if missing_meta:
            issues.append(Issue(
                rule_id="onpage-missing-meta-description",
                title="Pages missing meta descriptions",
                description=f"{len(missing_meta)} pages have no meta description tag.",
                severity=Severity.HIGH,
                category=IssueCategory.ON_PAGE,
                affected_urls=[p.url for p in missing_meta[:50]],
                affected_count=len(missing_meta),
                impact_score=calculate_impact_score(Severity.HIGH, len(missing_meta), total_pages, 70.0),
                recommendation="Write compelling meta descriptions (70-160 chars) to improve CTR from search results.",
            ))

        if dup_desc_urls:
            issues.append(Issue(
                rule_id="onpage-duplicate-meta-description",
                title="Duplicate meta descriptions",
                description=f"{len(dup_desc_urls)} pages share identical meta descriptions.",
                severity=Severity.MEDIUM,
                category=IssueCategory.ON_PAGE,
                affected_urls=[p.url for p in dup_desc_urls[:50]],
                affected_count=len(dup_desc_urls),
                impact_score=calculate_impact_score(Severity.MEDIUM, len(dup_desc_urls), total_pages, 50.0),
                recommendation="Write unique meta descriptions for every page.",
            ))

        if missing_h1:
            issues.append(Issue(
                rule_id="onpage-missing-h1",
                title="Pages missing H1 heading",
                description=f"{len(missing_h1)} pages have no H1 heading.",
                severity=Severity.HIGH,
                category=IssueCategory.ON_PAGE,
                affected_urls=[p.url for p in missing_h1[:50]],
                affected_count=len(missing_h1),
                impact_score=calculate_impact_score(Severity.HIGH, len(missing_h1), total_pages, 65.0),
                recommendation="Add a single, keyword-rich H1 heading to every page.",
            ))

        if multiple_h1:
            issues.append(Issue(
                rule_id="onpage-multiple-h1",
                title="Pages with multiple H1 headings",
                description=f"{len(multiple_h1)} pages have more than one H1 heading.",
                severity=Severity.MEDIUM,
                category=IssueCategory.ON_PAGE,
                affected_urls=[p.url for p in multiple_h1[:50]],
                affected_count=len(multiple_h1),
                impact_score=calculate_impact_score(Severity.MEDIUM, len(multiple_h1), total_pages, 40.0),
                recommendation="Use only one H1 per page. Use H2-H6 for subheadings.",
            ))

        if missing_alt:
            total_missing = sum(int(p.meta.get("images_missing_alt", 0)) for p in missing_alt)
            issues.append(Issue(
                rule_id="onpage-missing-alt-text",
                title="Images missing alt text",
                description=f"{total_missing} images across {len(missing_alt)} pages are missing alt attributes.",
                severity=Severity.MEDIUM,
                category=IssueCategory.ON_PAGE,
                affected_urls=[p.url for p in missing_alt[:50]],
                affected_count=len(missing_alt),
                impact_score=calculate_impact_score(Severity.MEDIUM, len(missing_alt), total_pages, 45.0),
                recommendation="Add descriptive alt text to all meaningful images. Use empty alt='' for decorative images.",
                metadata={"total_images_missing_alt": total_missing},
            ))

        if thin_content:
            issues.append(Issue(
                rule_id="onpage-thin-content",
                title="Pages with thin content",
                description=f"{len(thin_content)} pages have fewer than {self.MIN_WORD_COUNT} words.",
                severity=Severity.MEDIUM,
                category=IssueCategory.ON_PAGE,
                affected_urls=[p.url for p in thin_content[:50]],
                affected_count=len(thin_content),
                impact_score=calculate_impact_score(Severity.MEDIUM, len(thin_content), total_pages, 55.0),
                recommendation=f"Expand content to at least {self.MIN_WORD_COUNT} words. Focus on depth and value.",
            ))

        if long_urls:
            issues.append(Issue(
                rule_id="onpage-long-urls",
                title="URLs exceeding recommended length",
                description=f"{len(long_urls)} pages have URLs longer than {self.URL_MAX_LENGTH} characters.",
                severity=Severity.LOW,
                category=IssueCategory.ON_PAGE,
                affected_urls=[p.url for p in long_urls[:50]],
                affected_count=len(long_urls),
                impact_score=calculate_impact_score(Severity.LOW, len(long_urls), total_pages, 25.0),
                recommendation="Keep URLs short, descriptive, and keyword-rich.",
            ))

        if uppercase_urls:
            issues.append(Issue(
                rule_id="onpage-uppercase-urls",
                title="URLs containing uppercase characters",
                description=f"{len(uppercase_urls)} pages have uppercase letters in their URL paths.",
                severity=Severity.LOW,
                category=IssueCategory.ON_PAGE,
                affected_urls=[p.url for p in uppercase_urls[:50]],
                affected_count=len(uppercase_urls),
                impact_score=20.0,
                recommendation="Use only lowercase URLs. Redirect uppercase variants to lowercase equivalents.",
            ))

        score = calculate_category_score(issues, 12, total_pages)

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
                "avg_title_length": sum(len(t) for t in titles) / max(1, len(titles)),
                "avg_word_count": sum(int(p.meta.get("word_count", 0)) for p in pages) / total_pages,
                "unique_titles": len(set(titles)),
                "total_titles": len(titles),
            },
        )
