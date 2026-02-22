"""
Audit Tasks - Celery task definitions for orchestrating full SEO audits.

Flow:
1. run_full_audit()        → Creates audit record, dispatches crawl
2. run_crawl_task()        → Crawls site, stores page data, dispatches engines
3. run_engine_task()       → Runs individual engine, stores result
4. finalize_audit_task()   → Aggregates scores, generates recommendations, marks complete

Error handling:
- Each task retries up to CELERY_MAX_RETRIES times
- Partial results are preserved if any engine fails
- Audit status reflects actual completion state
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import structlog
from celery import chain, chord, group
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.engines.base import AuditResult, EngineStatus, SiteData
from app.engines.crawler.engine import CrawlerEngine
from app.engines.onpage.engine import OnPageAnalyzerEngine
from app.engines.prioritization.engine import PrioritizationEngine
from app.engines.scoring.engine import ScoringEngine
from app.engines.technical.engine import TechnicalSEOEngine
from app.models.models import Audit, AuditIssue, AuditRecommendation, CategoryScoreRecord, EngineResult, Page, Site
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)
settings = get_settings()


def run_async(coro):
    """Run an async coroutine in a Celery (sync) task context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ─────────────────────────────────────────────
# Engine Registry
# ─────────────────────────────────────────────

ENGINE_REGISTRY = {
    "crawler": CrawlerEngine,
    "technical": TechnicalSEOEngine,
    "onpage": OnPageAnalyzerEngine,
    # Additional engines registered here as built:
    # "performance": PerformanceEngine,
    # "content_ai": ContentAIEngine,
    # "internal_links": InternalLinksEngine,
    # "schema": SchemaValidatorEngine,
}

ANALYSIS_ENGINES = ["technical", "onpage"]  # Engines that run after crawl


# ─────────────────────────────────────────────
# Task: Run Full Audit
# ─────────────────────────────────────────────

@celery_app.task(
    name="app.workers.audit_tasks.run_full_audit",
    bind=True,
    max_retries=1,
    queue="analysis_queue",
)
def run_full_audit(self, audit_id: str, site_id: str, config: dict) -> dict:
    """
    Orchestrate a complete SEO audit.
    Creates and dispatches the full engine chain.
    """
    logger.info("Starting full audit", audit_id=audit_id, site_id=site_id)

    try:
        # Update audit status to crawling
        run_async(_update_audit_status(audit_id, "crawling", started_at=datetime.now(timezone.utc)))

        # Step 1: Crawl → Step 2: Analyze → Step 3: Score → Step 4: Finalize
        workflow = chain(
            run_crawl_task.s(audit_id, site_id, config),
            run_analysis_engines.s(audit_id, site_id),
            finalize_audit_task.s(audit_id),
        )
        workflow.apply_async()

        return {"status": "dispatched", "audit_id": audit_id}

    except Exception as exc:
        logger.error("Failed to start audit", audit_id=audit_id, error=str(exc))
        run_async(_update_audit_status(audit_id, "failed", error_message=str(exc)))
        raise self.retry(exc=exc, countdown=30)


# ─────────────────────────────────────────────
# Task: Crawl
# ─────────────────────────────────────────────

@celery_app.task(
    name="app.workers.crawl_tasks.run_crawl_task",
    bind=True,
    queue="crawl_queue",
    soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT,
    time_limit=settings.CELERY_TASK_TIME_LIMIT,
    max_retries=settings.CELERY_MAX_RETRIES,
    acks_late=True,
)
def run_crawl_task(self, _prev: Any, audit_id: str, site_id: str, config: dict) -> dict:
    """Execute the crawler and persist crawled pages."""
    logger.info("Starting crawl task", audit_id=audit_id)

    try:
        site_data = run_async(_build_site_data(audit_id, site_id, config))

        engine = CrawlerEngine()
        result = run_async(engine.execute(site_data))

        # Persist pages to DB
        run_async(_persist_pages(audit_id, site_id, site_data.pages))

        # Update audit
        run_async(_update_audit_status(
            audit_id,
            "analyzing",
            pages_crawled=len(site_data.pages),
        ))

        # Serialize site_data for next task (without full HTML to save memory)
        serialized = _serialize_site_data(site_data)
        logger.info("Crawl complete", audit_id=audit_id, pages=len(site_data.pages))

        return {"site_data": serialized, "crawl_result": result.model_dump()}

    except SoftTimeLimitExceeded:
        logger.error("Crawl task timed out", audit_id=audit_id)
        run_async(_update_audit_status(audit_id, "failed", error_message="Crawl timed out"))
        raise

    except Exception as exc:
        logger.error("Crawl task failed", audit_id=audit_id, error=str(exc), exc_info=True)
        countdown = settings.CELERY_RETRY_BACKOFF * (self.request.retries + 1)
        raise self.retry(exc=exc, countdown=countdown)


# ─────────────────────────────────────────────
# Task: Run Analysis Engines
# ─────────────────────────────────────────────

@celery_app.task(
    name="app.workers.audit_tasks.run_analysis_engines",
    bind=True,
    queue="analysis_queue",
)
def run_analysis_engines(self, prev_result: dict, audit_id: str, site_id: str) -> dict:
    """
    Fan-out: run all analysis engines in parallel using Celery chord.
    Waits for all engines to complete before proceeding.
    """
    logger.info("Dispatching analysis engines", audit_id=audit_id, engines=ANALYSIS_ENGINES)

    site_data_dict = prev_result.get("site_data", {})
    crawl_result = prev_result.get("crawl_result", {})

    # Create a group of engine tasks (run in parallel)
    engine_tasks = group([
        run_engine_task.s(engine_name, audit_id, site_id, site_data_dict)
        for engine_name in ANALYSIS_ENGINES
    ])

    # chord: run group, then aggregate results
    result = chord(engine_tasks)(aggregate_engine_results.s(audit_id, crawl_result))

    # Wait synchronously for chord completion (Celery chain requirement)
    engine_results = result.get(timeout=settings.CELERY_TASK_SOFT_TIME_LIMIT)

    return {
        "engine_results": engine_results,
        "crawl_result": crawl_result,
        "site_data": site_data_dict,
    }


# ─────────────────────────────────────────────
# Task: Single Engine
# ─────────────────────────────────────────────

@celery_app.task(
    name="app.workers.audit_tasks.run_engine_task",
    bind=True,
    queue="analysis_queue",
    soft_time_limit=1800,
    time_limit=2400,
    max_retries=2,
)
def run_engine_task(self, engine_name: str, audit_id: str, site_id: str, site_data_dict: dict) -> dict:
    """Run a single audit engine and persist its results."""
    logger.info("Running engine", engine=engine_name, audit_id=audit_id)

    try:
        engine_class = ENGINE_REGISTRY.get(engine_name)
        if not engine_class:
            raise ValueError(f"Unknown engine: {engine_name}")

        site_data = _deserialize_site_data(site_data_dict)
        engine = engine_class()
        result = run_async(engine.execute(site_data))

        # Persist engine result and issues
        run_async(_persist_engine_result(audit_id, result))

        logger.info(
            "Engine complete",
            engine=engine_name,
            audit_id=audit_id,
            score=result.score,
            issues=len(result.issues),
        )

        return result.model_dump()

    except SoftTimeLimitExceeded:
        logger.error("Engine timed out", engine=engine_name, audit_id=audit_id)
        return AuditResult(
            engine_name=engine_name,
            audit_id=uuid.UUID(audit_id),
            status=EngineStatus.FAILED,
            category="technical",
            error_message="Engine execution timed out",
        ).model_dump()

    except Exception as exc:
        logger.error("Engine failed", engine=engine_name, audit_id=audit_id, error=str(exc))
        raise self.retry(exc=exc, countdown=30)


# ─────────────────────────────────────────────
# Task: Aggregate Results
# ─────────────────────────────────────────────

@celery_app.task(
    name="app.workers.audit_tasks.aggregate_engine_results",
    queue="report_queue",
)
def aggregate_engine_results(engine_results_list: list[dict], audit_id: str, crawl_result: dict) -> list[dict]:
    """Collect all engine results (chord callback)."""
    logger.info("Aggregating engine results", audit_id=audit_id, count=len(engine_results_list))
    return engine_results_list


# ─────────────────────────────────────────────
# Task: Finalize Audit
# ─────────────────────────────────────────────

@celery_app.task(
    name="app.workers.report_tasks.finalize_audit_task",
    queue="report_queue",
    soft_time_limit=300,
)
def finalize_audit_task(prev_result: dict, audit_id: str) -> dict:
    """
    Compute final scores, generate recommendations, mark audit complete.
    """
    logger.info("Finalizing audit", audit_id=audit_id)

    engine_results_raw = prev_result.get("engine_results", [])
    site_data_dict = prev_result.get("site_data", {})

    try:
        # Reconstruct AuditResult objects
        engine_results = [AuditResult.model_validate(r) for r in engine_results_raw if r]

        site_data = _deserialize_site_data(site_data_dict)
        site_data.settings["engine_results"] = engine_results

        # Run scoring engine
        scoring_engine = ScoringEngine()
        scoring_result = run_async(scoring_engine.execute(site_data))

        # Run prioritization engine
        prioritization_engine = PrioritizationEngine()
        site_data.settings["engine_results"] = engine_results
        priority_result = run_async(prioritization_engine.execute(site_data))

        # Persist final results
        run_async(_persist_final_results(audit_id, scoring_result, priority_result))

        # Update audit as complete
        run_async(_update_audit_status(
            audit_id,
            "complete",
            overall_score=scoring_result.score,
            overall_grade=scoring_result.grade,
            confidence_score=scoring_result.metadata.get("confidence_score"),
            estimated_revenue_impact=scoring_result.metadata.get("estimated_monthly_revenue_impact"),
            issues_found=len(priority_result.issues),
            critical_issues=scoring_result.metadata.get("issue_summary", {}).get("critical", 0),
            completed_at=datetime.now(timezone.utc),
        ))

        logger.info(
            "Audit complete",
            audit_id=audit_id,
            score=scoring_result.score,
            grade=scoring_result.grade,
            issues=len(priority_result.issues),
        )

        return {"status": "complete", "score": scoring_result.score, "grade": scoring_result.grade}

    except Exception as exc:
        logger.error("Finalization failed", audit_id=audit_id, error=str(exc), exc_info=True)
        run_async(_update_audit_status(audit_id, "failed", error_message=str(exc)))
        raise


# ─────────────────────────────────────────────
# DB Helpers (async)
# ─────────────────────────────────────────────

async def _build_site_data(audit_id: str, site_id: str, config: dict) -> SiteData:
    async with AsyncSessionLocal() as session:
        site = await session.get(Site, uuid.UUID(site_id))
        if not site:
            raise ValueError(f"Site {site_id} not found")

        return SiteData(
            audit_id=uuid.UUID(audit_id),
            site_id=uuid.UUID(site_id),
            domain=site.domain,
            root_url=site.root_url,
            settings={**site.settings, **config},
        )


async def _update_audit_status(audit_id: str, status: str, **kwargs) -> None:
    async with AsyncSessionLocal() as session:
        values = {"status": status, **kwargs}
        await session.execute(
            update(Audit).where(Audit.id == uuid.UUID(audit_id)).values(**values)
        )
        await session.commit()


async def _persist_pages(audit_id: str, site_id: str, pages) -> None:
    async with AsyncSessionLocal() as session:
        for page_data in pages:
            page = Page(
                audit_id=uuid.UUID(audit_id),
                site_id=uuid.UUID(site_id),
                url=page_data.url,
                canonical_url=page_data.canonical_url,
                status_code=page_data.status_code,
                content_type=page_data.content_type,
                depth=page_data.depth,
                title=page_data.meta.get("title"),
                meta_description=page_data.meta.get("description"),
                word_count=int(page_data.meta.get("word_count", 0)),
                load_time_ms=page_data.load_time_ms,
                page_size_bytes=page_data.page_size_bytes,
                has_canonical=bool(page_data.canonical_url),
                meta_tags=page_data.meta,
                structured_data=page_data.structured_data,
                headers=page_data.headers,
            )
            session.add(page)

        await session.commit()
        logger.info("Pages persisted", count=len(pages))


async def _persist_engine_result(audit_id: str, result: AuditResult) -> None:
    async with AsyncSessionLocal() as session:
        engine_rec = EngineResult(
            audit_id=uuid.UUID(audit_id),
            engine_name=result.engine_name,
            category=result.category,
            status=result.status,
            score=result.score,
            grade=result.grade,
            execution_time_ms=result.execution_time_ms,
            pages_analyzed=result.pages_analyzed,
            issues_count=len(result.issues),
            metadata=result.metadata,
            error_message=result.error_message,
        )
        session.add(engine_rec)

        for issue in result.issues:
            issue_rec = AuditIssue(
                audit_id=uuid.UUID(audit_id),
                rule_id=issue.rule_id,
                title=issue.title,
                description=issue.description,
                category=issue.category,
                severity=issue.severity,
                impact_score=issue.impact_score,
                effort_score=issue.effort_score,
                affected_count=issue.affected_count,
                affected_urls=issue.affected_urls,
                recommendation=issue.recommendation,
                documentation_url=issue.documentation_url or "",
                metadata=issue.metadata,
            )
            session.add(issue_rec)

        await session.commit()


async def _persist_final_results(audit_id: str, scoring: AuditResult, priorities: AuditResult) -> None:
    async with AsyncSessionLocal() as session:
        # Persist recommendations
        for rec in priorities.recommendations:
            rec_rec = AuditRecommendation(
                audit_id=uuid.UUID(audit_id),
                issue_id=rec.issue_id,
                priority_rank=rec.priority_rank,
                title=rec.title,
                description=rec.description,
                effort=rec.effort,
                impact=rec.impact,
                estimated_traffic_gain=rec.estimated_traffic_gain,
                estimated_revenue_impact=rec.estimated_revenue_impact,
                implementation_steps=rec.implementation_steps,
            )
            session.add(rec_rec)

        # Persist category scores
        for cat_score in scoring.metadata.get("category_scores", []):
            score_rec = CategoryScoreRecord(
                audit_id=uuid.UUID(audit_id),
                site_id=scoring.audit_id,
                category=cat_score["category"],
                score=cat_score["score"],
                grade=cat_score["grade"],
                weight=cat_score["weight"],
                issues_count=cat_score["issues_count"],
                critical_count=cat_score["critical_count"],
            )
            session.add(score_rec)

        await session.commit()


def _serialize_site_data(site_data: SiteData) -> dict:
    """Serialize site data for Celery task passing (strip heavy HTML)."""
    data = site_data.model_dump()
    # Strip HTML content to reduce message size
    for page in data.get("pages", []):
        page["html"] = ""  # Don't pass HTML between tasks
    return data


def _deserialize_site_data(data: dict) -> SiteData:
    """Reconstruct SiteData from serialized dict."""
    return SiteData.model_validate(data)
