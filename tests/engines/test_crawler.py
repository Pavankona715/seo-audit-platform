"""
Tests for the Crawler Engine.
Uses httpx MockTransport to avoid real network calls.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.base import SiteData, PageData
from app.engines.crawler.engine import (
    CrawlerEngine,
    RateLimiter,
    URLNormalizer,
)


# ─────────────────────────────────────────────
# URL Normalizer Tests
# ─────────────────────────────────────────────

class TestURLNormalizer:

    def test_normalizes_relative_url(self):
        result = URLNormalizer.normalize("/about", "https://example.com/")
        assert result == "https://example.com/about"

    def test_removes_fragment(self):
        result = URLNormalizer.normalize("https://example.com/page#section", "https://example.com")
        assert result == "https://example.com/page"

    def test_removes_utm_params(self):
        result = URLNormalizer.normalize(
            "https://example.com/page?utm_source=google&id=123",
            "https://example.com"
        )
        assert "utm_source" not in result
        assert "id=123" in result

    def test_skips_pdf(self):
        result = URLNormalizer.normalize("https://example.com/doc.pdf", "https://example.com")
        assert result is None

    def test_skips_mailto(self):
        result = URLNormalizer.normalize("mailto:test@example.com", "https://example.com")
        assert result is None

    def test_normalizes_trailing_slash(self):
        result = URLNormalizer.normalize("https://example.com/page/", "https://example.com")
        assert result == "https://example.com/page"

    def test_root_trailing_slash_preserved(self):
        result = URLNormalizer.normalize("https://example.com/", "https://example.com")
        assert result == "https://example.com/"

    def test_same_domain_check(self):
        assert URLNormalizer.is_same_domain("https://example.com/page", "example.com")
        assert URLNormalizer.is_same_domain("https://sub.example.com/page", "example.com")
        assert not URLNormalizer.is_same_domain("https://other.com/page", "example.com")

    def test_fingerprint_consistency(self):
        fp1 = URLNormalizer.url_fingerprint("https://example.com/page")
        fp2 = URLNormalizer.url_fingerprint("https://example.com/page")
        assert fp1 == fp2

    def test_fingerprint_uniqueness(self):
        fp1 = URLNormalizer.url_fingerprint("https://example.com/page1")
        fp2 = URLNormalizer.url_fingerprint("https://example.com/page2")
        assert fp1 != fp2


# ─────────────────────────────────────────────
# Rate Limiter Tests
# ─────────────────────────────────────────────

class TestRateLimiter:

    @pytest.mark.asyncio
    async def test_acquire_does_not_raise(self):
        limiter = RateLimiter(rate=100.0, max_tokens=10)
        # Should not raise or sleep with high rate
        await limiter.acquire()

    @pytest.mark.asyncio
    async def test_tokens_decrease_on_acquire(self):
        limiter = RateLimiter(rate=100.0, max_tokens=5)
        initial_tokens = limiter.tokens
        await limiter.acquire()
        assert limiter.tokens < initial_tokens


# ─────────────────────────────────────────────
# Crawler Engine Tests
# ─────────────────────────────────────────────

class TestCrawlerEngine:

    @pytest.fixture
    def site_data(self):
        return SiteData(
            audit_id=uuid.uuid4(),
            site_id=uuid.uuid4(),
            domain="example.com",
            root_url="https://example.com",
            settings={
                "max_pages": 10,
                "max_depth": 2,
                "concurrency": 2,
                "rate_limit_rps": 100.0,
                "js_render": False,
            }
        )

    @pytest.mark.asyncio
    async def test_crawl_issue_4xx_detection(self):
        engine = CrawlerEngine()
        pages = [
            PageData(url="https://example.com/404", status_code=404),
            PageData(url="https://example.com/404-2", status_code=404),
            PageData(url="https://example.com/ok", status_code=200),
        ]
        issues = engine._analyze_crawl_issues(pages, "https://example.com")
        issue_ids = [i.rule_id for i in issues]
        assert "crawl-4xx-pages" in issue_ids

    @pytest.mark.asyncio
    async def test_crawl_issue_5xx_critical(self):
        engine = CrawlerEngine()
        pages = [PageData(url="https://example.com/error", status_code=500)]
        issues = engine._analyze_crawl_issues(pages, "https://example.com")
        critical = [i for i in issues if i.rule_id == "crawl-5xx-pages"]
        assert critical
        assert critical[0].severity.value == "critical"

    @pytest.mark.asyncio
    async def test_score_decreases_with_errors(self):
        engine = CrawlerEngine()

        good_pages = [PageData(url=f"https://example.com/{i}", status_code=200) for i in range(10)]
        good_score = engine._calculate_crawl_score(good_pages, [])

        bad_pages = [PageData(url=f"https://example.com/{i}", status_code=404) for i in range(10)]
        from app.engines.base import Issue, Severity, IssueCategory
        bad_issue = Issue(
            rule_id="crawl-4xx-pages",
            title="4xx",
            description="",
            severity=Severity.HIGH,
            category=IssueCategory.CRAWLABILITY,
            affected_count=10,
            impact_score=80.0,
        )
        bad_score = engine._calculate_crawl_score(bad_pages, [bad_issue])

        assert good_score > bad_score
