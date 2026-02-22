"""
Audit API Routes

No business logic lives here.
Routes validate input, call services, return responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, HttpUrl, field_validator
from sqlalchemy import select, func

from app.core.database import DBSession
from app.models.models import Audit, Organization, Site
from app.workers.audit_tasks import run_full_audit

logger = structlog.get_logger(__name__)
router = APIRouter()

DEFAULT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ─────────────────────────────────────────────
# Request / Response Schemas
# ─────────────────────────────────────────────

class CreateAuditRequest(BaseModel):
    site_url: HttpUrl
    max_pages: int = 5000
    max_depth: int = 10
    js_render: bool = False
    rate_limit_rps: float = 5.0
    monthly_traffic: int = 10_000

    @field_validator("max_pages")
    @classmethod
    def validate_max_pages(cls, v: int) -> int:
        if v < 1 or v > 50_000:
            raise ValueError("max_pages must be between 1 and 50,000")
        return v


class AuditResponse(BaseModel):
    id: UUID
    site_id: UUID
    status: str
    created_at: datetime
    message: str = ""


class AuditDetailResponse(BaseModel):
    id: UUID
    site_id: UUID
    status: str
    overall_score: float | None
    overall_grade: str | None
    confidence_score: float | None
    estimated_revenue_impact: float | None
    pages_crawled: int
    issues_found: int
    critical_issues: int
    started_at: datetime | None
    completed_at: datetime | None
    duration_seconds: int | None
    created_at: datetime


class IssueResponse(BaseModel):
    id: UUID
    rule_id: str
    title: str
    description: str
    category: str
    severity: str
    impact_score: float
    effort_score: float
    affected_count: int
    affected_urls: list[str]
    recommendation: str
    documentation_url: str | None
    is_resolved: bool


class RecommendationResponse(BaseModel):
    id: UUID
    issue_id: str
    priority_rank: int
    title: str
    description: str
    effort: str
    impact: str
    estimated_traffic_gain: float
    estimated_revenue_impact: float
    implementation_steps: list[str]
    status: str


class PaginatedResponse(BaseModel):
    items: list[Any]
    total: int
    page: int
    per_page: int
    pages: int


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@router.post(
    "",
    response_model=AuditResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a new SEO audit",
    description="Initiates an asynchronous SEO audit for the given URL. Returns immediately with audit ID.",
)
async def create_audit(
    request: CreateAuditRequest,
    db: DBSession,
) -> AuditResponse:
    """
    Start a full SEO audit.

    1. Ensure default organization exists
    2. Resolve or create Site record
    3. Create Audit record
    4. Dispatch Celery workflow
    5. Return 202 with audit ID
    """
    parsed = urlparse(str(request.site_url))
    domain = parsed.netloc.lower()
    root_url = f"{parsed.scheme}://{domain}"

    # Ensure default organization exists
    org_result = await db.execute(select(Organization).where(Organization.id == DEFAULT_ORG_ID))
    org = org_result.scalar_one_or_none()
    if not org:
        org = Organization(
            id=DEFAULT_ORG_ID,
            name="Default Organization",
            slug="default",
            plan="free",
            settings={},
            is_active=True,
        )
        db.add(org)
        await db.flush()

    # Upsert site
    result = await db.execute(select(Site).where(Site.domain == domain))
    site = result.scalar_one_or_none()

    if not site:
        site = Site(
            domain=domain,
            root_url=root_url,
            name=domain,
            organization_id=DEFAULT_ORG_ID,
        )
        db.add(site)
        await db.flush()

    # Create audit
    config = {
        "max_pages": request.max_pages,
        "max_depth": request.max_depth,
        "js_render": request.js_render,
        "rate_limit_rps": request.rate_limit_rps,
        "monthly_traffic": request.monthly_traffic,
    }

    audit = Audit(
        site_id=site.id,
        config=config,
        status="pending",
    )
    db.add(audit)
    await db.commit()
    await db.refresh(audit)

    # Dispatch async workflow
    task = run_full_audit.apply_async(
        args=[str(audit.id), str(site.id), config],
        task_id=str(audit.id),
    )

    # Store Celery task ID
    audit.celery_task_id = task.id
    await db.commit()

    logger.info("Audit created", audit_id=str(audit.id), domain=domain)

    return AuditResponse(
        id=audit.id,
        site_id=site.id,
        status="pending",
        created_at=audit.created_at,
        message="Audit started. Poll /api/v1/audits/{id} for status.",
    )


@router.get(
    "/{audit_id}",
    response_model=AuditDetailResponse,
    summary="Get audit status and summary",
)
async def get_audit(audit_id: UUID, db: DBSession) -> AuditDetailResponse:
    audit = await db.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    return AuditDetailResponse(
        id=audit.id,
        site_id=audit.site_id,
        status=audit.status,
        overall_score=audit.overall_score,
        overall_grade=audit.overall_grade,
        confidence_score=audit.confidence_score,
        estimated_revenue_impact=audit.estimated_revenue_impact,
        pages_crawled=audit.pages_crawled,
        issues_found=audit.issues_found,
        critical_issues=audit.critical_issues,
        started_at=audit.started_at,
        completed_at=audit.completed_at,
        duration_seconds=audit.duration_seconds,
        created_at=audit.created_at,
    )


@router.get(
    "/{audit_id}/issues",
    response_model=PaginatedResponse,
    summary="Get all issues for an audit",
)
async def get_audit_issues(
    audit_id: UUID,
    db: DBSession,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    severity: str | None = Query(None, description="Filter by severity: critical|high|medium|low"),
    category: str | None = Query(None, description="Filter by category"),
) -> PaginatedResponse:
    audit = await db.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    from app.models.models import AuditIssue
    query = select(AuditIssue).where(AuditIssue.audit_id == audit_id)

    if severity:
        query = query.where(AuditIssue.severity == severity)
    if category:
        query = query.where(AuditIssue.category == category)

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar()

    query = query.order_by(AuditIssue.impact_score.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    issues = result.scalars().all()

    return PaginatedResponse(
        items=[
            IssueResponse(
                id=i.id,
                rule_id=i.rule_id,
                title=i.title,
                description=i.description,
                category=i.category,
                severity=i.severity,
                impact_score=i.impact_score,
                effort_score=i.effort_score,
                affected_count=i.affected_count,
                affected_urls=i.affected_urls,
                recommendation=i.recommendation,
                documentation_url=i.documentation_url,
                is_resolved=i.is_resolved,
            )
            for i in issues
        ],
        total=total,
        page=page,
        per_page=per_page,
        pages=-(-total // per_page),
    )


@router.get(
    "/{audit_id}/recommendations",
    response_model=list[RecommendationResponse],
    summary="Get prioritized recommendations for an audit",
)
async def get_recommendations(audit_id: UUID, db: DBSession) -> list[RecommendationResponse]:
    audit = await db.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    if audit.status != "complete":
        raise HTTPException(status_code=409, detail=f"Audit is not complete yet (status: {audit.status})")

    from app.models.models import AuditRecommendation
    result = await db.execute(
        select(AuditRecommendation)
        .where(AuditRecommendation.audit_id == audit_id)
        .order_by(AuditRecommendation.priority_rank)
    )
    recs = result.scalars().all()

    return [
        RecommendationResponse(
            id=r.id,
            issue_id=r.issue_id,
            priority_rank=r.priority_rank,
            title=r.title,
            description=r.description,
            effort=r.effort,
            impact=r.impact,
            estimated_traffic_gain=r.estimated_traffic_gain,
            estimated_revenue_impact=r.estimated_revenue_impact,
            implementation_steps=r.implementation_steps,
            status=r.status,
        )
        for r in recs
    ]


@router.get(
    "/{audit_id}/scores",
    summary="Get score breakdown by category",
)
async def get_score_breakdown(audit_id: UUID, db: DBSession) -> dict:
    audit = await db.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    from app.models.models import CategoryScoreRecord
    result = await db.execute(
        select(CategoryScoreRecord).where(CategoryScoreRecord.audit_id == audit_id)
    )
    scores = result.scalars().all()

    return {
        "overall_score": audit.overall_score,
        "overall_grade": audit.overall_grade,
        "confidence_score": audit.confidence_score,
        "estimated_revenue_impact": audit.estimated_revenue_impact,
        "categories": [
            {
                "category": s.category,
                "score": s.score,
                "grade": s.grade,
                "weight": s.weight,
                "issues_count": s.issues_count,
                "critical_count": s.critical_count,
            }
            for s in scores
        ],
    }