"""Repair durable scheduling state; automatic lease takeover is opt-in after V3 proof."""

import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from partner_agent.infra.db import session_factory
from partner_agent.infra.errors import APIError
from partner_agent.infra.models_stage2 import Chat, Outbox, Run, RunAttempt
from partner_agent.infra.settings import get_settings
from partner_agent.runs.service import append_event, enqueue_dispatch, finish_run, lock_run
from partner_agent.runtime.scheduler import Scheduler


async def reconcile_once() -> None:
    settings = get_settings()
    async with session_factory()() as db:
        identifiers = list(
            (
                await db.scalars(
                    select(Run.id)
                    .join(Chat, Chat.active_run_id == Run.id)
                    .where(
                        Chat.deleted_at.is_(None),
                        Run.status.in_(["queued", "running", "waiting_input"]),
                    )
                    .order_by(Run.created_at)
                    .limit(100)
                )
            ).all()
        )
    for run_id in identifiers:
        async with session_factory()() as db:
            try:
                chat, run = await lock_run(db, run_id)
            except APIError:
                continue
            now = await db.scalar(select(func.clock_timestamp()))
            assert now is not None
            if chat.active_run_id != run.id:
                continue
            if run.status == "queued":
                await enqueue_dispatch(db, run)
                event = await db.scalar(
                    select(Outbox).where(
                        Outbox.run_id == run.id,
                        Outbox.dispatch_generation == run.dispatch_generation,
                    )
                )
                if (
                    event is not None
                    and event.status == "dispatched"
                    and run.created_at < now - timedelta(minutes=5)
                ):
                    # Scheduling acknowledgement is not worker progress. Keep it visible.
                    run.outcome = {
                        "code": "scheduler_progress_delayed",
                        "profile": "offline_fixture",
                    }
            elif run.status == "waiting_input":
                if run.waiting_since is not None and run.waiting_since < now - timedelta(hours=24):
                    await finish_run(db, chat, run, "cancelled", {"code": "input_timeout"})
            elif run.status == "running" and (
                run.lease_expires_at is None or run.lease_expires_at <= now
            ):
                if not settings.runtime_automatic_takeover:
                    if not (run.outcome or {}).get("recovery_required"):
                        run.outcome = {
                            "code": "manual_recovery_required",
                            "recovery_required": True,
                        }
                        append_event(
                            db, chat, run, "run.progress", {"code": "manual_recovery_required"}
                        )
                else:
                    # Only enabled after actual saver rollback/pending-write fencing proof.
                    if run.running_since is not None:
                        run.active_duration_ms += max(
                            0, int((now - run.running_since).total_seconds() * 1000)
                        )
                    attempts = (
                        await db.scalars(
                            select(RunAttempt).where(
                                RunAttempt.run_id == run.id,
                                RunAttempt.status == "running",
                            )
                        )
                    ).all()
                    for attempt in attempts:
                        attempt.status, attempt.ended_at = "lease_expired", now
                        attempt.error_code = "lease_expired"
                    run.running_since = None
                    run.status = "queued"
                    run.owner_token, run.lease_expires_at = None, None
                    run.fence += 1
                    run.dispatch_generation += 1
                    run.outcome = None
                    await enqueue_dispatch(db, run)
                    append_event(db, chat, run, "run.queued", {"reason": "lease_recovery"})
            await db.commit()


async def repair_failed_schedules(scheduler: Scheduler) -> None:
    async with session_factory()() as db:
        rows = (
            await db.execute(
                select(Run.id, Outbox.scheduler_id, Run.dispatch_generation)
                .join(
                    Outbox,
                    (Run.id == Outbox.run_id)
                    & (Run.dispatch_generation == Outbox.dispatch_generation),
                )
                .where(
                    Run.status == "queued",
                    Outbox.status == "dispatched",
                    Outbox.scheduler_id.is_not(None),
                )
                .limit(20)
            )
        ).all()
    for run_id, scheduler_id, generation in rows:
        try:
            status = await asyncio.wait_for(scheduler.status(str(scheduler_id)), timeout=10)
        except Exception:
            continue
        if status not in ("FAILED", "CANCELLED", "COMPLETED"):
            continue
        async with session_factory()() as db:
            try:
                chat, run = await lock_run(db, run_id)
            except APIError:
                continue
            if (
                run.status == "queued"
                and run.dispatch_generation == generation
                and chat.active_run_id == run.id
            ):
                run.dispatch_generation += 1
                await enqueue_dispatch(db, run)
                append_event(
                    db, chat, run, "run.progress", {"code": "scheduler_delivery_recovered"}
                )
                await db.commit()


async def main() -> None:
    scheduler = Scheduler()
    while True:
        try:
            await reconcile_once()
            await repair_failed_schedules(scheduler)
        except Exception:
            pass
        await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(main())
