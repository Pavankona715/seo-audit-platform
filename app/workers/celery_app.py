"""
Celery Application Configuration

Queue Architecture:
- crawl_queue:   High memory/CPU workers for crawling
- analysis_queue: Medium workers for audit engines
- report_queue:  Light workers for scoring/reporting
- default:       General tasks

Worker scaling:
- crawl_queue:    2-5 workers (each runs async crawl)
- analysis_queue: 5-20 workers (CPU-bound analysis)
- report_queue:   2-5 workers
"""

from celery import Celery
from celery.signals import after_setup_logger, worker_ready
from kombu import Exchange, Queue

from app.core.config import get_settings

settings = get_settings()

# ─────────────────────────────────────────────
# Celery App
# ─────────────────────────────────────────────

celery_app = Celery(
    "seo_audit_platform",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.workers.audit_tasks",
        "app.workers.crawl_tasks",
        "app.workers.report_tasks",
    ],
)

# ─────────────────────────────────────────────
# Queue Definitions
# ─────────────────────────────────────────────

default_exchange = Exchange("default", type="direct")
crawl_exchange = Exchange("crawl", type="direct")
analysis_exchange = Exchange("analysis", type="direct")
report_exchange = Exchange("report", type="direct")

celery_app.conf.task_queues = (
    Queue("default", default_exchange, routing_key="default"),
    Queue("crawl_queue", crawl_exchange, routing_key="crawl"),
    Queue("analysis_queue", analysis_exchange, routing_key="analysis"),
    Queue("report_queue", report_exchange, routing_key="report"),
)

celery_app.conf.task_default_queue = "default"
celery_app.conf.task_default_exchange = "default"
celery_app.conf.task_default_routing_key = "default"

# ─────────────────────────────────────────────
# Task Routing
# ─────────────────────────────────────────────

celery_app.conf.task_routes = {
    "app.workers.crawl_tasks.*": {"queue": "crawl_queue"},
    "app.workers.audit_tasks.*": {"queue": "analysis_queue"},
    "app.workers.report_tasks.*": {"queue": "report_queue"},
}

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,

    # Reliability
    task_acks_late=True,                    # Ack after completion, not on receive
    task_reject_on_worker_lost=True,        # Re-queue if worker dies
    worker_prefetch_multiplier=1,           # Don't prefetch - process one at a time

    # Timeouts
    task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,

    # Retries
    task_max_retries=settings.CELERY_MAX_RETRIES,

    # Results
    result_expires=86400 * 7,              # Keep results 7 days
    result_backend_transport_options={
        "master_name": "mymaster",
    },

    # Monitoring
    worker_send_task_events=True,
    task_send_sent_event=True,

    # Beat schedule (periodic tasks)
    beat_schedule={
        "cleanup-old-results": {
            "task": "app.workers.report_tasks.cleanup_old_results",
            "schedule": 3600 * 24,  # Daily
        },
    },
)


# ─────────────────────────────────────────────
# Signals
# ─────────────────────────────────────────────

@worker_ready.connect
def on_worker_ready(sender, **kwargs):
    import structlog
    logger = structlog.get_logger("celery.worker")
    logger.info("Celery worker ready", hostname=sender.hostname)


@after_setup_logger.connect
def setup_celery_logging(logger, *args, **kwargs):
    from app.core.logging import configure_logging
    configure_logging()
