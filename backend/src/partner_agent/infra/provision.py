"""Run once after Alembic, never from API startup or an Alembic transaction."""

import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from partner_agent.infra.settings import get_settings


async def provision_checkpoints() -> None:
    url = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    async with await AsyncConnection.connect(
        url,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options="-c search_path=langgraph,public",
    ) as connection:
        saver = AsyncPostgresSaver(connection)
        await saver.setup()


if __name__ == "__main__":
    asyncio.run(provision_checkpoints())
