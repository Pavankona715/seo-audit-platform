"""Health check endpoints for load balancer and monitoring."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str
    checks: dict[str, str]


@router.get("", response_model=HealthResponse, include_in_schema=False)
async def health_check() -> HealthResponse:
    from app.core.config import get_settings
    settings = get_settings()

    checks: dict[str, str] = {}

    try:
        from app.core.database import AsyncSessionLocal
        import sqlalchemy
        async with AsyncSessionLocal() as session:
            await session.execute(sqlalchemy.text("SELECT 1"))
        checks["database"] = "healthy"
    except Exception as e:
        checks["database"] = f"unhealthy: {str(e)}"

    try:
        from app.core.redis import get_redis_client
        redis = await get_redis_client()
        await redis.ping()
        checks["redis"] = "healthy"
    except Exception as e:
        checks["redis"] = f"unhealthy: {str(e)}"

    overall = "healthy" if all("unhealthy" not in v for v in checks.values()) else "degraded"

    return HealthResponse(
        status=overall,
        version=settings.APP_VERSION,
        checks=checks,
    )


@router.get("/ready", include_in_schema=False)
async def readiness() -> dict:
    """Kubernetes readiness probe."""
    return {"ready": True}


@router.get("/live", include_in_schema=False)
async def liveness() -> dict:
    """Kubernetes liveness probe."""
    return {"alive": True}
