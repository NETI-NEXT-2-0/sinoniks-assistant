import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from partner_agent.infra import (
    models_chat_files,  # noqa: F401 -- register private chat files
    models_designs,  # noqa: F401 -- register project revisions
    models_documents,  # noqa: F401 -- register document metadata
    models_stage2,  # noqa: F401 -- register stage-2 metadata
)
from partner_agent.infra.models import Base
from partner_agent.infra.settings import get_settings

config = context.config
target_metadata = Base.metadata


def offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def migrate(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online() -> None:
    engine = create_async_engine(get_settings().database_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


if context.is_offline_mode():
    offline()
else:
    asyncio.run(online(), loop_factory=asyncio.SelectorEventLoop)
