# SEO Audit Platform — Complete System Architecture

## 1. System Overview

Enterprise-grade modular SEO audit platform capable of:
- Auditing **10,000 websites/day**
- Crawling **50,000 pages per audit**
- Supporting **concurrent multi-tenant audits**
- Horizontally scaling across all components

**Core design principle:** Each audit engine is an independently deployable, stateless module. Engines communicate exclusively through structured data contracts — never direct imports of each other.

---

## 2. Architecture Decision: Microservices vs Monolith

**Decision: Modular Monolith → Microservices migration path**

The codebase is organized as a monolith with strict module boundaries. Each engine is isolated enough to be extracted to its own service when throughput demands it. This avoids distributed systems complexity in v1 while preserving the extraction path.

```
┌─────────────────────────────────────────────────────────────────┐
│                       API Gateway (Nginx)                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                    FastAPI Application                           │
│  /api/v1/audits   /api/v1/pages   /api/v1/reports              │
└────────────────────────────┬────────────────────────────────────┘
                             │
        ┌────────────────────▼───────────────────────┐
        │            Redis Message Broker             │
        │  crawl_queue | analysis_queue | report_queue│
        └──────┬─────────────┬───────────────┬───────┘
               │             │               │
    ┌──────────▼──┐  ┌───────▼──────┐  ┌───▼────────┐
    │ Crawl Worker│  │Analysis Worker│  │Report Worker│
    │   (2-5)     │  │    (5-20)    │  │   (2-5)    │
    └──────┬──────┘  └──────┬───────┘  └─────┬──────┘
           │                │                 │
    ┌──────▼────────────────▼─────────────────▼──────┐
    │               PostgreSQL (Primary)               │
    │          Read Replica (for reports)              │
    └─────────────────────────────────────────────────┘
```

---

## 3. Audit Engine Pipeline

```
START AUDIT
    │
    ▼
[1] CrawlerEngine          ← BFS crawl, Playwright rendering
    │  Outputs: SiteData (pages, crawl_stats)
    │
    ▼
[2] Fan-out (Celery chord) ← All engines run in parallel
    ├── TechnicalSEOEngine
    ├── OnPageAnalyzerEngine
    ├── PerformanceEngine
    ├── ContentAIEngine
    ├── InternalLinksEngine
    ├── SchemaValidatorEngine
    └── AuthorityEngine
    │  Each outputs: AuditResult (issues, score)
    │
    ▼
[3] ScoringEngine          ← Weighted aggregation → overall score
    │
    ▼
[4] PrioritizationEngine   ← Ranks fixes by ROI formula
    │
    ▼
[5] Audit COMPLETE
    │  DB: all results persisted
    │  Cache: summary cached in Redis
    └─ Frontend polls /api/v1/audits/{id} → receives results
```

---

## 4. Crawler Design

### BFS Algorithm

```
FUNCTION crawl(root_url, config):
  queue  = deque([CrawlURL(root_url, depth=0)])
  visited = set()
  pages   = []

  WHILE queue AND len(pages) < max_pages:
    batch = queue.popleft(batch_size)
    
    FOR crawl_url IN batch:
      IF crawl_url IN visited: CONTINUE
      IF depth > max_depth:   CONTINUE
      IF robots.disallows():  CONTINUE
      
      visited.add(crawl_url)
      await rate_limiter.acquire()
      
      page = await fetcher.fetch(crawl_url)
      pages.append(page)
      
      FOR link IN page.links:
        IF same_domain AND not_visited:
          queue.append(link, depth+1)
  
  RETURN pages
```

### Rate Limiting: Token Bucket

```
Tokens = min(max_tokens, tokens + elapsed × rate)
IF tokens < 1: sleep((1 - tokens) / rate)
ELSE:          tokens -= 1
```

### JS Rendering Decision Logic

```
fetch_http(url) → check for JS framework signatures:
  - __NEXT_DATA__, data-reactroot, ng-version, Vue.createApp
  - HTML > 1KB but zero <p> tags

IF any indicator: re-fetch with Playwright
```

---

## 5. Rule Engine

Rules are defined in JSON. No code changes needed to add new checks.

### Rule Structure

```json
{
  "id": "onpage-missing-title",
  "name": "Missing Title Tag",
  "category": "on_page",
  "severity": "critical",
  "conditions": [
    { "field": "meta.title", "operator": "not_exists", "value": null }
  ],
  "condition_logic": "AND",
  "impact_score": 95.0,
  "effort_score": 2.0,
  "recommendation": "..."
}
```

### Supported Operators

| Operator | Description |
|----------|-------------|
| `eq` / `ne` | Exact match |
| `lt` / `gt` / `lte` / `gte` | Numeric comparison |
| `contains` / `not_contains` | Substring check |
| `matches` / `not_matches` | Regex check |
| `exists` / `not_exists` | Field presence |
| `length_lt` / `length_gt` | String/array length |
| `starts_with` / `ends_with` | Prefix/suffix |

### Transforms

`len` · `lower` · `upper` · `strip` · `count` · `int` · `float` · `bool`

---

## 6. Scoring Model

### Category Weights

| Category | Weight | Rationale |
|----------|--------|-----------|
| Technical | 20% | Foundation — technical debt kills everything else |
| On-Page | 15% | Direct ranking signals |
| Content | 15% | High effort but high impact |
| Performance | 15% | Core Web Vitals are ranking factors |
| Crawlability | 15% | Unindexable = invisible |
| Internal Links | 10% | PageRank distribution |
| Schema | 5% | Rich results opportunity |
| Authority | 5% | External signal (limited control) |

### Score Calculation

```
Overall = Σ(engine_score × category_weight) / Σ(category_weights of successful engines)

Category Score:
  penalty = Σ(severity_weight × coverage_ratio) per issue
  score   = max(0, 100 - (penalty / max_penalty) × 100)
```

### Grading Scale

| Score | Grade |
|-------|-------|
| 90–100 | A |
| 80–89 | B |
| 65–79 | C |
| 50–64 | D |
| 0–49 | F |

---

## 7. Prioritization Formula

```
Priority = (Impact × 0.40)
         + (Traffic_Potential × 0.25)
         + (Effort_Ease × 0.20)     ← (10 - effort_score) × 10
         + (Severity_Weight × 0.15)
```

| Factor | Rationale |
|--------|-----------|
| Impact (40%) | How much does fixing this improve SEO? |
| Traffic Potential (25%) | How much traffic will this unlock? |
| Effort Ease (20%) | Favor quick wins over multi-sprint projects |
| Severity (15%) | Critical issues need urgency bias |

---

## 8. Database Schema

```sql
organizations (id, name, plan, settings)
    │
    └─ sites (id, organization_id, domain, root_url, last_score)
           │
           └─ audits (id, site_id, status, overall_score, pages_crawled, issues_found)
                  │
                  ├─ pages (id, audit_id, url, status_code, title, word_count, load_time_ms)
                  ├─ engine_results (id, audit_id, engine_name, score, grade)
                  ├─ audit_issues (id, audit_id, rule_id, severity, impact_score, affected_urls)
                  ├─ audit_recommendations (id, audit_id, priority_rank, effort, impact)
                  └─ category_scores (id, audit_id, category, score, grade)
```

**Key Indexes:**
- `audits.site_id` — frequent site→audits lookups
- `audit_issues.severity` — filter by severity in reports
- `audit_issues.audit_id` — all issues for an audit
- `category_scores (site_id, category)` — trend analysis queries

---

## 9. API Reference

### Start Audit
```
POST /api/v1/audits
Body: { "site_url": "https://example.com", "max_pages": 5000 }
Returns: 202 { "id": "<uuid>", "status": "pending" }
```

### Poll Status
```
GET /api/v1/audits/{id}
Returns: { "status": "complete", "overall_score": 74.3, "overall_grade": "C" }
```

### Fetch Issues
```
GET /api/v1/audits/{id}/issues?severity=critical&page=1&per_page=50
Returns: PaginatedResponse<Issue>
```

### Fetch Recommendations
```
GET /api/v1/audits/{id}/recommendations
Returns: Recommendation[] sorted by priority_rank
```

### Score Breakdown
```
GET /api/v1/audits/{id}/scores
Returns: { overall_score, categories: [{ category, score, grade, weight }] }
```

---

## 10. AI Usage Strategy

| Use AI For | Use Rules For |
|------------|---------------|
| Content quality assessment (thin, duplicate, topical gaps) | Title/meta length checks |
| Competitive gap analysis | Status code validation |
| Natural language recommendations | Redirect chain detection |
| Schema markup generation suggestions | robots.txt parsing |
| Anchor text quality scoring | Structured data validation |
| Revenue impact narrative generation | URL structure checks |

**Reasoning:** Rules are deterministic, auditable, and free. AI adds value specifically where qualitative judgment is needed. AI calls cost money and introduce latency — use them surgically.

---

## 11. Scalability Design

### Target: 10,000 audits/day × 50,000 pages

```
Peak throughput = 10,000 × 50,000 / 86,400 = ~5,787 pages/second

With CRAWLER_MAX_CONCURRENCY=20 and CRAWLER_RATE_LIMIT_RPS=5:
  Per crawl worker: 5 pages/second sustained
  Required crawl workers: 5787 / 5 = ~1,160 concurrent crawls
  With 10 pages/sec burst: ~580 workers needed at peak

Horizontal scaling:
  - Crawl workers: auto-scale 10–500 based on crawl_queue depth
  - Analysis workers: auto-scale 5–200 based on analysis_queue depth
  - API servers: auto-scale 2–20 based on CPU/request latency
```

### Infrastructure Stack

```
Load Balancer (AWS ALB)
    │
    ├── API Pods (ECS Fargate / EKS) — auto-scaled
    │
    ├── Celery Workers (ECS Fargate) — queue-depth scaled
    │   ├── Crawl Workers (high memory: 4GB RAM each)
    │   ├── Analysis Workers (moderate: 2GB RAM each)
    │   └── Report Workers (light: 1GB RAM each)
    │
    ├── RDS PostgreSQL (Multi-AZ, r6g.2xlarge)
    │   └── Read Replica for report queries
    │
    ├── ElastiCache Redis (cluster mode, 6 nodes)
    │   ├── DB 0: Application cache
    │   ├── DB 1: Celery broker
    │   └── DB 2: Celery results
    │
    └── S3 (crawl artifact storage, HTML snapshots)
```

---

## 12. Build Roadmap

### Phase 1 — Core Infrastructure (Weeks 1–3)
- [x] FastAPI application skeleton
- [x] PostgreSQL models + Alembic migrations
- [x] Redis + Celery setup
- [x] Base engine interface
- [x] Rule engine
- [x] Crawler engine
- [x] Docker Compose dev environment

### Phase 2 — Audit Engines (Weeks 4–8)
- [ ] Technical SEO engine (complete)
- [ ] On-page analyzer (complete)
- [ ] Performance + Core Web Vitals (PageSpeed API integration)
- [ ] Internal links graph analyzer
- [ ] Schema.org validator
- [ ] Content AI engine (OpenAI integration)

### Phase 3 — Intelligence Layer (Weeks 9–11)
- [ ] Scoring engine (complete)
- [ ] Prioritization engine (complete)
- [ ] Revenue impact modeling
- [ ] Competitor intelligence engine
- [ ] Historical trend tracking

### Phase 4 — Production Hardening (Weeks 12–14)
- [ ] Authentication + multi-tenancy
- [ ] Rate limiting per API key
- [ ] Prometheus + Grafana dashboards
- [ ] Sentry error tracking
- [ ] Load testing (Locust)
- [ ] Kubernetes Helm charts
- [ ] CI/CD pipeline (GitHub Actions)

### Phase 5 — Scale (Weeks 15+)
- [ ] Auto-scaling policies (KEDA queue-based)
- [ ] Read replica for heavy report queries
- [ ] S3 crawl artifact storage
- [ ] CDN for report delivery
- [ ] White-label API support
