"""Serialize audience mutation with final authorization, without locking during remote I/O."""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Dedicated transaction-scoped advisory lock, unrelated to worker/session locks.
AUDIENCE_LOCK_KEY = 8642917502


async def audience_read_lock(db: AsyncSession) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": AUDIENCE_LOCK_KEY})


async def audience_write_lock(db: AsyncSession) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": AUDIENCE_LOCK_KEY})
