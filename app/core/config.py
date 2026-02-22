"""
Configuration system with environment-based settings.
Uses pydantic-settings for validation and type safety.
"""

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    # Application
    APP_VERSION: str = "1.0.0"
    ENV: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False
    SECRET_KEY: str = Field(..., min_length=32)
    API_KEY_HEADER: str = "X-API-Key"

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 4

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # Database — plain str to avoid pydantic MultiHostUrl mangling the username
    POSTGRES_DSN: str = Field(..., description="PostgreSQL connection string")
    POSTGRES_POOL_SIZE: int = 20
    POSTGRES_MAX_OVERFLOW: int = 40
    POSTGRES_POOL_TIMEOUT: int = 30
    POSTGRES_ECHO: bool = False

    # Redis
    REDIS_DSN: RedisDsn = Field(..., description="Redis connection string")
    REDIS_MAX_CONNECTIONS: int = 50
    REDIS_SOCKET_TIMEOUT: float = 5.0

    # Celery
    CELERY_BROKER_URL: str = Field(..., description="Celery broker URL (Redis)")
    CELERY_RESULT_BACKEND: str = Field(..., description="Celery result backend")
    CELERY_TASK_SOFT_TIME_LIMIT: int = 3600   # 1 hour
    CELERY_TASK_TIME_LIMIT: int = 7200        # 2 hours hard limit
    CELERY_MAX_RETRIES: int = 3
    CELERY_RETRY_BACKOFF: int = 60

    # Crawler
    CRAWLER_MAX_CONCURRENCY: int = 20
    CRAWLER_MAX_PAGES_PER_AUDIT: int = 50_000
    CRAWLER_REQUEST_TIMEOUT: int = 30
    CRAWLER_RATE_LIMIT_RPS: float = 5.0
    CRAWLER_USER_AGENT: str = "SEOAuditBot/1.0 (+https://seoplatform.com/bot)"
    CRAWLER_JS_RENDER_TIMEOUT: int = 15_000   # ms for Playwright

    # Storage
    S3_BUCKET: str = Field(..., description="S3 bucket for crawl artifacts")
    S3_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""

    # External APIs
    PAGESPEED_API_KEY: str = ""
    AHREFS_API_KEY: str = ""
    MOZ_API_KEY: str = ""
    SEMRUSH_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"

    # Scoring weights (must sum to 1.0)
    WEIGHT_CRAWLABILITY: float = 0.15
    WEIGHT_TECHNICAL: float = 0.20
    WEIGHT_ON_PAGE: float = 0.15
    WEIGHT_CONTENT: float = 0.15
    WEIGHT_PERFORMANCE: float = 0.15
    WEIGHT_INTERNAL_LINKS: float = 0.10
    WEIGHT_SCHEMA: float = 0.05
    WEIGHT_AUTHORITY: float = 0.05

    # Rate Limiting
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = 60
    RATE_LIMIT_BURST: int = 10

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors(cls, v: str | list) -> list:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v

    @property
    def postgres_url(self) -> str:
        """Async URL for SQLAlchemy + asyncpg."""
        url = self.POSTGRES_DSN
        for scheme in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://", "postgres://"):
            if url.startswith(scheme):
                return "postgresql+asyncpg://" + url[len(scheme):]
        return url

    @property
    def postgres_sync_url(self) -> str:
        """Sync URL for SQLAlchemy + psycopg2."""
        url = self.POSTGRES_DSN
        for scheme in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://", "postgres://"):
            if url.startswith(scheme):
                return "postgresql+psycopg2://" + url[len(scheme):]
        return url


@lru_cache()
def get_settings() -> Settings:
    """Cached settings instance - created once per process."""
    return Settings()