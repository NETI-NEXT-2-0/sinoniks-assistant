"""Native PostgreSQL proof for saver3.2.0, outer transactions and ownership fencing."""

import asyncio
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    empty_checkpoint,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from sqlalchemy import delete, func, select

from partner_agent.infra.db import get_engine, session_factory
from partner_agent.infra.models import User
from partner_agent.infra.models_stage2 import Chat, ChatBranch, ResumeCommand, Run
from partner_agent.infra.settings import get_settings
from partner_agent.runs.service import StaleOwnership, admit_run
from partner_agent.runtime.fenced import FencedSaver
from partner_agent.runtime.fixture import fixture_graph
from partner_agent.runtime.lifecycle import Claim, claim_run

pytestmark = pytest.mark.integration


@dataclass
class Case:
    claim: Claim
    url: str
    user_id: UUID


@pytest_asyncio.fixture
async def case(monkeypatch, request):
    url = os.getenv("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Native PostgreSQL test database is required")
    assert url.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test")
    monkeypatch.setenv("PARTNER_DATABASE_URL", url)
    monkeypatch.setenv("PARTNER_ENVIRONMENT", "test")
    monkeypatch.setenv("PARTNER_RUNTIME_FIXTURE_DELAY_SECONDS", "0")
    get_settings.cache_clear()
    get_engine.cache_clear()
    user_id = uuid4()
    async with session_factory()() as db:
        user = User(
            id=user_id,
            normalized_login="fence-" + user_id.hex,
            password_hash="unused",
            role="user",
            disabled=False,
        )
        db.add(user)
        await db.flush()
        chat = Chat(id=uuid4(), owner_id=user.id)
        db.add(chat)
        await db.flush()
        branch = ChatBranch(id=uuid4(), chat_id=chat.id)
        db.add(branch)
        await db.flush()
        chat.default_branch_id = branch.id
        run = await admit_run(db, chat, branch, user, getattr(request, "param", "Native fixture"))
        await db.commit()
        run_id = run.id
    claim = await claim_run(run_id, 0)
    assert claim is not None
    native = url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        yield Case(claim, native, user_id)
    finally:
        async with await AsyncConnection.connect(native, autocommit=True) as conn:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await conn.execute(
                    f"DELETE FROM langgraph.{table} WHERE thread_id = %s", (str(run_id),)
                )
        async with session_factory()() as db:
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()


def input_checkpoint(
    case: Case,
) -> tuple[RunnableConfig, Checkpoint, CheckpointMetadata, ChannelVersions]:
    value = empty_checkpoint()
    value["channel_values"] = {
        "__start__": {"input_message_id": str(case.claim.input_message_id), "values": [1, 2, 3]}
    }
    value["channel_versions"] = {"__start__": "1"}
    return (
        {"configurable": {"thread_id": str(case.claim.ownership.run_id), "checkpoint_ns": ""}},
        value,
        {"source": "input", "step": -1, "parents": {}},
        {"__start__": "1"},
    )


async def counts(connection, run_id: UUID) -> list[int]:
    result = []
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        cursor = await connection.execute(
            f"SELECT count(*) AS n FROM langgraph.{table} WHERE thread_id = %s", (str(run_id),)
        )
        result.append((await cursor.fetchone())["n"])
    return result


class FailAfterPut(AsyncPostgresSaver):
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        await super().aput(config, checkpoint, metadata, new_versions)
        raise RuntimeError("Injected failure after actual delegate write")


class ExpireAfterWrites(AsyncPostgresSaver):
    completed = False

    async def expire(self, config: RunnableConfig) -> None:
        # Inject expiry after the real write, on the same locked transaction.
        # This tests the final guard without racing a 100 ms setup deadline.
        assert isinstance(self.conn, AsyncConnection)
        await self.conn.execute(
            "UPDATE public.runs SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE id = %s", (UUID(config["configurable"]["thread_id"]),),
        )
        self.completed = True

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        result = await super().aput(config, checkpoint, metadata, new_versions)
        await self.expire(config)
        return result

    async def aput_writes(
        self, config: RunnableConfig, writes: Any, task_id: str, task_path: str = ""
    ) -> None:
        await super().aput_writes(config, writes, task_id, task_path)
        await self.expire(config)


@pytest.mark.parametrize("pipeline", [True, False])
async def test_input_marker_and_blob_commit_on_same_connection(case, pipeline):
    async with await AsyncConnection.connect(
        case.url, autocommit=True, row_factory=dict_row, options="-c search_path=langgraph,public"
    ) as conn:
        delegate = AsyncPostgresSaver(conn)
        if pipeline:
            assert delegate.supports_pipeline, "V3 must exercise native libpq pipeline support"
        else:
            delegate.supports_pipeline = False
        saver = FencedSaver(
            conn, case.claim.ownership, case.claim.input_message_id, delegate=delegate
        )
        config, checkpoint, metadata, versions = input_checkpoint(case)
        saved = await saver.aput(config, checkpoint, metadata, versions)
        assert saved["configurable"]["checkpoint_id"] == checkpoint["id"]
        assert (await counts(conn, case.claim.ownership.run_id)) == [1, 1, 0]
    async with session_factory()() as db:
        run = await db.get(Run, case.claim.ownership.run_id)
        assert run.input_applied and run.input_checkpoint_id == checkpoint["id"]


async def test_failure_after_real_pipeline_put_rolls_back_everything(case):
    async with await AsyncConnection.connect(
        case.url, autocommit=True, row_factory=dict_row, options="-c search_path=langgraph,public"
    ) as conn:
        saver = FencedSaver(
            conn, case.claim.ownership, case.claim.input_message_id, delegate=FailAfterPut(conn)
        )
        with pytest.raises(RuntimeError, match="Injected failure"):
            await saver.aput(*input_checkpoint(case))
        assert await counts(conn, case.claim.ownership.run_id) == [0, 0, 0]
    async with session_factory()() as db:
        assert not (await db.get(Run, case.claim.ownership.run_id)).input_applied


async def test_lease_expiry_after_delegate_rolls_back_checkpoint_blob_and_input_marker(case):
    async with await AsyncConnection.connect(
        case.url, autocommit=True, row_factory=dict_row, options="-c search_path=langgraph,public"
    ) as conn:
        saver = FencedSaver(
            conn, case.claim.ownership, case.claim.input_message_id,
            delegate=ExpireAfterWrites(conn),
        )
        with pytest.raises(StaleOwnership):
            await saver.aput(*input_checkpoint(case))
        assert saver.delegate.completed
        assert await counts(conn, case.claim.ownership.run_id) == [0, 0, 0]
    async with session_factory()() as db:
        run = await db.get(Run, case.claim.ownership.run_id)
        assert not run.input_applied and run.input_checkpoint_id is None


async def test_pending_resume_write_and_marker_rollback_after_expiry(case):
    command_id = uuid4()
    async with session_factory()() as db:
        run = await db.get(Run, case.claim.ownership.run_id)
        db.add(
            ResumeCommand(
                id=command_id,
                run_id=run.id,
                clarification_id=uuid4(),
                answer_message_id=run.input_message_id,
            )
        )
        run.pending_resume_command_id = command_id
        await db.commit()
    async with await AsyncConnection.connect(
        case.url, autocommit=True, row_factory=dict_row, options="-c search_path=langgraph,public"
    ) as conn:
        ordinary = FencedSaver(conn, case.claim.ownership, case.claim.input_message_id)
        config = await ordinary.aput(*input_checkpoint(case))
        saver = FencedSaver(
            conn, case.claim.ownership, case.claim.input_message_id,
            delegate=ExpireAfterWrites(conn),
        )
        with pytest.raises(StaleOwnership):
            await saver.aput_writes(
                config,
                [("__resume__", {"resume_command_id": str(command_id), "answer": "yes"})],
                "resume-task",
            )
        assert saver.delegate.completed
        assert await counts(conn, case.claim.ownership.run_id) == [1, 1, 0]
    async with session_factory()() as db:
        command = await db.get(ResumeCommand, command_id)
        assert not command.input_applied and command.checkpoint_id is None


async def test_stale_fence_cannot_write_or_read_another_thread(case):
    async with session_factory()() as db:
        run = await db.get(Run, case.claim.ownership.run_id)
        run.fence += 1
        await db.commit()
    async with await AsyncConnection.connect(
        case.url, autocommit=True, row_factory=dict_row, options="-c search_path=langgraph,public"
    ) as conn:
        saver = FencedSaver(conn, case.claim.ownership, case.claim.input_message_id)
        with pytest.raises(StaleOwnership):
            await saver.aput(*input_checkpoint(case))
        with pytest.raises(StaleOwnership):
            await saver.aget_tuple({"configurable": {"thread_id": str(uuid4())}})
        assert await counts(conn, case.claim.ownership.run_id) == [0, 0, 0]


class CrashAfterResume(FencedSaver):
    async def aput_writes(self, config, writes, task_id, task_path=""):
        await super().aput_writes(config, writes, task_id, task_path)
        if any(channel == "__resume__" for channel, _ in writes):
            raise RuntimeError("Injected process failure after committed resume marker")


@pytest.mark.parametrize("case", ["/fixture clarify native test"], indirect=True)
async def test_committed_resume_marker_recovers_with_none_without_reapplying_answer(case):
    published = []

    async def publish(value):
        if not published or published[-1] != value:
            published.append(value)

    async with await AsyncConnection.connect(
        case.url, autocommit=True, row_factory=dict_row, options="-c search_path=langgraph,public"
    ) as conn:
        saver = FencedSaver(conn, case.claim.ownership, case.claim.input_message_id)
        config = {
            "configurable": {"thread_id": str(case.claim.ownership.run_id), "checkpoint_ns": ""},
            "recursion_limit": 12,
        }
        graph = fixture_graph(saver, publish, chunk_size=64, delay_seconds=0)
        initial = await graph.ainvoke(case.claim.graph_input, config, durability="sync")
        assert initial["__interrupt__"]
        command_id = uuid4()
        async with session_factory()() as db:
            run = await db.get(Run, case.claim.ownership.run_id)
            db.add(
                ResumeCommand(
                    id=command_id,
                    run_id=run.id,
                    clarification_id=uuid4(),
                    answer_message_id=run.input_message_id,
                )
            )
            run.pending_resume_command_id = command_id
            await db.commit()
        crashing = fixture_graph(
            CrashAfterResume(conn, case.claim.ownership, case.claim.input_message_id),
            publish,
            chunk_size=64,
            delay_seconds=0,
        )
        with pytest.raises(RuntimeError, match="Injected process failure"):
            await crashing.ainvoke(
                Command(resume={"resume_command_id": str(command_id), "answer": "yes"}),
                config,
                durability="sync",
            )
        async with session_factory()() as db:
            command = await db.get(ResumeCommand, command_id)
            assert command.input_applied and command.checkpoint_id
        recovered = await graph.ainvoke(None, config, durability="sync")
        assert "Уточнение: yes." in recovered["answer"]
        number = len(published)
        await graph.ainvoke(None, config, durability="sync")
        assert len(published) == number
        async with session_factory()() as db:
            run = await db.get(Run, case.claim.ownership.run_id)
            assert run.input_applied


@pytest.mark.parametrize("operation", ["checkpoint", "pending_writes"])
@pytest.mark.parametrize("first_writer", ["cancel", "saver"])
async def test_cancel_and_native_saver_serialize_on_same_chat_lock(case, operation, first_writer):
    from partner_agent.runs.service import finish_run, lock_run

    entered = asyncio.Event()
    release = asyncio.Event()
    run_id = case.claim.ownership.run_id

    class PausedDelegate(AsyncPostgresSaver):
        async def aput(self, *args, **kwargs):
            result = await super().aput(*args, **kwargs)
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return result

        async def aput_writes(self, *args, **kwargs):
            await super().aput_writes(*args, **kwargs)
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)

    async with await AsyncConnection.connect(
        case.url, autocommit=True, row_factory=dict_row, options="-c search_path=langgraph,public"
    ) as conn:
        ordinary = FencedSaver(conn, case.claim.ownership, case.claim.input_message_id)
        config = input_checkpoint(case)[0]
        if operation == "pending_writes":
            config = await ordinary.aput(*input_checkpoint(case))
        saver = FencedSaver(
            conn, case.claim.ownership, case.claim.input_message_id,
            delegate=PausedDelegate(conn),
        )

        async def save():
            if operation == "checkpoint":
                await saver.aput(*input_checkpoint(case))
            else:
                await saver.aput_writes(config, [("answer", "synthetic")], "race-task")

        async def cancel():
            async with session_factory()() as db:
                chat, run = await lock_run(db, run_id)
                await finish_run(db, chat, run, "cancelled", {"code": "race_cancel"})
                if first_writer == "cancel":
                    entered.set()
                    await asyncio.wait_for(release.wait(), timeout=5)
                await db.commit()

        first = asyncio.create_task(cancel() if first_writer == "cancel" else save())
        second = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            second = asyncio.create_task(save() if first_writer == "cancel" else cancel())
            # The second connection must wait for the first transaction's row lock.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(second), timeout=0.05)
        finally:
            release.set()
            results = await asyncio.gather(
                *([first, second] if second is not None else [first]), return_exceptions=True
            )
        assert results[0] is None
        if first_writer == "cancel":
            assert isinstance(results[1], StaleOwnership)
        else:
            assert results[1] is None
        expected = [0, 0, 0] if operation == "checkpoint" else [1, 1, 0]
        if first_writer == "saver":
            expected = [1, 1, 0] if operation == "checkpoint" else [1, 1, 1]
        assert await counts(conn, run_id) == expected
        with pytest.raises(StaleOwnership):
            await saver.aput_writes(config, [("answer", "late")], "late-task")
        assert await counts(conn, run_id) == expected
    async with session_factory()() as db:
        run = await db.get(Run, run_id)
        assert run.status == "cancelled" and run.assistant_message_id is None
        assert run.owner_token is None


@pytest.mark.parametrize("operation", ["checkpoint", "pending_writes"])
async def test_killed_writer_rolls_back_native_transaction_and_releases_locks(case, operation):
    import json
    import sys

    from partner_agent.runs.service import finish_run, lock_run

    config, checkpoint, metadata, versions = input_checkpoint(case)
    run_id = case.claim.ownership.run_id
    async with await AsyncConnection.connect(
        case.url, autocommit=True, row_factory=dict_row, options="-c search_path=langgraph,public"
    ) as connection:
        if operation == "pending_writes":
            saver = FencedSaver(connection, case.claim.ownership, case.claim.input_message_id)
            config = await saver.aput(config, checkpoint, metadata, versions)
        baseline = await counts(connection, run_id)
        owner = case.claim.ownership
        payload = {
            "url": case.url,
            "owner": {name: str(getattr(owner, name)) for name in (
                "run_id", "chat_id", "branch_id", "owner_token"
            )},
            "fence": owner.fence,
            "input_message_id": str(case.claim.input_message_id),
            "operation": operation,
            "config": config,
            "checkpoint": checkpoint,
            "metadata": metadata,
            "versions": versions,
        }
        # Credentials travel over stdin, never argv or captured child diagnostics.
        child_code = '''
import asyncio, json, sys
from uuid import UUID
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from partner_agent.runs.service import Ownership
from partner_agent.runtime.fenced import FencedSaver

print("imports_ready", flush=True)

async def main():
    data = json.loads(sys.stdin.readline())
    class PausedSaver(AsyncPostgresSaver):
        async def aput(self, *args, **kwargs):
            result = await super().aput(*args, **kwargs)
            print("delegate_written", flush=True)
            await asyncio.Event().wait()
            return result
        async def aput_writes(self, *args, **kwargs):
            await super().aput_writes(*args, **kwargs)
            print("delegate_written", flush=True)
            await asyncio.Event().wait()
    owner = Ownership(**{k: UUID(v) for k, v in data["owner"].items()}, fence=data["fence"])
    async with await AsyncConnection.connect(
        data["url"], autocommit=True, row_factory=dict_row,
        options="-c search_path=langgraph,public"
    ) as conn:
        saver = FencedSaver(conn, owner, UUID(data["input_message_id"]), delegate=PausedSaver(conn))
        if data["operation"] == "checkpoint":
            await saver.aput(data["config"], data["checkpoint"], data["metadata"], data["versions"])
        else:
            await saver.aput_writes(data["config"], [("answer", "uncommitted")], "killed-task")
asyncio.run(main())
'''
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", child_code,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            process.stdin.write((json.dumps(payload) + "\n").encode())
            await process.stdin.drain()
            process.stdin.close()
            imported = await asyncio.wait_for(process.stdout.readline(), timeout=30)
            assert imported == b"imports_ready\n", "Child did not finish bounded imports"
            signal = await asyncio.wait_for(process.stdout.readline(), timeout=10)
            assert signal == b"delegate_written\n", "Child did not reach the native write"
            # Its uncommitted saver rows are invisible on this independent connection.
            assert await counts(connection, run_id) == baseline
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
            assert process.returncode < 0
            # Acquiring the same locks proves PG released the killed writer's transaction.
            async with session_factory()() as db:
                chat, run = await asyncio.wait_for(lock_run(db, run_id), timeout=5)
                await finish_run(db, chat, run, "cancelled", {"code": "writer_process_killed"})
                await db.commit()
            assert await counts(connection, run_id) == baseline
        finally:
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)
    async with session_factory()() as db:
        run = await db.get(Run, run_id)
        assert run.status == "cancelled"
        assert run.input_applied is (operation == "pending_writes")


async def test_graph_continues_committed_input_after_confirmed_process_death(case):
    import json
    import sys

    from partner_agent.infra.models_stage2 import Message, RunAttempt
    from partner_agent.runs.service import enqueue_dispatch, lock_run
    from partner_agent.runtime.runner import execute_run

    owner = case.claim.ownership
    child_code = '''
import asyncio, json, sys
from uuid import UUID
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from partner_agent.runs.service import Ownership
from partner_agent.runtime.fenced import FencedSaver
from partner_agent.runtime.fixture import fixture_graph

print("child_imports_ready", flush=True)

async def main():
    data = json.loads(sys.stdin.readline())
    owner = Ownership(**{k: UUID(v) for k, v in data["owner"].items()}, fence=data["fence"])
    async def publish(text):
        print("prepared_checkpoint_committed", flush=True)
        await asyncio.Event().wait()
    async with await AsyncConnection.connect(
        data["url"], autocommit=True, row_factory=dict_row,
        options="-c search_path=langgraph,public"
    ) as conn:
        print("child_database_ready", flush=True)
        graph = fixture_graph(FencedSaver(conn, owner, UUID(data["message_id"])), publish)
        await graph.ainvoke(data["input"], {
            "configurable": {"thread_id": str(owner.run_id), "checkpoint_ns": ""},
            "recursion_limit": 12,
        }, durability="sync")
asyncio.run(main())
'''
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", child_code,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        payload = {
            "url": case.url,
            "owner": {name: str(getattr(owner, name)) for name in (
                "run_id", "chat_id", "branch_id", "owner_token"
            )},
            "fence": owner.fence,
            "message_id": str(case.claim.input_message_id),
            "input": case.claim.graph_input,
        }
        process.stdin.write((json.dumps(payload) + "\n").encode())
        await process.stdin.drain()
        process.stdin.close()
        assert await asyncio.wait_for(process.stdout.readline(), timeout=30) == (
            b"child_imports_ready\n"
        )
        assert await asyncio.wait_for(process.stdout.readline(), timeout=10) == (
            b"child_database_ready\n"
        )
        assert await asyncio.wait_for(process.stdout.readline(), timeout=10) == (
            b"prepared_checkpoint_committed\n"
        )
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=5)
        assert process.returncode < 0
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)

    # Test-only operator recovery, permitted only after confirmed old-process death.
    # Production automatic takeover remains disabled.
    async with session_factory()() as db:
        _, run = await lock_run(db, owner.run_id)
        assert run.input_applied and run.input_checkpoint_id
        original_checkpoint = run.input_checkpoint_id
        attempt = await db.scalar(select(RunAttempt).where(RunAttempt.run_id == run.id))
        attempt.status = "lease_expired"
        attempt.ended_at = await db.scalar(select(func.clock_timestamp()))
        run.status = "queued"
        run.owner_token = None
        run.lease_expires_at = None
        run.running_since = None
        run.fence += 1
        run.dispatch_generation += 1
        await enqueue_dispatch(db, run)
        await db.commit()
    result = await execute_run(owner.run_id, dispatch_generation=1)
    assert result["status"] == "succeeded"
    assert (await execute_run(owner.run_id, dispatch_generation=1))["status"] == (
        "duplicate_or_ineligible"
    )
    async with session_factory()() as db:
        run = await db.get(Run, owner.run_id)
        assert run.input_checkpoint_id == original_checkpoint
        messages = (await db.scalars(select(Message).where(Message.run_id == run.id))).all()
        assert len([m for m in messages if m.role == "user"]) == 1
        answers = [m for m in messages if m.role == "assistant"]
        assert len(answers) == 1 and "Native fixture" in answers[0].content
        assert "Ходов в контексте ветки: 1." in answers[0].content
        assert await db.scalar(select(func.count()).select_from(RunAttempt).where(
            RunAttempt.run_id == run.id
        )) == 2
