"""
SEO Audit Platform - Main Application Entry Point
Production-grade FastAPI application with lifespan management.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.routes import audits, health
from app.core.config import get_settings
from app.core.database import engine
from app.core.logging import configure_logging
from app.core.redis import get_redis_client

logger = structlog.get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """Manage application lifecycle: startup and shutdown."""
    configure_logging()
    logger.info("Starting SEO Audit Platform", version=settings.APP_VERSION, env=settings.ENV)

    # Verify DB connectivity
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: c.execute(__import__("sqlalchemy").text("SELECT 1")))
    logger.info("Database connection verified")

    # Verify Redis
    redis = await get_redis_client()
    await redis.ping()
    logger.info("Redis connection verified")

    yield

    # Graceful shutdown
    await engine.dispose()
    await redis.aclose()
    logger.info("Application shutdown complete")


def create_application() -> FastAPI:
    app = FastAPI(
        title="SEO Audit Platform API",
        description="Enterprise-grade SEO audit engine powering modular analysis.",
        version=settings.APP_VERSION,
        docs_url="/docs" if settings.ENV != "production" else None,
        redoc_url="/redoc" if settings.ENV != "production" else None,
        lifespan=lifespan,
    )

    # Middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # Routers
    app.include_router(health.router, prefix="/health", tags=["Health"])
    app.include_router(audits.router, prefix="/api/v1/audits", tags=["Audits"])
    # TODO: add pages, reports, scores routers when implemented

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled exception", path=request.url.path, error=str(exc), exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request.headers.get("x-request-id")},
        )

    return app


app = create_application()