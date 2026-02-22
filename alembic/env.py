"""
Alembic migration environment.
Configured for async SQLAlchemy with asyncpg.
"""

import asyncio
import ssl
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# Import all models so Alembic can detect them
from app.models.models import *  # noqa
from app.core.database import Base
from app.core.config import get_settings

settings = get_settings()

config = context.config
config.set_main_option("sqlalchemy.url", settings.postgres_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    connectable = create_async_engine(
        settings.postgres_url,
        poolclass=pool.NullPool,
        # Disable prepared statements at the SQLAlchemy dialect level
        # Required for Supabase pgbouncer in transaction mode
        execution_options={"no_parameters": True},
        connect_args={
            "ssl": ssl_context,
            "prepared_statement_cache_size": 0,
            "statement_cache_size": 0,
        },
    )

    async with connectable.connect() as connection:
        # Also set at connection level
        await connection.execution_options(no_parameters=True)
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()