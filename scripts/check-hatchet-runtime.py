#!/usr/bin/env python3
"""Run inside agent-worker with relay paused; real scheduler, UUID-scoped synthetic data."""

import asyncio
import json
import time
from uuid import uuid4

from sqlalchemy import delete, func, select

from partner_agent.infra.db import session_factory
from partner_agent.infra.models import User
from partner_agent.infra.models_stage2 import (
    Chat,
    ChatBranch,
    Message,
    Outbox,
    Run,
    RunAttempt,
)
from partner_agent.runs.service import admit_run, finish_run, lock_run
from partner_agent.runtime.relay import deliver_one
from partner_agent.runtime.scheduler import Scheduler


async def main():
    scheduler = Scheduler()
    users = [uuid4(), uuid4()]
    runs = []
    report = {}

    async def create(user_id, content):
        async with session_factory()() as db:
            user = await db.get(User, user_id)
            chat = Chat(id=uuid4(), owner_id=user_id)
            db.add(chat)
            await db.flush()
            branch = ChatBranch(id=uuid4(), chat_id=chat.id)
            db.add(branch)
            await db.flush()
            chat.default_branch_id = branch.id
            run = await admit_run(db, chat, branch, user, content)
            await db.commit()
            runs.append(run.id)
            return run.id

    async def inspect(run_id):
        async with session_factory()() as db:
            run = await db.get(Run, run_id)
            event = await db.scalar(select(Outbox).where(Outbox.run_id == run_id))
            return {
                "status": run.status,
                "partial": run.partial_text,
                "started_at": run.started_at,
                "scheduler_id": event.scheduler_id,
                "cancel_sent": event.last_error_code == "cancel_sent",
            }

    async def until(run_id, predicate, timeout=35):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = await inspect(run_id)
            if predicate(state):
                return state
            await asyncio.sleep(0.1)
        raise RuntimeError("Runtime smoke timed out waiting for a technical state")

    async def cancel(run_id):
        async with session_factory()() as db:
            chat, run = await lock_run(db, run_id)
            await finish_run(db, chat, run, "cancelled", {"code": "synthetic_smoke_cancel"})
            await db.commit()
        state = await inspect(run_id)
        if state["scheduler_id"]:
            await scheduler.cancel(state["scheduler_id"])

    class UnknownOnce:
        injected = False

        async def dispatch(self, payload):
            await scheduler.dispatch(payload)
            self.injected = True
            raise TimeoutError("Synthetic lost scheduler acknowledgement")

        async def cancel(self, scheduler_id):
            await scheduler.cancel(scheduler_id)

    async with session_factory()() as db:
        for identifier in users:
            db.add(
                User(
                    id=identifier,
                    normalized_login="runtime-smoke-" + identifier.hex,
                    password_hash="unused-synthetic-account",
                    role="user",
                    disabled=False,
                )
            )
        await db.commit()
    try:
        # Acceptance is lost after the actual server received the request.
        normal = await create(users[0], "Проверка реального Hatchet")
        lost = UnknownOnce()
        assert await deliver_one(lost, normal) and lost.injected
        finished = await until(normal, lambda state: state["status"] == "succeeded")
        assert finished["scheduler_id"]
        async with session_factory()() as db:
            attempts = await db.scalar(
                select(func.count()).select_from(RunAttempt).where(RunAttempt.run_id == normal)
            )
            finals = await db.scalar(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.run_id == normal,
                    Message.role == "assistant",
                    Message.status == "completed",
                )
            )
            assert attempts == finals == 1
        report["unknown_acceptance_single_result"] = True

        # Two chats of the same user execute concurrently, capped globally at two.
        first = await create(users[0], "/fixture slow first")
        second = await create(users[0], "/fixture slow second")
        await deliver_one(scheduler, first)
        await deliver_one(scheduler, second)
        await until(first, lambda state: state["status"] == "running" and bool(state["partial"]))
        await until(
            second,
            lambda state: state["status"] == "running" and bool(state["partial"]),
        )
        report["same_user_independent_chats"] = True

        # User A's third chat is queued upstream; user B reaches the shared cap.
        third = await create(users[0], "A third queued chat")
        await deliver_one(scheduler, third)
        other = await create(users[1], "B fair scheduling")
        await deliver_one(scheduler, other)
        await asyncio.sleep(0.5)
        async with session_factory()() as db:
            active = await db.scalar(
                select(func.count())
                .select_from(Run)
                .where(Run.id.in_(runs), Run.status == "running")
            )
            assert active == 2
        await cancel(first)
        b = await until(other, lambda state: state["status"] == "succeeded")
        a = await until(third, lambda state: state["status"] == "succeeded")
        assert b["started_at"] <= a["started_at"], "Another user's waiting chat was starved"
        report["global_two_and_user_round_robin"] = True
        await cancel(second)
        for identifier in (first, second):
            sid = (await inspect(identifier))["scheduler_id"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if await scheduler.status(sid) == "CANCELLED":
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("Hatchet cancellation was not confirmed")
        report["actual_scheduler_cancel"] = True
        print(json.dumps(report, sort_keys=True))
    finally:
        # Stop authoritative writers before maintenance-only checkpoint cleanup.
        for identifier in runs:
            state = await inspect(identifier)
            if state["status"] not in ("succeeded", "failed", "cancelled"):
                await cancel(identifier)
        await asyncio.sleep(0.3)
        async with session_factory()() as db:
            await db.execute(delete(User).where(User.id.in_(users)))
            await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
