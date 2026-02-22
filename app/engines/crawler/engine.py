"""
Crawler Engine - Production BFS web crawler with JS rendering support.

Architecture:
- BFS traversal with configurable depth limits
- Async concurrency control via semaphore
- Per-domain rate limiting using token bucket
- Playwright integration for JS-rendered pages
- robots.txt and sitemap parsing
- Canonical URL normalization and duplicate detection
- Real-time progress reporting via Redis pub/sub
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog
from bs4 import BeautifulSoup
from playwright.async_api import Browser, Page, async_playwright

from app.core.config import get_settings
from app.engines.base import (
    AuditEngine,
    AuditResult,
    EngineStatus,
    IssueCategory,
    Issue,
    PageData,
    SiteData,
    Severity,
)

logger = structlog.get_logger(__name__)
settings = get_settings()


# ─────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────

@dataclass
class CrawlURL:
    """URL in the crawl queue with metadata."""
    url: str
    depth: int
    parent_url: str | None = None
    discovered_via: str = "link"  # link | sitemap | manual


@dataclass
class CrawlStats:
    """Live crawl statistics."""
    total_queued: int = 0
    total_crawled: int = 0
    total_failed: int = 0
    total_skipped: int = 0
    js_rendered: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def pages_per_second(self) -> float:
        elapsed = self.elapsed_seconds
        return self.total_crawled / elapsed if elapsed > 0 else 0


@dataclass
class RateLimiter:
    """
    Token bucket rate limiter per domain.
    Allows burst up to max_tokens then enforces steady rate.
    """
    rate: float  # Tokens per second
    max_tokens: float
    tokens: float = field(init=False)
    last_refill: float = field(default_factory=time.time, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self):
        self.tokens = self.max_tokens

    async def acquire(self) -> None:
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            self.tokens = min(self.max_tokens, self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens < 1:
                wait_time = (1 - self.tokens) / self.rate
                await asyncio.sleep(wait_time)
                self.tokens = 0
            else:
                self.tokens -= 1


# ─────────────────────────────────────────────
# URL Utilities
# ─────────────────────────────────────────────

class URLNormalizer:
    """Normalizes URLs for deduplication and comparison."""

    IGNORED_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref", "fbclid", "gclid"}
    IGNORED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".css", ".js", ".woff", ".woff2", ".ttf", ".zip", ".tar", ".gz", ".mp4", ".mp3", ".wav"}

    @classmethod
    def normalize(cls, url: str, base_url: str) -> str | None:
        """
        Normalize a URL relative to base_url.
        Returns None if URL should be skipped.
        """
        try:
            # Resolve relative URLs
            url = urljoin(base_url, url.strip())
            parsed = urlparse(url)

            # Only HTTP/HTTPS
            if parsed.scheme not in ("http", "https"):
                return None

            # Skip ignored extensions
            path_lower = parsed.path.lower()
            if any(path_lower.endswith(ext) for ext in cls.IGNORED_EXTENSIONS):
                return None

            # Remove fragment
            # Strip ignored query params
            if parsed.query:
                from urllib.parse import parse_qs, urlencode
                params = parse_qs(parsed.query, keep_blank_values=True)
                filtered = {k: v for k, v in params.items() if k not in cls.IGNORED_PARAMS}
                query = urlencode(filtered, doseq=True)
            else:
                query = ""

            # Normalize trailing slash (remove for non-root paths)
            path = parsed.path
            if path != "/" and path.endswith("/"):
                path = path.rstrip("/")

            normalized = urlunparse((
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                path,
                parsed.params,
                query,
                "",  # No fragment
            ))
            return normalized

        except Exception:
            return None

    @classmethod
    def url_fingerprint(cls, url: str) -> str:
        """Create a short hash fingerprint for deduplication."""
        return hashlib.md5(url.encode()).hexdigest()

    @classmethod
    def is_same_domain(cls, url: str, root_domain: str) -> bool:
        """Check if URL belongs to the root domain (including subdomains)."""
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        return host == root_domain or host.endswith(f".{root_domain}")


# ─────────────────────────────────────────────
# Robots.txt Handler
# ─────────────────────────────────────────────

class RobotsHandler:
    """Parse and enforce robots.txt rules."""

    def __init__(self):
        self._parsers: dict[str, RobotFileParser] = {}

    async def fetch_and_parse(self, domain: str, session: httpx.AsyncClient) -> None:
        """Fetch robots.txt for a domain."""
        robots_url = f"https://{domain}/robots.txt"
        parser = RobotFileParser(robots_url)

        try:
            response = await session.get(robots_url, timeout=10)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            self._parsers[domain] = parser
        except Exception as e:
            logger.debug("Could not fetch robots.txt", domain=domain, error=str(e))
            self._parsers[domain] = RobotFileParser()

    def can_fetch(self, url: str, user_agent: str) -> bool:
        """Check if URL is allowed by robots.txt."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        parser = self._parsers.get(domain)
        if parser is None:
            return True  # No robots.txt = allow all
        return parser.can_fetch(user_agent, url)

    def get_crawl_delay(self, domain: str) -> float | None:
        """Get crawl-delay directive if specified."""
        parser = self._parsers.get(domain)
        if parser:
            delay = parser.crawl_delay(settings.CRAWLER_USER_AGENT)
            return float(delay) if delay else None
        return None


# ─────────────────────────────────────────────
# Sitemap Parser
# ─────────────────────────────────────────────

class SitemapParser:
    """Discover and parse XML sitemaps."""

    async def discover(self, root_url: str, session: httpx.AsyncClient) -> list[str]:
        """Discover sitemap URLs from robots.txt and common locations."""
        sitemap_urls: set[str] = set()

        # Check common sitemap locations
        parsed = urlparse(root_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        candidates = [
            f"{base}/sitemap.xml",
            f"{base}/sitemap_index.xml",
            f"{base}/sitemap/sitemap.xml",
        ]

        for url in candidates:
            urls = await self._fetch_sitemap(url, session)
            sitemap_urls.update(urls)

        return list(sitemap_urls)

    async def _fetch_sitemap(self, url: str, session: httpx.AsyncClient) -> list[str]:
        """Fetch and parse a single sitemap."""
        try:
            response = await session.get(url, timeout=15)
            if response.status_code != 200:
                return []

            content = response.text
            urls = []

            # Sitemap index
            if "<sitemapindex" in content:
                soup = BeautifulSoup(content, "xml")
                for loc in soup.find_all("loc"):
                    nested = await self._fetch_sitemap(loc.text.strip(), session)
                    urls.extend(nested)

            # URL sitemap
            elif "<urlset" in content:
                soup = BeautifulSoup(content, "xml")
                for loc in soup.find_all("loc"):
                    urls.append(loc.text.strip())

            return urls

        except Exception as e:
            logger.debug("Sitemap fetch failed", url=url, error=str(e))
            return []


# ─────────────────────────────────────────────
# Page Fetcher
# ─────────────────────────────────────────────

class PageFetcher:
    """
    Fetches individual pages via HTTP or Playwright (JS rendering).
    Decides rendering mode based on content-type and response analysis.
    """

    JS_INDICATORS = [
        "application/javascript",
        "__NEXT_DATA__",
        "window.__data",
        "ng-version",
        "data-reactroot",
        "Vue.createApp",
        "nuxt",
    ]

    def __init__(self, browser: Browser, http_session: httpx.AsyncClient):
        self.browser = browser
        self.http_session = http_session

    async def fetch(self, url: str, force_render: bool = False) -> PageData:
        """
        Fetch a page, auto-detecting if JS rendering is needed.
        """
        # First try plain HTTP
        http_result = await self._fetch_http(url)

        if force_render or self._needs_rendering(http_result):
            logger.debug("JS rendering required", url=url)
            return await self._fetch_rendered(url)

        return http_result

    async def _fetch_http(self, url: str) -> PageData:
        """Fetch via plain HTTP using httpx."""
        start = time.perf_counter()
        try:
            response = await self.http_session.get(
                url,
                follow_redirects=True,
                timeout=settings.CRAWLER_REQUEST_TIMEOUT,
            )
            elapsed = (time.perf_counter() - start) * 1000
            content = response.text

            page = PageData(
                url=str(response.url),
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
                html=content,
                headers=dict(response.headers),
                load_time_ms=elapsed,
                page_size_bytes=len(response.content),
            )

            if response.status_code == 200 and "text/html" in response.headers.get("content-type", ""):
                self._parse_html(page, content)

            return page

        except httpx.TimeoutException:
            return PageData(url=url, status_code=408)
        except httpx.TooManyRedirects:
            return PageData(url=url, status_code=310)
        except Exception as e:
            logger.warning("HTTP fetch failed", url=url, error=str(e))
            return PageData(url=url, status_code=0)

    async def _fetch_rendered(self, url: str) -> PageData:
        """Fetch via Playwright for JS-rendered content."""
        start = time.perf_counter()
        page: Page | None = None
        try:
            page = await self.browser.new_page()

            # Block unnecessary resources for speed
            await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2}", lambda r: r.abort())

            response = await page.goto(
                url,
                wait_until="networkidle",
                timeout=settings.CRAWLER_JS_RENDER_TIMEOUT,
            )

            # Wait for content to stabilize
            await page.wait_for_load_state("domcontentloaded")

            html = await page.content()
            elapsed = (time.perf_counter() - start) * 1000

            page_data = PageData(
                url=page.url,
                status_code=response.status if response else 200,
                content_type="text/html",
                html=html,
                load_time_ms=elapsed,
                page_size_bytes=len(html.encode()),
            )

            self._parse_html(page_data, html)
            return page_data

        except Exception as e:
            logger.warning("Playwright fetch failed", url=url, error=str(e))
            return PageData(url=url, status_code=0)
        finally:
            if page:
                await page.close()

    def _needs_rendering(self, page: PageData) -> bool:
        """Heuristically determine if page needs JS rendering."""
        if page.status_code == 0:
            return False  # Connection failed - rendering won't help

        html = page.html
        if not html:
            return False

        # Check for JS framework indicators
        for indicator in self.JS_INDICATORS:
            if indicator in html:
                return True

        # Very thin HTML with few elements is suspicious
        soup = BeautifulSoup(html, "lxml")
        paragraphs = soup.find_all("p")
        if len(html) > 1000 and len(paragraphs) == 0:
            return True

        return False

    def _parse_html(self, page: PageData, html: str) -> None:
        """Extract structured data from HTML."""
        try:
            soup = BeautifulSoup(html, "lxml")

            # Meta tags
            for tag in soup.find_all("meta"):
                name = tag.get("name") or tag.get("property") or ""
                content = tag.get("content") or ""
                if name and content:
                    page.meta[name.lower()] = content

            # Title
            title_tag = soup.find("title")
            if title_tag:
                page.meta["title"] = title_tag.get_text(strip=True)

            # Canonical
            canonical = soup.find("link", rel="canonical")
            if canonical and canonical.get("href"):
                page.canonical_url = canonical["href"]

            # Links
            page.links = []
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                    page.links.append(href)

            # Images
            page.images = []
            for img in soup.find_all("img"):
                page.images.append({
                    "src": img.get("src", ""),
                    "alt": img.get("alt", ""),
                    "width": img.get("width"),
                    "height": img.get("height"),
                    "loading": img.get("loading"),
                })

            # Structured data (JSON-LD)
            page.structured_data = []
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    import json
                    data = json.loads(script.string or "")
                    page.structured_data.append(data)
                except Exception:
                    pass

            # Text content
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            page.text_content = " ".join(soup.get_text(separator=" ").split())

        except Exception as e:
            logger.warning("HTML parse error", url=page.url, error=str(e))


# ─────────────────────────────────────────────
# Main Crawler
# ─────────────────────────────────────────────

class CrawlerEngine(AuditEngine):
    """
    Production BFS web crawler.

    Flow:
    1. Initialize: parse robots.txt, discover sitemaps
    2. Seed queue with root URL + sitemap URLs
    3. BFS: dequeue URL → check rules → fetch → parse links → enqueue
    4. Concurrency controlled via semaphore
    5. Rate limited per domain via token bucket
    6. Duplicate detection via URL fingerprint set
    7. Results aggregated into SiteData
    """

    ENGINE_NAME = "crawler"
    CATEGORY = IssueCategory.CRAWLABILITY

    def __init__(self):
        super().__init__()
        self.robots_handler = RobotsHandler()
        self.sitemap_parser = SitemapParser()
        self.url_normalizer = URLNormalizer()

    async def run(self, site_data: SiteData) -> AuditResult:
        """Execute the crawler and populate site_data.pages."""
        max_pages = site_data.settings.get("max_pages", settings.CRAWLER_MAX_PAGES_PER_AUDIT)
        max_depth = site_data.settings.get("max_depth", 10)
        concurrency = site_data.settings.get("concurrency", settings.CRAWLER_MAX_CONCURRENCY)
        rate_limit = site_data.settings.get("rate_limit_rps", settings.CRAWLER_RATE_LIMIT_RPS)
        js_render = site_data.settings.get("js_render", False)

        parsed_root = urlparse(site_data.root_url)
        domain = parsed_root.netloc.lower()
        stats = CrawlStats()

        issues: list[Issue] = []
        crawled_pages: list[PageData] = []

        # Token bucket for rate limiting
        rate_limiter = RateLimiter(rate=rate_limit, max_tokens=min(rate_limit * 3, 10))

        # BFS queue and visited set
        queue: deque[CrawlURL] = deque()
        visited: set[str] = set()      # Normalized URLs
        fingerprints: set[str] = set() # MD5 content fingerprints for duplicate detection

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )

            headers = {
                "User-Agent": settings.CRAWLER_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }

            async with httpx.AsyncClient(
                headers=headers,
                follow_redirects=True,
                verify=False,  # Enterprise sites sometimes have SSL issues
                limits=httpx.Limits(max_connections=concurrency + 5, max_keepalive_connections=20),
            ) as http_client:

                fetcher = PageFetcher(browser=browser, http_session=http_client)

                # Step 1: Fetch robots.txt
                await self.robots_handler.fetch_and_parse(domain, http_client)
                crawl_delay = self.robots_handler.get_crawl_delay(domain)
                if crawl_delay:
                    rate_limiter.rate = 1.0 / crawl_delay
                    self.logger.info("Respecting crawl-delay", delay=crawl_delay, domain=domain)

                # Step 2: Discover sitemaps
                sitemap_urls = await self.sitemap_parser.discover(site_data.root_url, http_client)
                site_data.sitemap_urls = sitemap_urls
                self.logger.info("Sitemap URLs discovered", count=len(sitemap_urls))

                # Step 3: Seed queue
                queue.append(CrawlURL(url=site_data.root_url, depth=0, discovered_via="manual"))

                # Add sitemap URLs to queue (at depth 0, high priority)
                for surl in sitemap_urls[:1000]:  # Cap initial sitemap seeds
                    normalized = self.url_normalizer.normalize(surl, site_data.root_url)
                    if normalized:
                        queue.append(CrawlURL(url=normalized, depth=1, discovered_via="sitemap"))

                stats.total_queued = len(queue)
                semaphore = asyncio.Semaphore(concurrency)

                # Step 4: BFS Crawl Loop
                async def crawl_url(crawl_item: CrawlURL) -> PageData | None:
                    async with semaphore:
                        normalized = self.url_normalizer.normalize(crawl_item.url, site_data.root_url)
                        if not normalized:
                            return None

                        # Domain check
                        if not self.url_normalizer.is_same_domain(normalized, domain):
                            return None

                        # Dedup check
                        if normalized in visited:
                            return None
                        visited.add(normalized)

                        # Depth check
                        if crawl_item.depth > max_depth:
                            stats.total_skipped += 1
                            return None

                        # Robots check
                        if not self.robots_handler.can_fetch(normalized, settings.CRAWLER_USER_AGENT):
                            stats.total_skipped += 1
                            self.logger.debug("Blocked by robots.txt", url=normalized)
                            return None

                        # Rate limiting
                        await rate_limiter.acquire()

                        # Fetch
                        page = await fetcher.fetch(normalized, force_render=js_render)
                        page.depth = crawl_item.depth
                        stats.total_crawled += 1

                        if stats.total_crawled % 100 == 0:
                            self.logger.info(
                                "Crawl progress",
                                crawled=stats.total_crawled,
                                queued=len(queue),
                                pps=round(stats.pages_per_second, 2),
                            )

                        # Duplicate content detection via fingerprint
                        if page.html:
                            fp = hashlib.md5(page.html.encode()).hexdigest()
                            if fp in fingerprints:
                                page.meta["is_duplicate_content"] = "true"
                            fingerprints.add(fp)

                        # Enqueue discovered links
                        if crawl_item.depth < max_depth:
                            for link in page.links:
                                link_normalized = self.url_normalizer.normalize(link, normalized)
                                if (
                                    link_normalized
                                    and link_normalized not in visited
                                    and self.url_normalizer.is_same_domain(link_normalized, domain)
                                    and len(queue) + len(visited) < max_pages * 2
                                ):
                                    queue.append(CrawlURL(
                                        url=link_normalized,
                                        depth=crawl_item.depth + 1,
                                        parent_url=normalized,
                                        discovered_via="link",
                                    ))
                                    stats.total_queued += 1

                        return page

                # BFS with concurrent batching
                while queue and len(crawled_pages) < max_pages:
                    # Take a batch from the queue
                    batch_size = min(concurrency * 2, len(queue), max_pages - len(crawled_pages))
                    batch = [queue.popleft() for _ in range(batch_size)]

                    results = await asyncio.gather(*[crawl_url(item) for item in batch], return_exceptions=True)

                    for result in results:
                        if isinstance(result, PageData):
                            crawled_pages.append(result)
                        elif isinstance(result, Exception):
                            stats.total_failed += 1
                            self.logger.warning("Crawl task failed", error=str(result))

            await browser.close()

        # Update site_data with crawled pages
        site_data.pages = crawled_pages
        site_data.crawl_stats = {
            "total_crawled": stats.total_crawled,
            "total_failed": stats.total_failed,
            "total_skipped": stats.total_skipped,
            "elapsed_seconds": round(stats.elapsed_seconds, 2),
            "pages_per_second": round(stats.pages_per_second, 2),
            "sitemap_urls_found": len(sitemap_urls),
        }

        # Analyze crawl results for issues
        issues = self._analyze_crawl_issues(crawled_pages, site_data.root_url)
        score = self._calculate_crawl_score(crawled_pages, issues)

        return AuditResult(
            engine_name=self.ENGINE_NAME,
            audit_id=site_data.audit_id,
            status=EngineStatus.SUCCESS,
            category=self.CATEGORY,
            score=score,
            grade=self.calculate_grade(score),
            issues=issues,
            pages_analyzed=len(crawled_pages),
            metadata=site_data.crawl_stats,
        )

    def _analyze_crawl_issues(self, pages: list[PageData], root_url: str) -> list[Issue]:
        """Analyze crawled pages for crawlability and indexation issues."""
        issues: list[Issue] = []

        # 4xx pages
        error_4xx = [p for p in pages if 400 <= p.status_code < 500]
        if error_4xx:
            issues.append(Issue(
                rule_id="crawl-4xx-pages",
                title="Pages returning 4xx errors",
                description=f"{len(error_4xx)} pages return client error status codes.",
                severity=Severity.HIGH,
                category=IssueCategory.CRAWLABILITY,
                affected_urls=[p.url for p in error_4xx[:50]],
                affected_count=len(error_4xx),
                impact_score=min(100, len(error_4xx) * 2.0),
                recommendation="Fix or redirect broken URLs. Use 301 redirects for permanently moved content.",
            ))

        # 5xx pages
        error_5xx = [p for p in pages if p.status_code >= 500]
        if error_5xx:
            issues.append(Issue(
                rule_id="crawl-5xx-pages",
                title="Pages returning 5xx server errors",
                description=f"{len(error_5xx)} pages return server error status codes.",
                severity=Severity.CRITICAL,
                category=IssueCategory.CRAWLABILITY,
                affected_urls=[p.url for p in error_5xx[:50]],
                affected_count=len(error_5xx),
                impact_score=min(100, len(error_5xx) * 3.0),
                recommendation="Investigate server errors immediately. These pages are unindexable.",
            ))

        # Duplicate content
        duplicates = [p for p in pages if p.meta.get("is_duplicate_content") == "true"]
        if duplicates:
            issues.append(Issue(
                rule_id="crawl-duplicate-content",
                title="Duplicate content detected",
                description=f"{len(duplicates)} pages have identical or near-identical content.",
                severity=Severity.MEDIUM,
                category=IssueCategory.CRAWLABILITY,
                affected_urls=[p.url for p in duplicates[:50]],
                affected_count=len(duplicates),
                impact_score=min(80, len(duplicates) * 1.5),
                recommendation="Implement canonical tags or 301 redirects to consolidate duplicate content.",
            ))

        # Missing canonical
        no_canonical = [p for p in pages if p.status_code == 200 and not p.canonical_url]
        if no_canonical:
            issues.append(Issue(
                rule_id="crawl-missing-canonical",
                title="Pages without canonical tags",
                description=f"{len(no_canonical)} pages are missing canonical link elements.",
                severity=Severity.MEDIUM,
                category=IssueCategory.CRAWLABILITY,
                affected_urls=[p.url for p in no_canonical[:50]],
                affected_count=len(no_canonical),
                impact_score=min(60, len(no_canonical) * 0.5),
                recommendation="Add self-referencing canonical tags to all indexable pages.",
            ))

        # Non-canonical pages being crawled
        canonical_mismatch = [
            p for p in pages
            if p.canonical_url and p.canonical_url != p.url
            and p.status_code == 200
        ]
        if canonical_mismatch:
            issues.append(Issue(
                rule_id="crawl-canonical-mismatch",
                title="Crawled URLs differ from canonical",
                description=f"{len(canonical_mismatch)} crawled URLs point to a different canonical URL, wasting crawl budget.",
                severity=Severity.MEDIUM,
                category=IssueCategory.CRAWLABILITY,
                affected_urls=[p.url for p in canonical_mismatch[:50]],
                affected_count=len(canonical_mismatch),
                impact_score=min(70, len(canonical_mismatch) * 1.0),
                recommendation="Ensure internal links point to the canonical version of each URL.",
            ))

        # Very slow pages
        slow_pages = [p for p in pages if p.load_time_ms > 5000 and p.status_code == 200]
        if slow_pages:
            issues.append(Issue(
                rule_id="crawl-slow-pages",
                title="Pages with slow server response time",
                description=f"{len(slow_pages)} pages took over 5 seconds to respond.",
                severity=Severity.HIGH,
                category=IssueCategory.CRAWLABILITY,
                affected_urls=[p.url for p in slow_pages[:50]],
                affected_count=len(slow_pages),
                impact_score=min(80, len(slow_pages) * 2.0),
                recommendation="Optimize server response time. Target < 200ms TTFB.",
            ))

        return issues

    def _calculate_crawl_score(self, pages: list[PageData], issues: list[Issue]) -> float:
        """Calculate overall crawlability score."""
        if not pages:
            return 0.0

        total = len(pages)
        successful = len([p for p in pages if 200 <= p.status_code < 400])
        success_rate = successful / total

        # Base score from success rate
        score = success_rate * 70

        # Bonus for having sitemaps, canonicals, etc.
        has_canonical = len([p for p in pages if p.canonical_url]) / max(1, total)
        score += has_canonical * 20

        # Penalty per issue by severity
        severity_penalties = {
            Severity.CRITICAL: 20,
            Severity.HIGH: 10,
            Severity.MEDIUM: 5,
            Severity.LOW: 2,
        }
        for issue in issues:
            score -= severity_penalties.get(issue.severity, 0)

        return max(0.0, min(100.0, round(score, 2)))
