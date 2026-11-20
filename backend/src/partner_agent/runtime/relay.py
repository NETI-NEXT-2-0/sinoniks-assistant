"""Transactional outbox delivery. Scheduler requests always happen after PG commit."""

import asyncio
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, true

from partner_agent.infra.db import session_factory
from partner_agent.infra.models_stage2 import Chat, Outbox, Run
from partner_agent.runtime.scheduler import DispatchInput, Scheduler


class SchedulerPort(Protocol):
    async def dispatch(self, payload: DispatchInput) -> str: ...
    async def cancel(self, scheduler_id: str) -> None: ...


@dataclass(frozen=True)
class Delivery:
    id: UUID
    claim_token: UUID
    payload: DispatchInput


async def claim_delivery(run_id: UUID | None = None) -> Delivery | None:
    async with session_factory()() as db:
        now = await db.scalar(select(func.clock_timestamp()))
        assert now is not None
        row = await db.scalar(
            select(Outbox)
            .join(Run, Outbox.run_id == Run.id)
            .join(Chat, Run.chat_id == Chat.id)
            .where(
                Run.id == run_id if run_id is not None else true(),
                Run.status == "queued",
                Chat.active_run_id == Run.id,
                Chat.deleted_at.is_(None),
                Run.dispatch_generation == Outbox.dispatch_generation,
                Outbox.next_attempt_at <= now,
                or_(
                    Outbox.status == "pending",
                    (Outbox.status == "claimed") & (Outbox.lease_expires_at <= now),
                ),
            )
            .order_by(Outbox.next_attempt_at, Outbox.created_at)
            .limit(1)
            .with_for_update(skip_locked=True, of=Outbox)
        )
        if row is None:
            return None
        run = await db.get(Run, row.run_id)
        assert run is not None
        row.status = "claimed"
        row.claim_token = uuid4()
        row.lease_expires_at = now + timedelta(seconds=30)
        row.attempts += 1
        delivery = Delivery(
            row.id,
            row.claim_token,
            DispatchInput(
                run_id=run.id,
                chat_id=run.chat_id,
                user_id=run.user_id,
                dispatch_generation=row.dispatch_generation,
                dispatch_key=row.logical_key,
            ),
        )
        await db.commit()
        return delivery


async def deliver_one(scheduler: SchedulerPort, run_id: UUID | None = None) -> bool:
    delivery = await claim_delivery(run_id)
    if delivery is None:
        return False
    scheduler_id = None
    try:
        scheduler_id = await asyncio.wait_for(scheduler.dispatch(delivery.payload), timeout=20)
    except Exception:
        # Includes unknown acceptance: retries retain exactly the same dispatch key.
        pass
    async with session_factory()() as db:
        event = await db.scalar(
            select(Outbox)
            .where(
                Outbox.id == delivery.id,
                Outbox.status == "claimed",
                Outbox.claim_token == delivery.claim_token,
            )
            .with_for_update()
        )
        if event is None:
            return True  # Worker may have confirmed acceptance using its actual scheduler ID.
        now = await db.scalar(select(func.clock_timestamp()))
        assert now is not None
        event.claim_token = None
        event.lease_expires_at = None
        if scheduler_id is not None:
            event.status = "dispatched"
            event.scheduler_id = scheduler_id
            event.last_error_code = None
        else:
            event.status = "pending"
            event.last_error_code = "scheduler_acceptance_unknown"
            delay = min(60, 2 ** min(event.attempts, 6)) + random.uniform(0, 1)
            event.next_attempt_at = now + timedelta(seconds=delay)
        await db.commit()
    return True


async def cancel_terminal_jobs(scheduler: SchedulerPort) -> None:
    async with session_factory()() as db:
        rows = (
            await db.execute(
                select(Outbox.id, Outbox.scheduler_id)
                .join(Run)
                .where(
                    Run.status == "cancelled",
                    Outbox.scheduler_id.is_not(None),
                    or_(Outbox.last_error_code.is_(None), Outbox.last_error_code != "cancel_sent"),
                )
                .limit(20)
            )
        ).all()
    for event_id, scheduler_id in rows:
        try:
            await asyncio.wait_for(scheduler.cancel(str(scheduler_id)), timeout=10)
        except Exception:
            continue
        async with session_factory()() as db:
            event = await db.get(Outbox, event_id)
            if event is not None:
                event.last_error_code = "cancel_sent"
                await db.commit()


async def main() -> None:
    scheduler = Scheduler()
    while True:
        try:
            for _ in range(16):
                if not await deliver_one(scheduler):
                    break
            await cancel_terminal_jobs(scheduler)
        except Exception:
            # A dependency outage never invents acceptance; durable rows remain retryable.
            pass
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
