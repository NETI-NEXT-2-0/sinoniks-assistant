"""V3 data plane on real PostgreSQL; fixture generation is never a model-quality claim."""

import asyncio
import os
import uuid
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from partner_agent.auth.security import password_hasher
from partner_agent.chats.routes import event_stream
from partner_agent.infra.db import get_engine, session_factory
from partner_agent.infra.models import AuditEvent, Session, User, utcnow
from partner_agent.infra.models_stage2 import (
    Admission,
    Chat,
    ChatBranch,
    Message,
    Outbox,
    ResumeCommand,
    Run,
    RunEvent,
)
from partner_agent.infra.settings import get_settings
from partner_agent.main import app
from partner_agent.runs.service import (
    Ownership,
    StaleOwnership,
    admit_run,
    build_context,
    finish_run,
    lock_chat,
    lock_run,
    publish_prefix,
    server_now,
    visible_messages,
)

pytestmark = pytest.mark.integration
PASSWORD = "synthetic-chat-password-123"


@pytest_asyncio.fixture
async def plane(monkeypatch):
    url = os.environ.get("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set PARTNER_TEST_DATABASE_URL to a migrated disposable PostgreSQL database")
    monkeypatch.setenv("PARTNER_DATABASE_URL", url)
    monkeypatch.setenv("PARTNER_ENVIRONMENT", "test")
    monkeypatch.setenv("PARTNER_PUBLIC_ORIGIN", "http://localhost:8080")
    monkeypatch.setenv("PARTNER_API_REQUESTS_PER_MINUTE", "10000")
    get_settings.cache_clear()
    get_engine.cache_clear()
    ids = []
    clients = []

    async def new_user(role="user"):
        user_id = uuid.uuid4()
        login = "chat-" + uuid.uuid4().hex
        async with session_factory()() as db:
            from sqlalchemy import text

            assert (await db.scalar(text("select current_database()"))).endswith("_test")
            db.add(
                User(
                    id=user_id,
                    normalized_login=login,
                    password_hash=password_hasher.hash(PASSWORD),
                    role=role,
                    disabled=False,
                )
            )
            await db.commit()
        ids.append(user_id)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080"
        )
        clients.append(client)
        csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        result = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://localhost:8080", "X-CSRF-Token": csrf},
            json={"login": login, "password": PASSWORD},
        )
        assert result.status_code == 200, result.text
        headers = {"Origin": "http://localhost:8080", "X-CSRF-Token": result.json()["csrf_token"]}
        return client, headers, user_id

    yield new_user
    for client in clients:
        await client.aclose()
    async with session_factory()() as db:
        await db.execute(delete(AuditEvent).where(AuditEvent.actor_id.in_(ids)))
        await db.execute(delete(Session).where(Session.user_id.in_(ids)))
        await db.execute(delete(User).where(User.id.in_(ids)))
        await db.commit()
    await get_engine().dispose()
    get_engine.cache_clear()
    get_settings.cache_clear()


async def new_chat(client, headers):
    response = await client.post("/api/v1/chats", headers=headers, json={"title": "Проверка"})
    assert response.status_code == 201, response.text
    return response.json()


async def submit(client, headers, chat, text="Первый вопрос", key=None, branch=None):
    return await client.post(
        f"/api/v1/chats/{chat['id']}/runs",
        headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())},
        json={
            "branch_id": branch or chat["default_branch_id"],
            "message": text,
            "attachment_ids": [],
            "web_mode": "off",
        },
    )


async def own_run(db, run_id):
    chat, run = await lock_run(db, uuid.UUID(str(run_id)))
    token = uuid.uuid4()
    run.status, run.owner_token = "running", token
    run.fence += 1
    run.lease_expires_at = await server_now(db) + timedelta(seconds=30)
    run.running_since = await server_now(db)
    return chat, run, Ownership(run.id, chat.id, run.branch_id, token, run.fence)


async def complete(run_id, answer="Фикстура, без LLM"):
    async with session_factory()() as db:
        chat, run, owner = await own_run(db, run_id)
        await build_context(db, chat, run)
        await publish_prefix(db, chat, run, owner, answer)
        await finish_run(db, chat, run, "succeeded", {"kind": "fixture"}, ownership=owner)
        await db.commit()
        return run.assistant_message_id


async def test_http_owner_acl_csrf_revision_and_tombstone(plane):
    client, headers, _ = await plane()
    outsider, outsider_headers, _ = await plane("admin")
    chat = await new_chat(client, headers)
    assert (await outsider.get(f"/api/v1/chats/{chat['id']}")).status_code == 404
    assert (await outsider.get(f"/api/v1/chats/{chat['id']}/snapshot")).status_code == 404
    assert (
        await outsider.patch(
            f"/api/v1/chats/{chat['id']}",
            headers=outsider_headers,
            json={"title": "steal", "expected_revision": 1},
        )
    ).status_code == 404
    assert (
        await client.patch(
            f"/api/v1/chats/{chat['id']}", json={"title": "X", "expected_revision": 1}
        )
    ).status_code == 403
    result = await client.patch(
        f"/api/v1/chats/{chat['id']}",
        headers=headers,
        json={"title": "Новое имя", "expected_revision": 1},
    )
    assert result.status_code == 200 and result.json()["revision"] == 2
    assert (
        await client.patch(
            f"/api/v1/chats/{chat['id']}",
            headers=headers,
            json={"title": "stale", "expected_revision": 1},
        )
    ).status_code == 409
    admitted = (await submit(client, headers, chat)).json()
    assert (await outsider.get(f"/api/v1/runs/{admitted['run_id']}")).status_code == 404
    assert (await client.delete(f"/api/v1/chats/{chat['id']}", headers=headers)).status_code == 204
    assert (await client.get(f"/api/v1/chats/{chat['id']}/messages")).status_code == 404
    assert (await submit(client, headers, chat)).status_code == 404
    async with session_factory()() as db:
        run = await db.get(Run, uuid.UUID(admitted["run_id"]))
        assert run.status == "cancelled"
        assert (
            await db.scalar(
                select(func.count()).select_from(Message).where(Message.run_id == run.id)
            )
            == 1
        )


async def test_admission_duplicate_conflict_fifo_and_only_head_outbox(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    # Different live sessions avoid depending on the per-session lock to serialize commands.
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    headers["X-CSRF-Token"] = csrf
    response = await submit(client, headers, chat, key="stable")
    assert response.status_code == 202, response.text
    original = response.json()
    assert (await submit(client, headers, chat, key="stable")).json() == original
    changed = await submit(client, headers, chat, text="Другое тело", key="stable")
    assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
    second = (await submit(client, headers, chat, text="Второй вопрос")).json()
    assert second["queue_position"] == 1
    async with session_factory()() as db:
        runs = (
            await db.scalars(
                select(Run).where(Run.chat_id == uuid.UUID(chat["id"])).order_by(Run.queue_seq)
            )
        ).all()
        assert [r.queue_seq for r in runs] == [1, 2]
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Outbox)
                .where(Outbox.run_id.in_([r.id for r in runs]))
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Admission)
                .where(Admission.chat_id == uuid.UUID(chat["id"]))
            )
            == 2
        )
    await complete(original["run_id"])
    async with session_factory()() as db:
        actual = await db.get(Chat, uuid.UUID(chat["id"]))
        assert str(actual.active_run_id) == second["run_id"]
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Outbox)
                .where(Outbox.run_id == uuid.UUID(second["run_id"]))
            )
            == 1
        )


async def test_concurrent_same_key_accepts_once_and_rollback_leaves_nothing(plane):
    client, headers, user_id = await plane()
    chat = await new_chat(client, headers)
    # Independent HTTP clients share user but use different sessions, exercising the chat lock.
    async with session_factory()() as db:
        current = await db.get(User, user_id)
        login = current.normalized_login
    other = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080"
    )
    try:
        csrf = (await other.get("/api/v1/auth/csrf")).json()["csrf_token"]
        auth = await other.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://localhost:8080", "X-CSRF-Token": csrf},
            json={"login": login, "password": PASSWORD},
        )
        other_headers = {
            "Origin": "http://localhost:8080",
            "X-CSRF-Token": auth.json()["csrf_token"],
        }
        first, second = await asyncio.gather(
            submit(client, headers, chat, key="race"),
            submit(other, other_headers, chat, key="race"),
        )
        assert first.status_code == second.status_code == 202
        assert first.json() == second.json()
    finally:
        await other.aclose()
    async with session_factory()() as db:
        user = await db.get(User, user_id)
        locked = await lock_chat(db, uuid.UUID(chat["id"]), user)
        branch = await db.get(ChatBranch, locked.default_branch_id)
        rolled = await admit_run(db, locked, branch, user, "Должно откатиться")
        rolled_id = rolled.id
        await db.rollback()
    async with session_factory()() as db:
        assert await db.get(Run, rolled_id) is None
        assert (
            await db.scalar(
                select(func.count()).select_from(Message).where(Message.run_id == rolled_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count()).select_from(RunEvent).where(RunEvent.run_id == rolled_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count()).select_from(Outbox).where(Outbox.run_id == rolled_id)
            )
            == 0
        )
        locked = await db.get(Chat, uuid.UUID(chat["id"]))
        assert locked.queue_seq == 1


async def test_prefix_idempotence_fence_and_cancel_wins(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    admission = (await submit(client, headers, chat)).json()
    async with session_factory()() as db:
        locked, run, owner = await own_run(db, admission["run_id"])
        await publish_prefix(db, locked, run, owner, "А😀")
        await publish_prefix(db, locked, run, owner, "А😀")
        await publish_prefix(db, locked, run, owner, "А")
        await db.commit()
    response = await client.post(f"/api/v1/runs/{admission['run_id']}/cancel", headers=headers)
    assert response.status_code == 200 and response.json()["partial_text"] == "А😀"
    async with session_factory()() as db:
        locked, run = await lock_run(db, uuid.UUID(admission["run_id"]))
        with pytest.raises(StaleOwnership):
            await publish_prefix(db, locked, run, owner, "А😀late")
        await db.rollback()
    async with session_factory()() as db:
        locked, run = await lock_run(db, uuid.UUID(admission["run_id"]))
        await finish_run(db, locked, run, "succeeded", {"kind": "late"}, ownership=owner)
        await db.commit()
        assert run.status == "cancelled" and run.assistant_message_id is None
        deltas = (
            await db.scalars(
                select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "message.delta")
            )
        ).all()
        assert len(deltas) == 1 and deltas[0].payload["offset"] == 0


async def test_complete_wins_and_database_final_uniqueness(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    admission = (await submit(client, headers, chat)).json()
    await complete(admission["run_id"])
    response = await client.post(f"/api/v1/runs/{admission['run_id']}/cancel", headers=headers)
    assert response.json()["status"] == "succeeded"
    async with session_factory()() as db:
        run = await db.get(Run, uuid.UUID(admission["run_id"]))
        db.add(
            Message(
                id=uuid.uuid4(),
                chat_id=run.chat_id,
                branch_id=run.branch_id,
                run_id=run.id,
                role="assistant",
                status="completed",
                origin="test",
                content="duplicate",
                ordinal=999,
                revision=1,
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


async def test_context_frontier_branches_and_queued_input_isolation(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    first = (await submit(client, headers, chat, text="Исходный вопрос")).json()
    second = (await submit(client, headers, chat, text="Более поздний queued вопрос")).json()
    async with session_factory()() as db:
        locked, run, owner = await own_run(db, first["run_id"])
        context = await build_context(db, locked, run)
        assert [m.content for m in context] == ["Исходный вопрос"]
        await db.rollback()
    original_answer = await complete(first["run_id"], "Исходный ответ")
    fork = await client.post(
        f"/api/v1/chats/{chat['id']}/branches",
        headers={**headers, "Idempotency-Key": "edit"},
        json={
            "parent_branch_id": chat["default_branch_id"],
            "message_id": first["message_id"],
            "mode": "edit",
            "revised_text": "Новый вопрос",
        },
    )
    assert fork.status_code == 201, fork.text
    branch_id = fork.json()["branch_id"]
    async with session_factory()() as db:
        branch = await db.get(ChatBranch, uuid.UUID(branch_id))
        assert [m.content for m in await visible_messages(db, branch)] == ["Новый вопрос"]
        original = await db.get(Message, uuid.UUID(first["message_id"]))
        assert original.content == "Исходный вопрос"
        assert await db.get(Message, original_answer) is not None
        locked, run, owner = await own_run(db, second["run_id"])
        context = await build_context(db, locked, run)
        assert [m.content for m in context] == [
            "Исходный вопрос",
            "Исходный ответ",
            "Более поздний queued вопрос",
        ]
        await db.commit()
    snapshot = (
        await client.get(f"/api/v1/chats/{chat['id']}/snapshot?branch_id={branch_id}")
    ).json()
    assert snapshot["selected_branch_id"] == branch_id
    assert [m["content"] for m in snapshot["messages"]] == ["Новый вопрос"]
    assert snapshot["cursor"] >= 3


async def test_waiting_input_resumes_same_head_idempotently(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    first = (await submit(client, headers, chat)).json()
    second = (await submit(client, headers, chat, text="Ждёт уточнения")).json()
    clarification = uuid.uuid4()
    async with session_factory()() as db:
        locked, run = await lock_run(db, uuid.UUID(first["run_id"]))
        run.status = "waiting_input"
        run.clarification_id = clarification
        run.clarification_question = "Вариант?"
        run.clarification_options = ["А", "Б"]
        run.waiting_since = utcnow()
        await db.commit()
    body = {"clarification_id": str(clarification), "answer": "А"}
    response = await client.post(
        f"/api/v1/runs/{first['run_id']}/resume",
        headers={**headers, "Idempotency-Key": "resume"},
        json=body,
    )
    assert response.status_code == 202, response.text
    duplicate = await client.post(
        f"/api/v1/runs/{first['run_id']}/resume",
        headers={**headers, "Idempotency-Key": "resume"},
        json=body,
    )
    assert duplicate.json() == response.json()
    async with session_factory()() as db:
        run = await db.get(Run, uuid.UUID(first["run_id"]))
        locked = await db.get(Chat, run.chat_id)
        command = await db.get(ResumeCommand, run.pending_resume_command_id)
        assert locked.active_run_id == run.id and run.status == "queued"
        assert command.input_applied is False
        assert run.dispatch_generation == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Outbox)
                .where(Outbox.run_id == uuid.UUID(second["run_id"]))
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ResumeCommand)
                .where(ResumeCommand.run_id == run.id)
            )
            == 1
        )


class Connected:
    async def is_disconnected(self):
        return False


async def test_sse_replay_auth_revoke_and_expired_cursor_reset(plane):
    client, headers, user_id = await plane()
    chat = await new_chat(client, headers)
    admission = (await submit(client, headers, chat)).json()
    await complete(admission["run_id"])
    async with session_factory()() as db:
        session = await db.scalar(
            select(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
        )
        sid = session.id
        state = await db.get(Chat, uuid.UUID(chat["id"]))
        cursor = state.event_seq
    stream = event_stream(Connected(), uuid.UUID(chat["id"]), sid, user_id, 0)
    first = await anext(stream)
    assert "id: 1\n" in first and "event: run.queued" in first
    async with session_factory()() as db:
        await db.execute(update(Session).where(Session.id == sid).values(revoked_at=utcnow()))
        await db.commit()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await stream.aclose()
    # A fresh stream cannot leak the buffered remainder after revocation.
    fresh = event_stream(Connected(), uuid.UUID(chat["id"]), sid, user_id, 0)
    with pytest.raises(StopAsyncIteration):
        await anext(fresh)
    await fresh.aclose()
    async with session_factory()() as db:
        await db.execute(update(Session).where(Session.id == sid).values(revoked_at=None))
        await db.execute(
            update(Chat).where(Chat.id == uuid.UUID(chat["id"])).values(event_floor_seq=cursor)
        )
        await db.commit()
    reset = event_stream(Connected(), uuid.UUID(chat["id"]), sid, user_id, 0)
    event = await anext(reset)
    assert "event: stream.reset" in event and f'"cursor": {cursor}' in event
    with pytest.raises(StopAsyncIteration):
        await anext(reset)
    await reset.aclose()
    invalid = await client.get(
        f"/api/v1/chats/{chat['id']}/events", headers={"Last-Event-ID": "not-an-integer"}
    )
    assert invalid.status_code == 422


async def test_byte_limit_and_unsupported_features_are_explicit(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    assert (await submit(client, headers, chat, text="я" * 16385)).status_code == 422
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/runs",
        headers={**headers, "Idempotency-Key": "web"},
        json={"branch_id": chat["default_branch_id"], "message": "Вопрос", "web_mode": "public"},
    )
    assert response.status_code == 503 and response.json()["code"] == "stage_unavailable"
    assert (
        await client.post(
            f"/api/v1/chats/{chat['id']}/runs",
            headers=headers,
            json={"branch_id": chat["default_branch_id"], "message": "Вопрос"},
        )
    ).status_code == 422


@pytest.mark.parametrize("winner", ["succeeded", "cancelled"])
async def test_cancel_finalize_race_first_commit_wins_with_two_transactions(plane, winner):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    admitted = (await submit(client, headers, chat)).json()
    async with session_factory()() as db:
        _, run, owner = await own_run(db, admitted["run_id"])
        await db.commit()
    locked = asyncio.Event()
    release = asyncio.Event()
    second_entered = asyncio.Event()
    loser = "cancelled" if winner == "succeeded" else "succeeded"

    async def first():
        async with session_factory()() as db:
            current_chat, current_run = await lock_run(db, run.id)
            await finish_run(
                db,
                current_chat,
                current_run,
                winner,
                {"winner": winner},
                ownership=owner if winner == "succeeded" else None,
                text="Первый commit",
            )
            locked.set()
            await release.wait()
            await db.commit()

    async def second():
        await locked.wait()
        async with session_factory()() as db:
            second_entered.set()
            current_chat, current_run = await lock_run(db, run.id)
            await finish_run(
                db,
                current_chat,
                current_run,
                loser,
                {"winner": loser},
                ownership=owner if loser == "succeeded" else None,
                text="Поздний commit",
            )
            await db.commit()
            return current_run.status

    first_task = asyncio.create_task(first())
    await asyncio.wait_for(locked.wait(), timeout=5)
    second_task = asyncio.create_task(second())
    try:
        await asyncio.wait_for(second_entered.wait(), timeout=5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second_task), timeout=0.05)
    finally:
        release.set()
    _, second_result = await asyncio.gather(first_task, second_task)
    assert second_result == winner
    async with session_factory()() as db:
        result = await db.get(Run, run.id)
        assert result.status == winner and result.outcome == {"winner": winner}
        finals = (
            await db.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == run.id, RunEvent.type.in_(["run.succeeded", "run.cancelled"])
                )
            )
        ).all()
        assert len(finals) == 1
        messages = (
            await db.scalars(
                select(Message).where(Message.run_id == run.id, Message.role == "assistant")
            )
        ).all()
        assert len(messages) == (1 if winner == "succeeded" else 0)


async def test_sse_last_event_id_priority_and_no_request_transaction_at_yield(plane):
    from partner_agent.chats.routes import events

    client, headers, user_id = await plane()
    chat = await new_chat(client, headers)
    admitted = (await submit(client, headers, chat)).json()
    await complete(admitted["run_id"])
    request = Connected()
    request.cookies = {"partner-dev-session": client.cookies.get("partner-dev-session")}
    async with session_factory()() as db:
        response = await events(uuid.UUID(chat["id"]), request, db, after=0, last_event_id="2")
        assert db.in_transaction() is False
        item = await anext(response.body_iterator)
        assert "id: 3\n" in item and "event: message.completed" in item
        assert db.in_transaction() is False
        # Neither the request authorization transaction nor poll sessions survive a yield.
        assert get_engine().pool.checkedout() == 0
        await response.body_iterator.aclose()


async def test_snapshot_and_message_cursor_pages_preserve_order_and_ancestry(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    # One transaction creates 17 completed fixture turns: 34 immutable UI messages.
    async with session_factory()() as db:
        current = await lock_chat(db, uuid.UUID(chat["id"]))
        user = await db.get(User, current.owner_id)
        branch = await db.get(ChatBranch, current.default_branch_id)
        for index in range(17):
            run = await admit_run(db, current, branch, user, f"Вопрос {index}")
            await db.flush()
            _, run, owner = await own_run(db, run.id)
            await build_context(db, current, run)
            await finish_run(
                db,
                current,
                run,
                "succeeded",
                {"kind": "fixture"},
                ownership=owner,
                text=f"Ответ {index}",
            )
        await db.commit()
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    assert len(snapshot["messages"]) == 30
    assert snapshot["messages_next_cursor"] is not None
    previous = await client.get(
        f"/api/v1/chats/{chat['id']}/messages",
        params={"branch_id": chat["default_branch_id"], "before": snapshot["messages_next_cursor"]},
    )
    assert previous.status_code == 200
    assert previous.json()["next_cursor"] is None
    combined = previous.json()["items"] + snapshot["messages"]
    assert len({message["id"] for message in combined}) == 34
    assert [message["content"] for message in combined] == [
        content for index in range(17) for content in (f"Вопрос {index}", f"Ответ {index}")
    ]
    first_page = await client.get("/api/v1/chats", params={"limit": 1})
    assert first_page.status_code == 200 and first_page.json()["items"][0]["id"] == chat["id"]


async def test_unknown_scheduler_acceptance_retries_same_key_and_claims_once(plane):
    from partner_agent.runtime.lifecycle import claim_run
    from partner_agent.runtime.relay import deliver_one

    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    admitted = (await submit(client, headers, chat)).json()
    run_id = uuid.UUID(admitted["run_id"])

    class UnknownAcceptance:
        def __init__(self):
            self.payloads = []

        async def dispatch(self, payload):
            # The scheduler accepted the job, but the response was lost.
            # A separate PG transaction must already see the committed claim.
            async with session_factory()() as db:
                event = await db.scalar(select(Outbox).where(Outbox.run_id == run_id))
                assert event.status == "claimed" and event.claim_token is not None
            self.payloads.append(payload)
            if len(self.payloads) == 1:
                raise TimeoutError("Synthetic lost acknowledgement")
            return "synthetic-scheduler-id"

    scheduler = UnknownAcceptance()
    assert await deliver_one(scheduler, run_id)
    async with session_factory()() as db:
        event = await db.scalar(select(Outbox).where(Outbox.run_id == run_id))
        assert event.status == "pending"
        assert event.last_error_code == "scheduler_acceptance_unknown"
        assert event.claim_token is None and event.lease_expires_at is None
        event.next_attempt_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    assert await deliver_one(scheduler, run_id)
    assert scheduler.payloads[0] == scheduler.payloads[1]
    assert scheduler.payloads[0].dispatch_key == f"{run_id}:0"
    claims = await asyncio.gather(claim_run(run_id, 0), claim_run(run_id, 0))
    assert sum(claim is not None for claim in claims) == 1
    async with session_factory()() as db:
        from partner_agent.infra.models_stage2 import RunAttempt

        event = await db.scalar(select(Outbox).where(Outbox.run_id == run_id))
        assert event.status == "dispatched" and event.attempts == 2
        assert event.scheduler_id == "synthetic-scheduler-id"
        assert (
            await db.scalar(
                select(func.count()).select_from(RunAttempt).where(RunAttempt.run_id == run_id)
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.type == "run.started")
            )
            == 1
        )


async def test_worker_confirmation_survives_lost_scheduler_acknowledgement(plane):
    from partner_agent.runtime.lifecycle import claim_run
    from partner_agent.runtime.relay import deliver_one

    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    admitted = (await submit(client, headers, chat)).json()
    run_id = uuid.UUID(admitted["run_id"])

    class WorkerConfirmed:
        async def dispatch(self, payload):
            claim = await claim_run(run_id, payload.dispatch_generation, "worker-confirmed-id")
            assert claim is not None
            raise TimeoutError("Synthetic acknowledgement loss after worker startup")

    assert await deliver_one(WorkerConfirmed(), run_id)
    assert not await deliver_one(WorkerConfirmed(), run_id)
    async with session_factory()() as db:
        event = await db.scalar(select(Outbox).where(Outbox.run_id == run_id))
        run = await db.get(Run, run_id)
        assert event.status == "dispatched" and event.attempts == 1
        assert event.scheduler_id == "worker-confirmed-id"
        assert event.last_error_code is None and event.claim_token is None
        assert run.status == "running"


async def test_expired_lease_requires_manual_recovery_without_takeover(plane, monkeypatch):
    from partner_agent.runtime.lifecycle import claim_run, heartbeat
    from partner_agent.runtime.reconciler import reconcile_once

    monkeypatch.setenv("PARTNER_RUNTIME_AUTOMATIC_TAKEOVER", "false")
    get_settings.cache_clear()
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    admitted = (await submit(client, headers, chat)).json()
    run_id = uuid.UUID(admitted["run_id"])
    claim = await claim_run(run_id, 0)
    assert claim is not None
    async with session_factory()() as db:
        run = await db.get(Run, run_id)
        run.lease_expires_at = utcnow() - timedelta(seconds=1)
        fence = run.fence
        await db.commit()
    await reconcile_once()
    await reconcile_once()
    with pytest.raises(StaleOwnership):
        await heartbeat(claim.ownership)
    assert await claim_run(run_id, 0) is None
    async with session_factory()() as db:
        run = await db.get(Run, run_id)
        current = await db.get(Chat, run.chat_id)
        assert run.status == "running" and run.fence == fence
        assert run.outcome["recovery_required"] is True
        assert current.active_run_id == run.id
        events = (
            await db.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == "run.progress")
            )
        ).all()
        assert len(events) == 1
        assert events[0].payload["code"] == "manual_recovery_required"
        assert (
            await db.scalar(select(func.count()).select_from(Outbox).where(Outbox.run_id == run_id))
            == 1
        )


async def test_clarification_timeout_cancels_head_and_dispatches_next(plane):
    from partner_agent.runtime.reconciler import reconcile_once

    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    first = (await submit(client, headers, chat)).json()
    second = (await submit(client, headers, chat, text="Следующий ход")).json()
    async with session_factory()() as db:
        run = await db.get(Run, uuid.UUID(first["run_id"]))
        run.status = "waiting_input"
        run.waiting_since = utcnow() - timedelta(hours=25)
        await db.commit()
    await reconcile_once()
    await reconcile_once()
    async with session_factory()() as db:
        run = await db.get(Run, uuid.UUID(first["run_id"]))
        current = await db.get(Chat, run.chat_id)
        assert run.status == "cancelled" and run.outcome["code"] == "input_timeout"
        assert str(current.active_run_id) == second["run_id"]
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Outbox)
                .where(Outbox.run_id == uuid.UUID(second["run_id"]))
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(RunEvent.run_id == run.id, RunEvent.type == "run.cancelled")
            )
            == 1
        )


async def test_reconciler_backlog_does_not_hide_other_chat_heads(plane):
    from partner_agent.runtime.reconciler import reconcile_once

    client, headers, user_id = await plane()
    busy = await new_chat(client, headers)
    async with session_factory()() as db:
        current = await lock_chat(db, uuid.UUID(busy["id"]))
        user = await db.get(User, user_id)
        branch = await db.get(ChatBranch, current.default_branch_id)
        for index in range(101):
            await admit_run(db, current, branch, user, f"Queued backlog {index}")
        await db.commit()
    other = await new_chat(client, headers)
    admitted = (await submit(client, headers, other)).json()
    async with session_factory()() as db:
        run = await db.get(Run, uuid.UUID(admitted["run_id"]))
        run.status = "waiting_input"
        run.waiting_since = utcnow() - timedelta(hours=25)
        await db.commit()
    await reconcile_once()
    async with session_factory()() as db:
        run = await db.get(Run, uuid.UUID(admitted["run_id"]))
        assert run.status == "cancelled" and run.outcome["code"] == "input_timeout"


async def test_native_runner_clarification_http_resume_and_fifo(plane, monkeypatch):
    from partner_agent.runtime.runner import execute_run

    monkeypatch.setenv("PARTNER_RUNTIME_FIXTURE_DELAY_SECONDS", "0")
    get_settings.cache_clear()
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    first = (await submit(client, headers, chat, text="/fixture clarify end-to-end")).json()
    second = (await submit(client, headers, chat, text="Следующий вопрос")).json()
    first_id = uuid.UUID(first["run_id"])
    result = await execute_run(first_id, dispatch_generation=0)
    assert result["status"] == "waiting_input"
    state = (await client.get(f"/api/v1/runs/{first_id}")).json()
    async with session_factory()() as db:
        run = await db.get(Run, first_id)
        clarification = str(run.clarification_id)
        assert run.input_applied and run.status == "waiting_input"
    response = await client.post(
        f"/api/v1/runs/{first_id}/resume",
        headers={**headers, "Idempotency-Key": "native-resume"},
        json={"clarification_id": clarification, "answer": "А"},
    )
    assert response.status_code == 202, response.text
    assert state["status"] == "waiting_input"
    assert (await execute_run(first_id, dispatch_generation=1))["status"] == "succeeded"
    async with session_factory()() as db:
        run = await db.get(Run, first_id)
        command = await db.get(ResumeCommand, run.pending_resume_command_id)
        assert command.input_applied and command.checkpoint_id
        final = await db.get(Message, run.assistant_message_id)
        assert "Уточнение: А." in final.content
        current = await db.get(Chat, run.chat_id)
        assert str(current.active_run_id) == second["run_id"]
    assert (await execute_run(uuid.UUID(second["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"


async def test_knowledge_http_curator_ownership_csrf_and_no_storage_key_leak(plane):
    from partner_agent.infra.models_documents import Document, DocumentVersion

    curator, headers, _ = await plane(role="curator")
    admin, _, _ = await plane(role="admin")
    document_id = version_id = None
    try:
        assert (
            await curator.post("/api/v1/knowledge/documents", json={"title": "Курс"})
        ).status_code == 403
        response = await curator.post(
            "/api/v1/knowledge/documents", headers=headers, json={"title": "Программа курса"}
        )
        assert response.status_code == 201, response.text
        document_id = uuid.UUID(response.json()["id"])
        listing = await curator.get("/api/v1/knowledge/documents", params={"limit": 1})
        assert listing.status_code == 200
        assert listing.json()["items"][0]["id"] == str(document_id)
        assert listing.json()["next_cursor"] is None
        assert (await admin.get("/api/v1/knowledge/documents")).json()["items"] == []
        assert (
            await curator.get("/api/v1/knowledge/documents", params={"limit": 101})
        ).status_code == 422

        assert (await admin.get(f"/api/v1/knowledge/documents/{document_id}")).status_code == 404
        async with session_factory()() as db:
            version_id = uuid.uuid4()
            db.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    generation=1,
                    source_sha256="a" * 64,
                    media_type="text/plain",
                    size_bytes=10,
                    original_key=f"{document_id}/secret-key",
                    pipeline_version="canonical-v1",
                )
            )
            await db.commit()
        version = await curator.get(f"/api/v1/knowledge/versions/{version_id}")
        assert version.status_code == 200 and version.json()["status"] == "pending"
        assert "secret-key" not in version.text and "source_sha256" not in version.text
        assert (await admin.get(f"/api/v1/knowledge/versions/{version_id}")).status_code == 404
        publish = await curator.post(
            f"/api/v1/knowledge/versions/{version_id}/publish",
            headers=headers,
            json={"expected_revision": 1},
        )
        assert publish.status_code == 409 and publish.json()["code"] == "publication_not_ready"
    finally:
        async with session_factory()() as db:
            if version_id:
                await db.execute(delete(DocumentVersion).where(DocumentVersion.id == version_id))
            if document_id:
                await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


@pytest.mark.storage
async def test_text_upload_stores_exact_bytes_in_real_garage(plane):
    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents.ingest import storage
    from partner_agent.documents.service import publish_version, unpublish
    from partner_agent.documents.worker import index_once, ingest_once
    from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion
    from partner_agent.retrieval.index import FixtureIndex

    client, headers, _ = await plane(role="curator")
    response = await client.post(
        "/api/v1/knowledge/documents", headers=headers, json={"title": "Учебные материалы"}
    )
    document_id = uuid.UUID(response.json()["id"])
    version_id = None
    key = canonical_key = None
    source = "Программа обучения\nМодуль Python".encode()
    try:
        result = await client.post(
            f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=1",
            headers={**headers, "Content-Type": "text/plain; charset=utf-8"},
            content=source,
        )
        assert result.status_code == 202, result.text
        version_id = uuid.UUID(result.json()["id"])
        download_url = f"/api/v1/knowledge/versions/{version_id}/original/download"
        assert (await client.post(download_url)).status_code == 403
        original = await client.post(download_url, headers=headers)
        assert original.status_code == 200 and original.content == source
        assert original.headers["content-disposition"] == 'attachment; filename="document.txt"'
        assert original.headers["cache-control"] == "private, no-store"
        versions = await client.get(f"/api/v1/knowledge/documents/{document_id}/versions")
        assert versions.status_code == 200 and versions.json()["items"][0]["id"] == str(version_id)
        assert "original_key" not in versions.text and "source_sha256" not in versions.text
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            key = version.original_key
            assert version.status == "pending" and version.size_bytes == len(source)

        def read_back():
            adapter = storage()
            try:
                assert adapter.get(adapter.originals_bucket, key).data == source
            finally:
                adapter.client.close()

        await run_in_threadpool(read_back)
        processed = await asyncio.gather(ingest_once(), ingest_once())
        assert sum(processed) >= 1
        assert await ingest_once() == 0
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            canonical_key = version.canonical_key
            assert canonical_key.endswith(".json")
            assert version.status == "pending" and version.index_manifest is None
            await db.commit()
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(DisclosureUnit)
                    .where(DisclosureUnit.version_id == version_id)
                )
                == 0
            )

        review = await client.get(f"/api/v1/knowledge/versions/{version_id}/review")
        assert review.status_code == 200
        blocks = review.json()["blocks"]
        assert len(blocks) == 2
        assert all(not block["mixed_script_identifier"] for block in blocks)
        approved = await client.post(
            f"/api/v1/knowledge/versions/{version_id}/approve", headers=headers,
            json={"expected_revision": 2, "canonical_sha256": review.json()["canonical_sha256"],
                  "block_ids": [blocks[1]["id"]]},
        )
        assert approved.status_code == 202

        async with session_factory()() as db:
            doc = await db.get(Document, document_id)
            actor = await db.get(User, doc.owner_id)
            await db.commit()
            assert await index_once() >= 1
            assert await index_once() == 0
            version = await db.get(DocumentVersion, version_id, populate_existing=True)
            assert version.status == "ready_for_review"
            assert version.index_manifest["profile"] == "offline_lexical_fixture"
            assert doc.active_version_id is None
            from partner_agent.retrieval.service import search_knowledge

            assert await search_knowledge(db, actor, "Python") == []
            await publish_version(db, actor, document_id, version_id, doc.revision)
            await db.commit()
            assert doc.active_version_id == version_id
            hits = await search_knowledge(db, actor, "Python")
            assert len(hits) == 1 and hits[0]["excerpt"] == "Модуль Python"
            assert await search_knowledge(db, actor, "обучения") == []
            # Release read authorization locks before the independent HTTP request.
            await db.commit()
            http_search = await client.post(
                "/api/v1/knowledge/search", headers=headers, json={"query": "Python"}
            )
            assert http_search.status_code == 200 and len(http_search.json()["items"]) == 1
            await unpublish(db, actor, document_id, doc.revision)
            await db.commit()
            assert doc.active_version_id is None
            assert await search_knowledge(db, actor, "Python") == []
    finally:
        if version_id:
            await FixtureIndex(get_settings().qdrant_url).delete_version(version_id)
        if key:

            def remove():
                adapter = storage()
                try:
                    adapter.delete(adapter.originals_bucket, key)
                    if canonical_key:
                        adapter.delete(adapter.derived_bucket, canonical_key)
                finally:
                    adapter.client.close()

            await run_in_threadpool(remove)
        async with session_factory()() as db:
            document = await db.get(Document, document_id)
            if document:
                document.last_published_version_id = None
                await db.flush()
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


@pytest.mark.storage
async def test_http_docling_approval_quality_acl_and_retry(plane):
    from hashlib import sha256

    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents.docling_adapter import normalize_docling
    from partner_agent.documents.ingest import storage
    from partner_agent.documents.worker import index_once
    from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion

    client, headers, _ = await plane(role="curator")
    other, other_headers, _ = await plane(role="admin")
    created = await client.post(
        "/api/v1/knowledge/documents", headers=headers, json={"title": "Материалы курса"}
    )
    document_id, version_id = uuid.UUID(created.json()["id"]), uuid.uuid4()
    key = f"{document_id}/{version_id}/canonical.json"
    canonical = normalize_docling(
        {
            "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
            "texts": [
                {"self_ref": "#/texts/0", "text": "Программа Python"},
                {"self_ref": "#/texts/1", "text": "Закрытая информация"},
            ],
        },
        document_id,
        version_id,
        "a" * 64,
        "docling-2.150.0",
    )

    def put(value):
        adapter = storage()
        try:
            adapter.put(
                adapter.derived_bucket, key, value.model_dump_json().encode(), "application/json"
            )
        finally:
            adapter.client.close()

    async def install(value):
        data = value.model_dump_json().encode()
        await run_in_threadpool(put, value)
        digest = sha256(data).hexdigest()
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            version.canonical_sha256 = digest
            await db.commit()
        return digest

    try:
        async with session_factory()() as db:
            db.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    generation=1,
                    source_sha256="a" * 64,
                    media_type="application/pdf",
                    size_bytes=10,
                    original_key=f"{document_id}/{version_id}/original",
                    canonical_key=key,
                    pipeline_version="docling-adapter-v1",
                )
            )
            await db.commit()
        endpoint = f"/api/v1/knowledge/versions/{version_id}/approve"
        body = {"expected_revision": 1, "block_ids": [str(canonical.blocks[0].id)],
                "canonical_sha256": "0" * 64}
        assert (await client.post(endpoint, json=body)).status_code == 403
        assert (await other.post(endpoint, headers=other_headers, json=body)).status_code == 404
        linked = normalize_docling(
            {
                "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}]},
                "texts": [
                    {"self_ref": "#/texts/0", "text": "Курс",
                     "footnotes": [{"$ref": "#/texts/1"}]},
                    {"self_ref": "#/texts/1", "label": "footnote", "text": "Для участников"},
                ],
            }, document_id, version_id, "a" * 64, "docling-2.150.0",
        )
        body["canonical_sha256"] = await install(linked)
        linked_review = await client.get(f"/api/v1/knowledge/versions/{version_id}/review")
        assert linked_review.status_code == 200
        assert linked_review.json()["blocks"][0]["required_context"] == [1]
        assert linked_review.json()["quality_block"] is None
        omitted = await client.post(endpoint, headers=headers, json={
            **body, "block_ids": [str(linked.blocks[0].id)]
        })
        assert omitted.status_code == 409 and omitted.json()["code"] == "canonical_review_required"
        corrupted = canonical.model_copy(
            update={
                "blocks": (
                    canonical.blocks[0].model_copy(update={"text": "Подменённый текст"}),
                    canonical.blocks[1],
                )
            }
        )
        body["canonical_sha256"] = await install(corrupted)
        invalid = await client.post(endpoint, headers=headers, json=body)
        assert invalid.status_code == 409 and invalid.json()["code"] == "canonical_artifact_invalid"
        assert "Подменённый" not in invalid.text
        body["canonical_sha256"] = await install(
            canonical.model_copy(update={"warnings": ("native_text_order_uncertain",)})
        )
        warning_review = await client.get(f"/api/v1/knowledge/versions/{version_id}/review")
        assert warning_review.json()["quality_block"] == "manual_quality_review_required"
        rejected = await client.post(endpoint, headers=headers, json=body)
        assert (
            rejected.status_code == 409 and rejected.json()["code"] == "canonical_review_required"
        )
        async with session_factory()() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(DisclosureUnit)
                    .where(DisclosureUnit.version_id == version_id)
                )
                == 0
            )
            assert (await db.get(DocumentVersion, version_id)).status == "pending"
        body["canonical_sha256"] = await install(canonical)
        await run_in_threadpool(
            put, canonical.model_copy(update={"warnings": ("native_text_order_uncertain",)})
        )
        swapped = await client.post(endpoint, headers=headers, json=body)
        assert swapped.status_code == 409 and swapped.json()["code"] == "canonical_digest_mismatch"
        await run_in_threadpool(put, canonical)
        review_url = f"/api/v1/knowledge/versions/{version_id}/review"
        assert (await other.get(review_url)).status_code == 404
        first = await client.get(review_url, params={"limit": 1})
        assert first.status_code == 200
        assert first.json()["canonical_sha256"] == body["canonical_sha256"]
        assert first.headers["cache-control"] == "private, no-store"
        assert first.json()["blocks"][0]["text"] == "Программа Python"
        assert first.json()["next_offset"] == 1
        second = await client.get(review_url, params={"offset": 1, "limit": 1})
        assert second.json()["blocks"][0]["text"] == "Закрытая информация"
        assert second.json()["next_offset"] is None
        stale = await client.post(
            endpoint, headers=headers, json={**body, "canonical_sha256": "0" * 64}
        )
        assert stale.status_code == 409 and stale.json()["code"] == "canonical_review_changed"
        approved = await client.post(endpoint, headers=headers, json=body)
        assert approved.status_code == 202 and approved.json()["unit_count"] == 1
        assert (await client.get(review_url)).status_code == 409
        assert await index_once() >= 1
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            assert version.status == "ready_for_review"
            assert version.index_manifest["unit_count"] == 1
        repeated = await client.post(endpoint, headers=headers, json=body)
        assert repeated.status_code == 409
        async with session_factory()() as db:
            units = (
                await db.scalars(
                    select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
                )
            ).all()
            assert len(units) == 1 and units[0].text == "Программа Python"
    finally:

        def remove():
            adapter = storage()
            try:
                adapter.delete(adapter.derived_bucket, key)
            finally:
                adapter.client.close()

        await run_in_threadpool(remove)
        async with session_factory()() as db:
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            await db.execute(delete(DocumentVersion).where(DocumentVersion.id == version_id))
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


@pytest.mark.parametrize(
    ("media_type", "extension"),
    [("application/pdf", "pdf"),
     ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
     ("image/png", "png"),
     ("image/jpeg", "jpeg")],
)
@pytest.mark.parser
@pytest.mark.storage
async def test_real_parser_host_ingestion_and_curator_publication(
    plane, monkeypatch, tmp_path, media_type, extension
):
    """Explicit opt-in: actual Docker parser, PostgreSQL, Garage and Qdrant."""
    if os.environ.get("PARTNER_PARSER_INTEGRATION") != "1":
        pytest.skip("Real host parser integration requires explicit opt-in")
    import subprocess
    from pathlib import Path

    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents.ingest import storage
    from partner_agent.documents.parser_host import acquire_host_lock, process_version, watch
    from partner_agent.documents.worker import index_once
    from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion
    from partner_agent.retrieval.index import FixtureIndex

    root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("PARTNER_PARSER_MODELS", str(root / ".local/parser-models"))
    fixture = tmp_path / ("course." + extension)
    fixture_script = """
import sys
if sys.argv[2] == "docx":
    from docx import Document
    doc = Document()
    doc.add_paragraph("Course EDU-204 Python 32 hours")
    doc.add_paragraph("Private internal code SECRET-782")
    doc.save(sys.argv[1])
else:
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1600, 500), "white")
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
    draw = ImageDraw.Draw(image)
    draw.text((80, 90), "Course EDU-204 Python 32 hours", font=font, fill="black")
    draw.text((80, 200), "Private internal code SECRET-782", font=font, fill="black")
    image.save(sys.argv[1], sys.argv[2].upper(), resolution=150.0)
"""
    subprocess.run(
        [str(root / "parser/.venv/bin/python"), "-c", fixture_script, str(fixture), extension],
        check=True,
    )
    client, headers, _ = await plane(role="curator")
    other, _, _ = await plane(role="admin")
    created = await client.post(
        "/api/v1/knowledge/documents", headers=headers, json={"title": "PDF course"}
    )
    document_id = uuid.UUID(created.json()["id"])
    version_id = None
    original_key = canonical_key = None
    try:
        response = await client.post(
            f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=1",
            headers={**headers, "Content-Type": media_type}, content=fixture.read_bytes(),
        )
        assert response.status_code == 202, response.text
        version_id = uuid.UUID(response.json()["id"])
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            original_key = version.original_key
        assert (await client.get(f"/api/v1/knowledge/versions/{version_id}")).json()[
            "review_available"
        ] is False
        lock_fd = acquire_host_lock()
        try:
            if extension == "jpeg":
                task = asyncio.create_task(watch(lock_fd, 1))
                try:
                    async with asyncio.timeout(90):
                        while True:
                            async with session_factory()() as db:
                                current = await db.get(DocumentVersion, version_id)
                                if current and current.canonical_key:
                                    break
                            await asyncio.sleep(0.25)
                finally:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            else:
                assert await process_version(version_id, lock_fd)
            assert not await process_version(version_id, lock_fd)
        finally:
            os.close(lock_fd)
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            canonical_key = version.canonical_key
            assert version.status == "pending" and version.canonical_sha256
            assert not (await db.scalars(
                select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
            )).all()
        review_url = f"/api/v1/knowledge/versions/{version_id}/review"
        assert (await other.get(review_url)).status_code == 404
        review = await client.get(review_url)
        assert review.status_code == 200, review.text
        assert (await client.get(f"/api/v1/knowledge/versions/{version_id}")).json()[
            "review_available"
        ] is True
        blocks = review.json()["blocks"]
        public = [block for block in blocks if "EDU-204" in block["text"]]
        private = [block for block in blocks if "Private internal code" in block["text"]]
        assert public and private
        if extension != "docx":
            assert any(p["page"] == 1 and p["bbox"] for p in public[0]["positions"])
        approval = await client.post(
            f"/api/v1/knowledge/versions/{version_id}/approve", headers=headers,
            json={"expected_revision": 2, "canonical_sha256": review.json()["canonical_sha256"],
                  "block_ids": [public[0]["id"]]},
        )
        assert approval.status_code == 202, approval.text
        assert await index_once() >= 1
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            assert version.status == "ready_for_review"
            units = (await db.scalars(
                select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
            )).all()
            assert len(units) == 1 and "Private internal code" not in units[0].text
        published = await client.post(
            f"/api/v1/knowledge/versions/{version_id}/publish", headers=headers,
            json={"expected_revision": 2},
        )
        assert published.status_code == 200, published.text
        found = await client.post(
            "/api/v1/knowledge/search", headers=headers, json={"query": "EDU-204"}
        )
        assert len(found.json()["items"]) == 1
        hidden = await client.post(
            "/api/v1/knowledge/search", headers=headers, json={"query": "internal"}
        )
        assert hidden.json()["items"] == []
    finally:
        if version_id:
            await FixtureIndex(get_settings().qdrant_url).delete_version(version_id)
        def remove():
            adapter = storage()
            try:
                if original_key:
                    adapter.delete(adapter.originals_bucket, original_key)
                if canonical_key:
                    adapter.delete(adapter.derived_bucket, canonical_key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            document = await db.get(Document, document_id)
            if document:
                document.active_version_id = None
                document.last_published_version_id = None
                await db.flush()
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


@pytest.mark.storage
async def test_parser_pending_retries_checksum_and_finalizes_once(plane, monkeypatch):
    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents import parser_host
    from partner_agent.documents.ingest import storage
    from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion

    client, headers, _ = await plane(role="curator")
    created = await client.post(
        "/api/v1/knowledge/documents", headers=headers, json={"title": "Recovery"}
    )
    document_id = uuid.UUID(created.json()["id"])
    version_id = None
    original_key = canonical_key = None
    source = b"%PDF-synthetic-original"
    try:
        upload_url = f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=1"
        unsupported = await client.post(
            upload_url, headers={**headers, "Content-Type": "application/zip"}, content=b"PK"
        )
        assert unsupported.status_code == 415
        uploaded = await client.post(
            upload_url, headers={**headers, "Content-Type": "application/pdf"}, content=source
        )
        assert uploaded.status_code == 202
        version_id = uuid.UUID(uploaded.json()["id"])
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            original_key = version.original_key

        def put_source(data):
            adapter = storage()
            try:
                adapter.put(adapter.originals_bucket, original_key, data, "application/pdf")
            finally:
                adapter.client.close()

        def fake_parser(data, suffix, lock_fd):
            assert data == source and suffix == ".pdf"
            return {
                "partner_parser_version": "docling-2.150.0",
                "body": {"children": [{"$ref": "#/texts/0"}]},
                "texts": [{"self_ref": "#/texts/0", "text": "Test course"}],
            }

        monkeypatch.setattr(parser_host, "run_isolated", fake_parser)
        lock_fd = parser_host.acquire_host_lock()
        try:
            with pytest.raises(BlockingIOError):
                parser_host.acquire_host_lock()
            await run_in_threadpool(put_source, b"altered")
            assert await parser_host.try_process_version(version_id, lock_fd) == "retry"
            async with session_factory()() as db:
                version = await db.get(DocumentVersion, version_id)
                assert version.status == "pending" and version.canonical_key is None
                assert version.error_code == "source_checksum_mismatch"
            await run_in_threadpool(put_source, source)
            assert await parser_host.process_version(version_id, lock_fd)
            assert not await parser_host.process_version(version_id, lock_fd)
        finally:
            os.close(lock_fd)
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            canonical_key = version.canonical_key
            assert version.status == "pending" and version.canonical_sha256
            assert version.error_code is None
            assert not (await db.scalars(
                select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
            )).all()
    finally:
        def remove():
            adapter = storage()
            try:
                if original_key:
                    adapter.delete(adapter.originals_bucket, original_key)
                if canonical_key:
                    adapter.delete(adapter.derived_bucket, canonical_key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


@pytest.mark.storage
async def test_parser_backoff_budget_and_original_restore(plane, monkeypatch):
    import threading

    from sqlalchemy import text
    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents import parser_host
    from partner_agent.documents.ingest import storage
    from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion

    client, headers, _ = await plane(role="curator")
    other, other_headers, _ = await plane(role="admin")
    created = await client.post(
        "/api/v1/knowledge/documents", headers=headers, json={"title": "Retry budget"}
    )
    document_id = uuid.UUID(created.json()["id"])
    version_id = None
    original_key = canonical_key = None
    source = b"%PDF-valid-original"
    try:
        upload = await client.post(
            f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=1",
            headers={**headers, "Content-Type": "application/pdf"}, content=source,
        )
        assert upload.status_code == 202
        version_id = uuid.UUID(upload.json()["id"])
        async with session_factory()() as db:
            original_key = (await db.get(DocumentVersion, version_id)).original_key

        def overwrite(data=b"incorrect"):
            adapter = storage()
            try:
                adapter.put(adapter.originals_bucket, original_key, data, "application/pdf")
            finally:
                adapter.client.close()
        await run_in_threadpool(overwrite)
        actual_try = parser_host.try_process_version

        async def only_this_version(identifier, lock):
            return await actual_try(identifier, lock) if identifier == version_id else "skipped"

        monkeypatch.setattr(parser_host, "try_process_version", only_this_version)
        monkeypatch.setattr(parser_host, "assert_no_parser_containers", lambda: None)
        lock_fd = parser_host.acquire_host_lock()
        try:
            for reason in ("parser_container_present", "parser_infrastructure_unavailable"):
                def blocked():
                    raise RuntimeError(reason)
                with monkeypatch.context() as context:
                    context.setattr(parser_host, "assert_no_parser_containers", blocked)
                    with pytest.raises(RuntimeError, match=reason):
                        await parser_host.run_once(lock_fd)
                async with session_factory()() as db:
                    state = await db.get(DocumentVersion, version_id)
                    assert state.parser_attempts == 0 and state.error_code is None

            # If an orphan appears after preflight, the version still spends no attempt.
            await run_in_threadpool(overwrite, source)
            def late_orphan(data, suffix, lock):
                raise RuntimeError("parser_container_present")
            with monkeypatch.context() as context:
                context.setattr(parser_host, "run_isolated", late_orphan)
                assert await actual_try(version_id, lock_fd) == "blocked"
            async with session_factory()() as db:
                state = await db.get(DocumentVersion, version_id)
                assert state.parser_attempts == 0 and state.error_code is None
            await run_in_threadpool(overwrite)
            assert (await parser_host.run_once(lock_fd))[1] == 1
            assert (await parser_host.run_once(lock_fd))[1] == 0  # Retry is not due.

            # A reset while the old isolated attempt is running must win.
            await run_in_threadpool(overwrite, source)
            async with session_factory()() as db:
                version = await db.get(DocumentVersion, version_id)
                version.parser_retry_at = await db.scalar(
                    text("select now() - interval '1 second'")
                )
                await db.commit()
            started, release = threading.Event(), threading.Event()
            def stale_parser(data, suffix, lock):
                started.set()
                assert release.wait(timeout=5)
                raise RuntimeError("parser_failed")
            retry_url = f"/api/v1/knowledge/versions/{version_id}/retry"
            with monkeypatch.context() as context:
                context.setattr(parser_host, "run_isolated", stale_parser)
                old_attempt = asyncio.create_task(parser_host.run_once(lock_fd))
                try:
                    assert await asyncio.to_thread(started.wait, 5)
                    reset = await client.post(
                        retry_url, headers=headers, json={"expected_revision": 2}
                    )
                    assert reset.status_code == 202 and reset.json()["revision"] == 3
                finally:
                    release.set()
                    await old_attempt
            status = (await client.get(f"/api/v1/knowledge/versions/{version_id}")).json()
            assert status["parser_attempts"] == 0 and status["error_code"] is None
            assert status["parser_retry_at"] is None
            await run_in_threadpool(overwrite)
            for attempt in range(1, 6):
                if attempt > 1:
                    async with session_factory()() as db:
                        version = await db.get(DocumentVersion, version_id)
                        version.parser_retry_at = await db.scalar(
                            text("select now() - interval '1 second'")
                        )
                        await db.commit()
                assert (await parser_host.run_once(lock_fd))[1] == 1
            status = await client.get(f"/api/v1/knowledge/versions/{version_id}")
            assert status.json()["status"] == "failed"
            assert status.json()["parser_attempts"] == 5
            assert status.json()["error_code"] == "source_checksum_mismatch"
            assert status.json()["parser_retry_at"] is None
            assert (await parser_host.run_once(lock_fd))[1] == 0
            restore = f"/api/v1/knowledge/versions/{version_id}/original?expected_revision=3"
            restore_headers = {**headers, "Content-Type": "application/pdf"}
            wrong = await client.put(
                restore, headers=restore_headers, content=b"%PDF-wrong-original"
            )
            assert wrong.status_code == 409
            assert (await other.put(
                restore, headers={**other_headers, "Content-Type": "application/pdf"},
                content=source
            )).status_code == 404
            restored = await client.put(restore, headers=restore_headers, content=source)
            assert restored.status_code == 202
            assert (await client.post(retry_url, json={"expected_revision": 3})).status_code == 403
            reset = await client.post(retry_url, headers=headers, json={"expected_revision": 3})
            assert reset.status_code == 202 and reset.json()["revision"] == 4
            status = (await client.get(f"/api/v1/knowledge/versions/{version_id}")).json()
            assert status["status"] == "pending" and status["parser_attempts"] == 0
            assert status["error_code"] is None

            def fake_parser(data, suffix, lock):
                assert data == source
                return {
                    "partner_parser_version": "docling-2.150.0",
                    "body": {"children": [{"$ref": "#/texts/0"}]},
                    "texts": [{"self_ref": "#/texts/0", "text": "Recovered"}],
                }
            monkeypatch.setattr(parser_host, "run_isolated", fake_parser)
            assert (await parser_host.run_once(lock_fd))[0] == 1
        finally:
            os.close(lock_fd)
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            canonical_key = version.canonical_key
            assert canonical_key and version.status == "pending" and version.error_code is None
    finally:
        def remove():
            adapter = storage()
            try:
                if original_key:
                    adapter.delete(adapter.originals_bucket, original_key)
                if canonical_key:
                    adapter.delete(adapter.derived_bucket, canonical_key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


@pytest.mark.storage
async def test_direct_answer_and_original_grants_are_independent_and_revocable(plane, monkeypatch):
    from types import SimpleNamespace

    import psycopg
    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents import ingest
    from partner_agent.documents.worker import index_once, ingest_once
    from partner_agent.infra.models_documents import (
        DisclosureUnit,
        Document,
        DocumentGrant,
        DocumentVersion,
    )
    from partner_agent.retrieval.index import FixtureIndex

    owner, owner_headers, _ = await plane(role="curator")
    reader, reader_headers, reader_id = await plane(role="user")
    admin, admin_headers, _ = await plane(role="admin")
    created = await owner.post(
        "/api/v1/knowledge/documents", headers=owner_headers, json={"title": "ACL course"}
    )
    document_id = uuid.UUID(created.json()["id"])
    version_id = draft_id = None
    original_keys = []
    canonical_key = None
    source = b"Public Python course\nPrivate internal identifier"
    try:
        upload = await owner.post(
            f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=1",
            headers={**owner_headers, "Content-Type": "text/plain"}, content=source,
        )
        assert upload.status_code == 202
        version_id = uuid.UUID(upload.json()["id"])
        async with session_factory()() as db:
            original_keys.append((await db.get(DocumentVersion, version_id)).original_key)
        assert await ingest_once() >= 1
        review = (await owner.get(f"/api/v1/knowledge/versions/{version_id}/review")).json()
        async with session_factory()() as db:
            canonical_key = (await db.get(DocumentVersion, version_id)).canonical_key
        approval = await owner.post(
            f"/api/v1/knowledge/versions/{version_id}/approve", headers=owner_headers,
            json={"expected_revision": 2, "canonical_sha256": review["canonical_sha256"],
                  "block_ids": [review["blocks"][0]["id"]]},
        )
        assert approval.status_code == 202
        assert await index_once() >= 1
        published = await owner.post(
            f"/api/v1/knowledge/versions/{version_id}/publish", headers=owner_headers,
            json={"expected_revision": 2},
        )
        assert published.status_code == 200 and published.json()["revision"] == 3
        download_url = f"/api/v1/knowledge/versions/{version_id}/original/download"
        search_body = {"query": "Python"}
        assert (await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json=search_body
        )).json()["items"] == []
        assert (await reader.post(download_url, headers=reader_headers)).status_code == 404
        assert (await admin.post(download_url, headers=admin_headers)).status_code == 404
        grant_url = f"/api/v1/knowledge/documents/{document_id}/grants/{reader_id}"
        denied_grant = await admin.put(
            grant_url, headers=admin_headers,
            json={"expected_revision": 3, "allow_answer": True, "allow_original": True},
        )
        assert denied_grant.status_code == 404
        grant = await owner.put(
            grant_url, headers=owner_headers,
            json={"expected_revision": 3, "allow_answer": True, "allow_original": False},
        )
        assert grant.status_code == 200 and grant.json()["acl_revision"] == 2
        versions_url = f"/api/v1/knowledge/documents/{document_id}/versions"
        assert (await reader.get(versions_url)).status_code == 404
        assert (await admin.get(versions_url)).status_code == 404
        list_url = f"/api/v1/knowledge/documents/{document_id}/grants"
        listing = await owner.get(list_url)
        async with session_factory()() as db:
            reader_login = (await db.get(User, reader_id)).normalized_login
        assert listing.status_code == 200 and listing.json()["items"] == [
            {"user_id": str(reader_id), "login": reader_login, "disabled": False,
             "allow_answer": True, "allow_original": False}
        ]
        assert listing.headers["cache-control"] == "private, no-store"
        assert (await owner.get(list_url, params={"expected_acl_revision": 1})).status_code == 409
        assert (await reader.get(list_url)).status_code == 404
        found = await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json=search_body
        )
        assert len(found.json()["items"]) == 1
        assert (await reader.post(download_url, headers=reader_headers)).status_code == 404
        reader_review = await reader.get(f"/api/v1/knowledge/versions/{version_id}/review")
        assert reader_review.status_code == 404
        grant = await owner.put(
            grant_url, headers=owner_headers,
            json={"expected_revision": 4, "allow_answer": True, "allow_original": True},
        )
        assert grant.status_code == 200
        downloaded = await reader.post(download_url, headers=reader_headers)
        assert downloaded.status_code == 200 and downloaded.content == source
        assert downloaded.headers["cache-control"] == "private, no-store"
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        grant = await owner.put(
            grant_url, headers=owner_headers,
            json={"expected_revision": 5, "allow_answer": False, "allow_original": True},
        )
        assert grant.status_code == 200
        assert (await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json=search_body
        )).json()["items"] == []
        assert (await reader.post(download_url, headers=reader_headers)).content == source
        draft = await owner.post(
            f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=6",
            headers={**owner_headers, "Content-Type": "text/plain"}, content=b"Future draft",
        )
        assert draft.status_code == 202
        draft_id = uuid.UUID(draft.json()["id"])
        async with session_factory()() as db:
            original_keys.append((await db.get(DocumentVersion, draft_id)).original_key)
        assert (await reader.post(
            f"/api/v1/knowledge/versions/{draft_id}/original/download", headers=reader_headers
        )).status_code == 404

        # Revoke after Garage has returned bytes but before the final authorization check.
        real_storage = ingest.storage
        def racing_storage():
            adapter = real_storage()
            original_get = adapter.get
            def read_and_revoke(*args, **kwargs):
                value = original_get(*args, **kwargs)
                url = os.environ["PARTNER_TEST_DATABASE_URL"]
                connection_url = url.replace("postgresql+psycopg://", "postgresql://", 1)
                with psycopg.connect(connection_url) as conn:
                    conn.execute(
                        "DELETE FROM document_grants WHERE document_id = %s AND user_id = %s",
                        (document_id, reader_id),
                    )
                    conn.execute(
                        "UPDATE documents SET acl_revision = acl_revision + 1, "
                        "revision = revision + 1 WHERE id = %s", (document_id,),
                    )
                return value
            return SimpleNamespace(
                get=read_and_revoke, originals_bucket=adapter.originals_bucket,
                client=adapter.client
            )
        with monkeypatch.context() as context:
            context.setattr(ingest, "storage", racing_storage)
            revoked = await reader.post(download_url, headers=reader_headers)
        assert revoked.status_code == 404 and source not in revoked.content
        assert (await reader.post(download_url, headers=reader_headers)).status_code == 404
        assert (await owner.post(download_url, headers=owner_headers)).content == source
        regrant = await owner.put(
            grant_url, headers=owner_headers,
            json={"expected_revision": 8, "allow_answer": True, "allow_original": False},
        )
        assert regrant.status_code == 200
        assert len((await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json=search_body
        )).json()["items"]) == 1
        # Revoke after candidates were loaded, before the final evidence check.
        import partner_agent.retrieval.service as retrieval_service

        real_authorize = retrieval_service.lock_authorized_document
        async def revoke_before_final_check(db, actor, identifier, action):
            url = os.environ["PARTNER_TEST_DATABASE_URL"]
            connection_url = url.replace("postgresql+psycopg://", "postgresql://", 1)
            with psycopg.connect(connection_url) as conn:
                conn.execute(
                    "DELETE FROM document_grants WHERE document_id = %s AND user_id = %s",
                    (document_id, reader_id),
                )
                conn.execute(
                    "UPDATE documents SET acl_revision = acl_revision + 1, "
                    "revision = revision + 1 WHERE id = %s", (document_id,),
                )
            return await real_authorize(db, actor, identifier, action)
        with monkeypatch.context() as context:
            context.setattr(
                retrieval_service, "lock_authorized_document", revoke_before_final_check
            )
            raced_search = await reader.post(
                "/api/v1/knowledge/search", headers=reader_headers, json=search_body
            )
        assert raced_search.json()["items"] == []
        regrant = await owner.put(
            grant_url, headers=owner_headers,
            json={"expected_revision": 10, "allow_answer": True, "allow_original": False},
        )
        assert regrant.status_code == 200
        revoke = await owner.put(
            grant_url, headers=owner_headers,
            json={"expected_revision": 11, "allow_answer": False, "allow_original": False},
        )
        assert revoke.status_code == 200
        assert (await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json=search_body
        )).json()["items"] == []
        async with session_factory()() as db:
            doc = await db.get(Document, document_id)
            assert doc.policy_revision == 1 and doc.acl_revision == 9
            audits = await db.scalar(
                select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.action == "document.download_original",
                    AuditEvent.resource_id == str(version_id),
                )
            )
            assert audits == 3  # Two authorized reader downloads and one owner download.
    finally:
        if version_id:
            await FixtureIndex(get_settings().qdrant_url).delete_version(version_id)
        def remove():
            adapter = ingest.storage()
            try:
                for key in original_keys:
                    adapter.delete(adapter.originals_bucket, key)
                if canonical_key:
                    adapter.delete(adapter.derived_bucket, canonical_key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            await db.execute(delete(DocumentGrant).where(DocumentGrant.document_id == document_id))
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            doc = await db.get(Document, document_id)
            if doc:
                doc.active_version_id = None
                doc.last_published_version_id = None
                await db.flush()
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


@pytest.mark.storage
async def test_upload_idempotency_concurrent_retry_and_interrupted_put(plane):
    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents.ingest import admit_version, storage
    from partner_agent.infra.models_documents import Document, DocumentVersion

    client, headers, actor_id = await plane(role="curator")
    other, other_headers, _ = await plane(role="admin")
    created = await client.post(
        "/api/v1/knowledge/documents", headers=headers, json={"title": "Idempotent upload"}
    )
    document_id = uuid.UUID(created.json()["id"])
    keys = []
    source = b"Public course"
    try:
        upload_url = f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=1"
        idempotency_key = str(uuid.uuid4())
        upload_headers = {
            **headers, "Content-Type": "text/plain", "Idempotency-Key": idempotency_key
        }
        first, second = await asyncio.gather(
            client.post(upload_url, headers=upload_headers, content=source),
            client.post(upload_url, headers=upload_headers, content=source),
        )
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert {first.json()["reused"], second.json()["reused"]} == {False, True}
        conflict = await client.post(upload_url, headers=upload_headers, content=b"Other course")
        assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
        invalid = await client.post(
            upload_url, headers={**headers, "Content-Type": "text/plain", "Idempotency-Key": "bad"},
            content=source,
        )
        assert invalid.status_code == 422
        denied = await other.post(
            upload_url, headers={**other_headers, "Content-Type": "text/plain",
                                 "Idempotency-Key": idempotency_key}, content=source,
        )
        assert denied.status_code == 404
        # Simulate admission committed but original PUT interrupted.
        second_key, pending_source = uuid.uuid4(), b"Restored exact bytes"
        async with session_factory()() as db:
            actor = await db.get(User, actor_id)
            pending, is_new = await admit_version(
                db, actor, document_id, 2, pending_source, "text/plain", second_key
            )
            assert is_new
            pending_id, pending_original = pending.id, pending.original_key
            await db.commit()
        retry = await client.post(
            f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=2",
            headers={**headers, "Content-Type": "text/plain", "Idempotency-Key": str(second_key)},
            content=pending_source,
        )
        assert retry.status_code == 202 and retry.json()["id"] == str(pending_id)
        assert retry.json()["reused"] is True
        async with session_factory()() as db:
            versions = (await db.scalars(
                select(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )).all()
            keys = [version.original_key for version in versions]
            assert len(versions) == 2
            assert (await db.get(Document, document_id)).revision == 3
        def verify():
            adapter = storage()
            try:
                actual = adapter.get(adapter.originals_bucket, pending_original).data
                assert actual == pending_source
            finally:
                adapter.client.close()
        await run_in_threadpool(verify)
    finally:
        # Discover keys even if the assertions fail before the normal collection.
        async with session_factory()() as db:
            versions = (await db.scalars(
                select(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )).all()
            keys = [version.original_key for version in versions]
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()
        def remove():
            adapter = storage()
            try:
                for key in keys:
                    adapter.delete(adapter.originals_bucket, key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)


@pytest.mark.storage
async def test_published_docling_evidence_keeps_approved_linked_note(plane):
    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents.docling_adapter import normalize_docling
    from partner_agent.documents.ingest import storage
    from partner_agent.documents.worker import index_once
    from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion
    from partner_agent.retrieval.index import FixtureIndex

    client, headers, _ = await plane(role="curator")
    created = await client.post(
        "/api/v1/knowledge/documents", headers=headers, json={"title": "Условия курса"}
    )
    document_id, version_id = uuid.UUID(created.json()["id"]), uuid.uuid4()
    key = f"{document_id}/{version_id}/canonical.json"
    canonical = normalize_docling(
        {
            "body": {"children": [
                {"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}, {"$ref": "#/texts/2"}
            ]},
            "texts": [
                {"self_ref": "#/texts/0", "text": "Курс Python доступен",
                 "footnotes": [{"$ref": "#/texts/1"}]},
                {"self_ref": "#/texts/1", "label": "footnote", "text": "Только участникам"},
                {"self_ref": "#/texts/2", "text": "Секретная строка КРI-2036"},
            ],
        }, document_id, version_id, "a" * 64, "docling-2.150.0",
    )
    def put():
        adapter = storage()
        try:
            return adapter.put(
                adapter.derived_bucket, key, canonical.model_dump_json().encode(),
                "application/json"
            )
        finally:
            adapter.client.close()
    digest = await run_in_threadpool(put)
    try:
        async with session_factory()() as db:
            db.add(DocumentVersion(
                id=version_id, document_id=document_id, generation=1, source_sha256="a" * 64,
                media_type="application/pdf", size_bytes=10, original_key=f"{version_id}/original",
                canonical_key=key, canonical_sha256=digest, pipeline_version="docling-adapter-v1",
                status="pending",
            ))
            await db.commit()
        review = await client.get(f"/api/v1/knowledge/versions/{version_id}/review")
        assert review.status_code == 200
        assert [block["mixed_script_identifier"] for block in review.json()["blocks"]] == [
            False, False, True,
        ]
        assert review.json()["blocks"][2]["text"] == "Секретная строка КРI-2036"
        approved = await client.post(
            f"/api/v1/knowledge/versions/{version_id}/approve", headers=headers,
            json={"expected_revision": 1, "canonical_sha256": digest,
                  "block_ids": [str(canonical.blocks[0].id), str(canonical.blocks[1].id)]},
        )
        assert approved.status_code == 202 and approved.json()["unit_count"] == 1
        assert await index_once() >= 1
        published = await client.post(
            f"/api/v1/knowledge/versions/{version_id}/publish", headers=headers,
            json={"expected_revision": 1},
        )
        assert published.status_code == 200
        found = await client.post(
            "/api/v1/knowledge/search", headers=headers, json={"query": "Python"}
        )
        evidence = found.json()["items"]
        assert len(evidence) == 1
        assert "Только участникам" in evidence[0]["excerpt"]
        assert "Секретная" not in evidence[0]["excerpt"]
        assert evidence[0]["locator"]["context"][0]["block_id"] == str(canonical.blocks[1].id)
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            assert version.disclosure_format == "docling-context-v2"
            version.disclosure_format = None  # Simulate a pre-migration Docling version.
            await db.commit()
        hidden_legacy = await client.post(
            "/api/v1/knowledge/search", headers=headers, json={"query": "Python"}
        )
        assert hidden_legacy.json()["items"] == []
        versions = await client.get(f"/api/v1/knowledge/documents/{document_id}/versions")
        assert versions.json()["items"][0]["needs_reingest"] is True
        legacy_publish = await client.post(
            f"/api/v1/knowledge/versions/{version_id}/publish", headers=headers,
            json={"expected_revision": 2},
        )
        assert legacy_publish.status_code == 409
    finally:
        await FixtureIndex(get_settings().qdrant_url).delete_version(version_id)
        def remove():
            adapter = storage()
            try:
                adapter.delete(adapter.derived_bucket, key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            document = await db.get(Document, document_id)
            if document:
                document.active_version_id = None
                document.last_published_version_id = None
                await db.flush()
            await db.execute(delete(DocumentVersion).where(DocumentVersion.id == version_id))
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


@pytest.mark.storage
async def test_reindex_recovers_after_remote_write_and_rejects_changed_canonical(
    plane, monkeypatch
):
    from sqlalchemy import text
    from starlette.concurrency import run_in_threadpool

    import partner_agent.retrieval.index as index_module
    from partner_agent.documents.indexing import stage_version
    from partner_agent.documents.ingest import storage
    from partner_agent.documents.worker import index_once, ingest_once
    from partner_agent.infra.errors import APIError
    from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion
    from partner_agent.retrieval.index import COLLECTION, FixtureIndex, IndexConfigurationError

    client, headers, owner_id = await plane(role="curator")
    admin, admin_headers, _ = await plane(role="admin")
    created = await client.post(
        "/api/v1/knowledge/documents", headers=headers, json={"title": "Rebuild"}
    )
    document_id = uuid.UUID(created.json()["id"])
    version_id = original_key = canonical_key = None
    original_canonical = None
    try:
        uploaded = await client.post(
            f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=1",
            headers={**headers, "Content-Type": "text/plain"}, content=b"Python course",
        )
        assert uploaded.status_code == 202
        version_id = uuid.UUID(uploaded.json()["id"])
        assert await ingest_once() >= 1
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            original_key, canonical_key = version.original_key, version.canonical_key
        review = (await client.get(f"/api/v1/knowledge/versions/{version_id}/review")).json()
        approved = await client.post(
            f"/api/v1/knowledge/versions/{version_id}/approve", headers=headers,
            json={"expected_revision": 2, "canonical_sha256": review["canonical_sha256"],
                  "block_ids": [review["blocks"][0]["id"]]},
        )
        assert approved.status_code == 202 and await index_once() >= 1
        assert (await client.post(
            f"/api/v1/knowledge/versions/{version_id}/publish", headers=headers,
            json={"expected_revision": 2},
        )).status_code == 200
        reindex_url = f"/api/v1/knowledge/versions/{version_id}/reindex"
        assert (await admin.post(reindex_url, headers=admin_headers,
                                 json={"expected_revision": 3})).status_code == 404
        started = await client.post(reindex_url, headers=headers, json={"expected_revision": 3})
        assert started.status_code == 202 and started.json()["revision"] == 4
        incompatible = f"partner_test_incompatible_{uuid.uuid4().hex}"
        async with httpx.AsyncClient(base_url=get_settings().qdrant_url) as remote:
            response = await remote.put(f"/collections/{incompatible}", json={
                "vectors": {"size": 4, "distance": "Cosine"},
            })
            response.raise_for_status()
            try:
                with monkeypatch.context() as context:
                    context.setattr(index_module, "COLLECTION", incompatible)
                    with pytest.raises(IndexConfigurationError):
                        await index_once()
                async with session_factory()() as db:
                    version = await db.get(DocumentVersion, version_id)
                    assert version.index_attempts == 0 and version.index_manifest is None
            finally:
                (await remote.delete(f"/collections/{incompatible}")).raise_for_status()
        for failure in (
            IndexConfigurationError("index_configuration_incompatible"),
            httpx.ConnectError("synthetic dependency failure"),
        ):
            async def preflight_unavailable(self):
                raise failure
            with monkeypatch.context() as context:
                context.setattr(FixtureIndex, "ensure_compatible", preflight_unavailable)
                with pytest.raises(type(failure)):
                    await index_once()
            async with session_factory()() as db:
                version = await db.get(DocumentVersion, version_id)
                assert version.index_attempts == 0 and version.index_manifest is None
        async def late_configuration_change(self, points):
            raise IndexConfigurationError("index_configuration_incompatible")
        with monkeypatch.context() as context:
            context.setattr(FixtureIndex, "stage", late_configuration_change)
            with pytest.raises(IndexConfigurationError):
                await index_once()
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            assert version.index_attempts == 0 and version.index_manifest is None
        assert (await client.post(
            "/api/v1/knowledge/search", headers=headers, json={"query": "Python"}
        )).json()["items"] == []
        index = FixtureIndex(get_settings().qdrant_url)
        await index.delete_version(version_id)

        def replace_canonical(data=None):
            adapter = storage()
            try:
                previous = adapter.get(adapter.derived_bucket, canonical_key).data
                if data is not None:
                    adapter.put(adapter.derived_bucket, canonical_key, data, "application/json")
                return previous
            finally:
                adapter.client.close()
        original_canonical = await run_in_threadpool(replace_canonical, b"{}")
        async with session_factory()() as db:
            owner = await db.get(User, owner_id)
            with pytest.raises(APIError) as mismatch:
                await stage_version(db, owner, document_id, version_id)
            assert mismatch.value.code == "canonical_digest_mismatch"
        await index_once()
        status = (await client.get(f"/api/v1/knowledge/versions/{version_id}")).json()
        assert status["status"] == "processing"
        assert status["index_error"] == "canonical_digest_mismatch"
        await run_in_threadpool(replace_canonical, original_canonical)
        async with session_factory()() as db:
            unit = await db.scalar(
                select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
            )
            unit.text = "Altered before indexing"
            await db.commit()
        async with session_factory()() as db:
            owner = await db.get(User, owner_id)
            with pytest.raises(APIError) as changed_before:
                await stage_version(db, owner, document_id, version_id)
            assert changed_before.value.code == "disclosure_integrity_mismatch"
        async with session_factory()() as db:
            unit = await db.scalar(
                select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
            )
            unit.text = "Python course"
            await db.commit()
        async with httpx.AsyncClient(base_url=get_settings().qdrant_url) as qdrant:
            count_request = {"filter": {"must": [
                {"key": "version_id", "match": {"value": str(version_id)}}
            ]}, "exact": True}
            before = await qdrant.post(
                f"/collections/{COLLECTION}/points/count", json=count_request
            )
            assert before.json()["result"]["count"] == 0
            real_stage = FixtureIndex.stage
            async def crash_after_write(self, points):
                await real_stage(self, points)
                raise RuntimeError("synthetic crash after Qdrant write")
            with monkeypatch.context() as context:
                context.setattr(FixtureIndex, "stage", crash_after_write)
                async with session_factory()() as db:
                    owner = await db.get(User, owner_id)
                    with pytest.raises(RuntimeError, match="synthetic crash"):
                        await stage_version(db, owner, document_id, version_id)
            after = await qdrant.post(f"/collections/{COLLECTION}/points/count", json=count_request)
            assert after.json()["result"]["count"] == 1
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            assert version.status == "processing" and version.index_manifest["state"] == "error"

        async def mutate_after_write(self, points):
            result = await real_stage(self, points)
            async with session_factory()() as other:
                unit = await other.scalar(
                    select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
                )
                unit.text = "Changed after remote write"
                await other.commit()
            return result
        with monkeypatch.context() as context:
            context.setattr(FixtureIndex, "stage", mutate_after_write)
            async with session_factory()() as db:
                owner = await db.get(User, owner_id)
                with pytest.raises(APIError) as changed:
                    await stage_version(db, owner, document_id, version_id)
                assert changed.value.code == "index_state_changed"
        async with session_factory()() as db:
            unit = await db.scalar(
                select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
            )
            unit.text = "Python course"
            await db.commit()
        retry_url = f"/api/v1/knowledge/versions/{version_id}/retry-index"
        assert (await admin.post(retry_url, headers=admin_headers,
                                 json={"expected_revision": 4})).status_code == 404
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            version.index_retry_at = await db.scalar(text("select now() - interval '1 second'"))
            await db.commit()
        old_started, old_release = asyncio.Event(), asyncio.Event()
        async def stale_stage(self, points):
            if any(point["payload"]["version_id"] == str(version_id) for point in points):
                old_started.set()
                await old_release.wait()
                raise RuntimeError("old index attempt failed")
            return await real_stage(self, points)
        with monkeypatch.context() as context:
            context.setattr(FixtureIndex, "stage", stale_stage)
            old_attempt = asyncio.create_task(index_once())
            try:
                await asyncio.wait_for(old_started.wait(), timeout=5)
                reset = await client.post(retry_url, headers=headers, json={"expected_revision": 4})
                assert reset.status_code == 202 and reset.json()["revision"] == 5
            finally:
                old_release.set()
                await old_attempt
        reset_status = (await client.get(f"/api/v1/knowledge/versions/{version_id}")).json()
        assert reset_status["index_attempts"] == 0 and reset_status["index_error"] is None
        assert reset_status["index_retry_at"] is None

        # Two simultaneous pollers must not count or execute the same attempt twice.
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def hold_stage(self, points):
            nonlocal calls
            if any(point["payload"]["version_id"] == str(version_id) for point in points):
                calls += 1
                started.set()
                await release.wait()
            return await real_stage(self, points)
        with monkeypatch.context() as context:
            context.setattr(FixtureIndex, "stage", hold_stage)
            first = asyncio.create_task(index_once())
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                await asyncio.wait_for(index_once(), timeout=5)
                assert calls == 1
            finally:
                release.set()
                await first
        result = await client.post(
            "/api/v1/knowledge/search", headers=headers, json={"query": "Python"}
        )
        assert len(result.json()["items"]) == 1
        final_status = (await client.get(f"/api/v1/knowledge/versions/{version_id}")).json()
        assert final_status["index_error"] is None

        # A persistent dependency failure has a durable delay and a five-attempt limit.
        restarted = await client.post(reindex_url, headers=headers, json={"expected_revision": 5})
        assert restarted.status_code == 202 and restarted.json()["revision"] == 6
        async def fail_stage(self, points):
            raise RuntimeError("synthetic index outage")
        with monkeypatch.context() as context:
            context.setattr(FixtureIndex, "stage", fail_stage)
            for attempt in range(1, 6):
                await index_once()
                status = (await client.get(f"/api/v1/knowledge/versions/{version_id}")).json()
                assert status["index_attempts"] == attempt
                assert status["index_error"] == "index_dependency_unavailable"
                if attempt < 5:
                    assert status["index_retry_at"] is not None
                    await index_once()  # Before the deadline, no new attempt occurs.
                    status = (await client.get(f"/api/v1/knowledge/versions/{version_id}")).json()
                    assert status["index_attempts"] == attempt
                    async with session_factory()() as db:
                        version = await db.get(DocumentVersion, version_id)
                        version.index_retry_at = await db.scalar(
                            text("select now() - interval '1 second'")
                        )
                        await db.commit()
            await index_once()
            status = (await client.get(f"/api/v1/knowledge/versions/{version_id}")).json()
            assert status["index_attempts"] == 5 and status["index_retry_at"] is None
        reset = await client.post(retry_url, headers=headers, json={"expected_revision": 6})
        assert reset.status_code == 202 and reset.json()["revision"] == 7
        assert await index_once() >= 1
        final_status = (await client.get(f"/api/v1/knowledge/versions/{version_id}")).json()
        assert final_status["status"] == "ready_for_review" and final_status["index_attempts"] == 0
    finally:
        if version_id:
            await FixtureIndex(get_settings().qdrant_url).delete_version(version_id)
        def remove():
            adapter = storage()
            try:
                if original_key:
                    adapter.delete(adapter.originals_bucket, original_key)
                if canonical_key:
                    adapter.delete(adapter.derived_bucket, canonical_key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            if version_id:
                await db.execute(
                    delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
                )
            document = await db.get(Document, document_id)
            if document:
                document.active_version_id = None
                document.last_published_version_id = None
                await db.flush()
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


@pytest.mark.storage
async def test_text_ingestion_backoff_concurrency_and_manual_recovery(plane, monkeypatch):
    from sqlalchemy import text
    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents import worker
    from partner_agent.documents.ingest import storage
    from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion

    client, headers, _ = await plane(role="curator")
    admin, admin_headers, _ = await plane(role="admin")
    created = await client.post(
        "/api/v1/knowledge/documents", headers=headers, json={"title": "Text recovery"}
    )
    document_id = uuid.UUID(created.json()["id"])
    version_id = original_key = canonical_key = None
    source = "Открытый курс\nУсловия участия".encode()
    try:
        response = await client.post(
            f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=1",
            headers={**headers, "Content-Type": "text/plain"}, content=source,
        )
        assert response.status_code == 202
        version_id = uuid.UUID(response.json()["id"])
        async with session_factory()() as db:
            original_key = (await db.get(DocumentVersion, version_id)).original_key

        def replace(data):
            adapter = storage()
            try:
                if data is None:
                    adapter.delete(adapter.originals_bucket, original_key)
                else:
                    adapter.put(adapter.originals_bucket, original_key, data, "text/plain")
            finally:
                adapter.client.close()

        # A committed version can outlive an interrupted PUT.
        await run_in_threadpool(replace, None)
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        original_process = worker.process_stored_text

        async def blocked_process(db, actor, doc_id, candidate_id):
            nonlocal calls
            if candidate_id == version_id:
                calls += 1
                started.set()
                await release.wait()
            return await original_process(db, actor, doc_id, candidate_id)

        with monkeypatch.context() as context:
            context.setattr(worker, "process_stored_text", blocked_process)
            first = asyncio.create_task(worker.ingest_once())
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                await asyncio.wait_for(worker.ingest_once(), timeout=5)
                assert calls == 1
            finally:
                release.set()
                await first
        status_url = f"/api/v1/knowledge/versions/{version_id}"
        status = (await client.get(status_url)).json()
        assert status["status"] == "pending" and status["text_attempts"] == 1
        assert status["error_code"] == "text_dependency_unavailable"
        assert status["text_retry_at"] is not None
        await worker.ingest_once()  # Not due yet.
        assert (await client.get(status_url)).json()["text_attempts"] == 1

        # An old in-flight failure must not overwrite a curator's explicit reset.
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            version.text_retry_at = await db.scalar(text("select now() - interval '1 second'"))
            await db.commit()
        from partner_agent.infra.errors import APIError

        held_failure, release_failure = asyncio.Event(), asyncio.Event()
        async def stale_failure(db, actor, doc_id, candidate_id):
            if candidate_id == version_id:
                held_failure.set()
                await release_failure.wait()
                raise APIError(409, "source_checksum_mismatch", "Source checksum mismatch")
            return await original_process(db, actor, doc_id, candidate_id)
        retry_url = f"/api/v1/knowledge/versions/{version_id}/retry"
        with monkeypatch.context() as context:
            context.setattr(worker, "process_stored_text", stale_failure)
            old_attempt = asyncio.create_task(worker.ingest_once())
            try:
                await asyncio.wait_for(held_failure.wait(), timeout=5)
                reset = await client.post(retry_url, headers=headers, json={"expected_revision": 2})
                assert reset.status_code == 202 and reset.json()["revision"] == 3
            finally:
                release_failure.set()
                await old_attempt
        status = (await client.get(status_url)).json()
        assert status["text_attempts"] == 0 and status["error_code"] is None
        assert status["text_retry_at"] is None

        await run_in_threadpool(replace, b"changed")
        for expected in range(1, 6):
            if expected > 1:
                async with session_factory()() as db:
                    version = await db.get(DocumentVersion, version_id)
                    version.text_retry_at = await db.scalar(
                        text("select now() - interval '1 second'")
                    )
                    await db.commit()
            await worker.ingest_once()
            status = (await client.get(status_url)).json()
            assert status["text_attempts"] == expected
            assert status["error_code"] == "source_checksum_mismatch"
        assert status["status"] == "failed" and status["text_retry_at"] is None
        await worker.ingest_once()
        assert (await client.get(status_url)).json()["text_attempts"] == 5
        restore_url = f"/api/v1/knowledge/versions/{version_id}/original?expected_revision=3"
        assert (await admin.put(
            restore_url, headers={**admin_headers, "Content-Type": "text/plain"}, content=source,
        )).status_code == 404
        wrong = await client.put(
            restore_url, headers={**headers, "Content-Type": "text/plain"}, content=b"wrong"
        )
        assert wrong.status_code == 409
        restored = await client.put(
            restore_url, headers={**headers, "Content-Type": "text/plain"}, content=source
        )
        assert restored.status_code == 202
        assert (await client.post(retry_url, json={"expected_revision": 3})).status_code == 403
        assert (await client.post(
            retry_url, headers=headers, json={"expected_revision": 2}
        )).status_code == 409
        retried = await client.post(retry_url, headers=headers, json={"expected_revision": 3})
        assert retried.status_code == 202 and retried.json()["revision"] == 4

        # Cancelling a poller while it owns the session lock must release it.
        held, never = asyncio.Event(), asyncio.Event()
        async def interrupted_process(db, actor, doc_id, candidate_id):
            if candidate_id == version_id:
                held.set()
                await never.wait()
            return await original_process(db, actor, doc_id, candidate_id)
        with monkeypatch.context() as context:
            context.setattr(worker, "process_stored_text", interrupted_process)
            interrupted = asyncio.create_task(worker.ingest_once())
            await asyncio.wait_for(held.wait(), timeout=5)
            interrupted.cancel()
            outcome = await asyncio.gather(interrupted, return_exceptions=True)
            assert isinstance(outcome[0], asyncio.CancelledError)
        assert sum(await asyncio.gather(worker.ingest_once(), worker.ingest_once())) >= 1
        status = (await client.get(status_url)).json()
        assert status["status"] == "pending" and status["review_available"] is True
        assert status["text_attempts"] == 0 and status["error_code"] is None
        assert status["text_retry_at"] is None
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            canonical_key = version.canonical_key
            units = (await db.scalars(
                select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
            )).all()
            assert not units  # Explicit curator approval still precedes indexing.
    finally:
        def remove():
            adapter = storage()
            try:
                if original_key:
                    adapter.delete(adapter.originals_bucket, original_key)
                if canonical_key:
                    adapter.delete(adapter.derived_bucket, canonical_key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            if version_id:
                await db.execute(
                    delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
                )
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


@pytest.mark.storage
async def test_direct_unit_deny_overrides_answer_grant_and_is_rechecked(plane, monkeypatch):
    import psycopg
    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents.ingest import storage
    from partner_agent.documents.worker import index_once, ingest_once
    from partner_agent.infra.models_documents import (
        DisclosureAnswerDeny,
        DisclosureUnit,
        Document,
        DocumentGrant,
        DocumentVersion,
    )
    from partner_agent.retrieval.index import FixtureIndex

    owner, owner_headers, owner_id = await plane(role="curator")
    reader, reader_headers, reader_id = await plane(role="user")
    admin, admin_headers, _ = await plane(role="admin")
    created = await owner.post(
        "/api/v1/knowledge/documents", headers=owner_headers, json={"title": "Доступ к курсам"}
    )
    document_id = uuid.UUID(created.json()["id"])
    version_id = original_key = canonical_key = None
    source = "Python открытый курс\nPython внутренний курс".encode()
    try:
        uploaded = await owner.post(
            f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=1",
            headers={**owner_headers, "Content-Type": "text/plain"}, content=source,
        )
        assert uploaded.status_code == 202
        version_id = uuid.UUID(uploaded.json()["id"])
        assert await ingest_once() >= 1
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            original_key, canonical_key = version.original_key, version.canonical_key
        review = (await owner.get(f"/api/v1/knowledge/versions/{version_id}/review")).json()
        approved = await owner.post(
            f"/api/v1/knowledge/versions/{version_id}/approve", headers=owner_headers,
            json={"expected_revision": 2, "canonical_sha256": review["canonical_sha256"],
                  "block_ids": [block["id"] for block in review["blocks"]]},
        )
        assert approved.status_code == 202 and await index_once() >= 1
        published = await owner.post(
            f"/api/v1/knowledge/versions/{version_id}/publish", headers=owner_headers,
            json={"expected_revision": 2},
        )
        assert published.status_code == 200
        grant_url = f"/api/v1/knowledge/documents/{document_id}/grants/{reader_id}"
        granted = await owner.put(
            grant_url, headers=owner_headers,
            json={"expected_revision": 3, "allow_answer": True, "allow_original": True},
        )
        assert granted.status_code == 200 and granted.json()["revision"] == 4
        units_url = f"/api/v1/knowledge/versions/{version_id}/units"
        assert (await reader.get(units_url)).status_code == 404
        assert (await admin.get(units_url)).status_code == 404
        page1 = await owner.get(units_url, params={"limit": 1})
        assert page1.status_code == 200 and page1.headers["cache-control"] == "private, no-store"
        cursor = page1.json()["next_after"]
        assert cursor
        page2 = await owner.get(units_url, params={"limit": 1, "after": cursor})
        all_units = page1.json()["items"] + page2.json()["items"]
        assert len(all_units) == 2 and page2.json()["next_after"] is None
        secret = next(unit for unit in all_units if "внутренний" in unit["excerpt"])
        unit_id = uuid.UUID(secret["id"])
        deny_url = f"/api/v1/knowledge/versions/{version_id}/units/{unit_id}/denies/{reader_id}"
        deny_body = {"expected_revision": 4, "denied": True}
        assert (await admin.put(deny_url, headers=admin_headers, json=deny_body)).status_code == 404
        assert (await owner.put(deny_url, json=deny_body)).status_code == 403
        denied = await owner.put(deny_url, headers=owner_headers, json=deny_body)
        assert denied.status_code == 200 and denied.json()["revision"] == 5
        async with session_factory()() as db:
            reader_login = (await db.get(User, reader_id)).normalized_login
        access_url = f"/api/v1/knowledge/documents/{document_id}/access-review"
        access_body = {"login": reader_login.upper(), "version_id": str(version_id)}
        assert (await owner.post(access_url, json=access_body)).status_code == 403
        assert (
            await reader.post(access_url, headers=reader_headers, json=access_body)
        ).status_code == 404
        assert (
            await admin.post(access_url, headers=admin_headers, json=access_body)
        ).status_code == 404
        access = await owner.post(access_url, headers=owner_headers, json=access_body)
        assert access.status_code == 200 and access.headers["cache-control"] == "private, no-store"
        snapshot = access.json()
        assert snapshot["revision"] == 5 and snapshot["user_id"] == str(reader_id)
        assert snapshot["allow_answer"] and snapshot["allow_original"] and not snapshot["owner"]
        assert len(snapshot["units"]) == 2
        assert next(item for item in snapshot["units"] if item["id"] == str(unit_id))["denied"]
        assert (await owner.post(access_url, headers=owner_headers, json={
            "login": "absent-account-" + uuid.uuid4().hex,
        })).status_code == 404
        assert (await owner.post(access_url, headers=owner_headers, json={
            "login": reader_login, "version_id": str(uuid.uuid4()),
        })).status_code == 404
        repeated = await owner.put(
            deny_url, headers=owner_headers, json={"expected_revision": 5, "denied": True}
        )
        assert repeated.status_code == 200 and repeated.json()["revision"] == 5
        stale = await owner.put(deny_url, headers=owner_headers, json=deny_body)
        assert stale.status_code == 409
        deny_list = f"/api/v1/knowledge/versions/{version_id}/units/{unit_id}/denies"
        listing = await owner.get(deny_list)
        assert listing.json()["user_ids"] == [str(reader_id)]
        assert listing.headers["cache-control"] == "private, no-store"
        assert (await reader.get(deny_list)).status_code == 404
        found = await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json={"query": "Python"}
        )
        assert len(found.json()["items"]) == 1
        assert "открытый" in found.json()["items"][0]["excerpt"]
        owned = await owner.post(
            "/api/v1/knowledge/search", headers=owner_headers, json={"query": "Python"}
        )
        assert len(owned.json()["items"]) == 2
        # Answer deny does not rewrite the separately granted original permission.
        original = await reader.post(
            f"/api/v1/knowledge/versions/{version_id}/original/download", headers=reader_headers
        )
        assert original.status_code == 200 and original.content == source
        removed = await owner.put(
            deny_url, headers=owner_headers, json={"expected_revision": 5, "denied": False}
        )
        assert removed.status_code == 200 and removed.json()["revision"] == 6

        # Insert a deny after candidate loading but before the final document-locked check.
        import partner_agent.retrieval.service as retrieval_service

        real_authorize = retrieval_service.lock_authorized_document
        async def deny_before_final(db, actor, identifier, action):
            url = os.environ["PARTNER_TEST_DATABASE_URL"]
            connection_url = url.replace("postgresql+psycopg://", "postgresql://", 1)
            with psycopg.connect(connection_url) as conn:
                conn.execute(
                    "INSERT INTO disclosure_answer_denies (unit_id,user_id) VALUES (%s,%s)",
                    (unit_id, reader_id),
                )
                conn.execute(
                    "UPDATE documents SET acl_revision = acl_revision + 1, "
                    "revision = revision + 1 WHERE id = %s", (document_id,),
                )
            return await real_authorize(db, actor, identifier, action)
        with monkeypatch.context() as context:
            context.setattr(retrieval_service, "lock_authorized_document", deny_before_final)
            raced = await reader.post(
                "/api/v1/knowledge/search", headers=reader_headers, json={"query": "внутренний"}
            )
        assert raced.json()["items"] == []
        removed = await owner.put(
            deny_url, headers=owner_headers, json={"expected_revision": 7, "denied": False}
        )
        assert removed.status_code == 200 and removed.json()["revision"] == 8
        # Even the owner's answer access respects an explicit unit deny.
        owner_deny = f"/api/v1/knowledge/versions/{version_id}/units/{unit_id}/denies/{owner_id}"
        added = await owner.put(
            owner_deny, headers=owner_headers, json={"expected_revision": 8, "denied": True}
        )
        assert added.status_code == 200
        hidden = await owner.post(
            "/api/v1/knowledge/search", headers=owner_headers, json={"query": "внутренний"}
        )
        assert hidden.json()["items"] == []
        restored = await owner.put(
            owner_deny, headers=owner_headers, json={"expected_revision": 9, "denied": False}
        )
        assert restored.status_code == 200
        visible = await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json={"query": "Python"}
        )
        assert len(visible.json()["items"]) == 2
        async with session_factory()() as db:
            account = await db.get(User, reader_id)
            account.disabled = True
            await db.commit()
        disabled_review = await owner.post(access_url, headers=owner_headers, json=access_body)
        assert disabled_review.status_code == 200 and disabled_review.json()["disabled"]
        assert not disabled_review.json()["policy_allows_answer"]
        assert not disabled_review.json()["policy_allows_original"]
        reduced = await owner.put(grant_url, headers=owner_headers, json={
            "expected_revision": 10, "allow_answer": False, "allow_original": True,
        })
        assert reduced.status_code == 200 and reduced.json()["revision"] == 11
        increase = await owner.put(grant_url, headers=owner_headers, json={
            "expected_revision": 11, "allow_answer": True, "allow_original": True,
        })
        assert increase.status_code == 422
    finally:
        if version_id:
            await FixtureIndex(get_settings().qdrant_url).delete_version(version_id)
        def remove():
            adapter = storage()
            try:
                if original_key:
                    adapter.delete(adapter.originals_bucket, original_key)
                if canonical_key:
                    adapter.delete(adapter.derived_bucket, canonical_key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            units = select(DisclosureUnit.id).where(DisclosureUnit.version_id == version_id)
            await db.execute(
                delete(DisclosureAnswerDeny).where(DisclosureAnswerDeny.unit_id.in_(units))
            )
            await db.execute(delete(DocumentGrant).where(DocumentGrant.document_id == document_id))
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            document = await db.get(Document, document_id)
            if document:
                document.active_version_id = None
                document.last_published_version_id = None
                await db.flush()
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


@pytest.mark.storage
async def test_version_switch_requires_explicit_review_of_prior_unit_denies(plane):
    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents.ingest import storage
    from partner_agent.documents.worker import index_once, ingest_once
    from partner_agent.infra.models_documents import (
        DisclosureAnswerDeny,
        DisclosureUnit,
        Document,
        DocumentGrant,
        DocumentVersion,
    )
    from partner_agent.retrieval.index import FixtureIndex

    owner, owner_headers, owner_id = await plane(role="curator")
    reader, reader_headers, reader_id = await plane(role="user")
    created = await owner.post(
        "/api/v1/knowledge/documents", headers=owner_headers, json={"title": "Версии курса"}
    )
    document_id = uuid.UUID(created.json()["id"])
    version_ids = []
    storage_keys = []
    source = b"Python public course\nPython hidden course"

    async def make_version(revision):
        uploaded = await owner.post(
            f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision={revision}",
            headers={**owner_headers, "Content-Type": "text/plain"}, content=source,
        )
        assert uploaded.status_code == 202
        version_id = uuid.UUID(uploaded.json()["id"])
        version_ids.append(version_id)
        assert await ingest_once() >= 1
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            storage_keys.append((version.original_key, version.canonical_key))
        review = (await owner.get(f"/api/v1/knowledge/versions/{version_id}/review")).json()
        approved = await owner.post(
            f"/api/v1/knowledge/versions/{version_id}/approve", headers=owner_headers,
            json={"expected_revision": revision + 1, "canonical_sha256": review["canonical_sha256"],
                  "block_ids": [block["id"] for block in review["blocks"]]},
        )
        assert approved.status_code == 202 and await index_once() >= 1
        units = (await owner.get(f"/api/v1/knowledge/versions/{version_id}/units")).json()["items"]
        hidden_id = next(unit["id"] for unit in units if "hidden" in unit["excerpt"])
        return version_id, hidden_id

    try:
        first_id, first_unit = await make_version(1)
        published = await owner.post(
            f"/api/v1/knowledge/versions/{first_id}/publish", headers=owner_headers,
            json={"expected_revision": 2},
        )
        assert published.status_code == 200 and published.json()["revision"] == 3
        grant = await owner.put(
            f"/api/v1/knowledge/documents/{document_id}/grants/{reader_id}",
            headers=owner_headers,
            json={"expected_revision": 3, "allow_answer": True, "allow_original": True},
        )
        assert grant.status_code == 200
        old_deny = f"/api/v1/knowledge/versions/{first_id}/units/{first_unit}/denies/{reader_id}"
        assert (await owner.put(
            old_deny, headers=owner_headers, json={"expected_revision": 4, "denied": True}
        )).status_code == 200
        second_id, second_unit = await make_version(5)
        publish_url = f"/api/v1/knowledge/versions/{second_id}/publish"
        report_url = f"/api/v1/knowledge/documents/{document_id}/publication-acl-review"
        params = {"target_version_id": str(second_id)}
        assert (await reader.get(report_url, params=params)).status_code == 404
        report = await owner.get(report_url, params=params)
        assert report.status_code == 200 and report.headers["cache-control"] == "private, no-store"
        assert report.json()["revision"] == 6 and report.json()["incomplete"] is False
        assert report.json()["previous"][0]["user_id"] == str(reader_id)
        assert "hidden" in report.json()["previous"][0]["excerpt"]
        assert report.json()["target"] == []
        blocked = await owner.post(
            publish_url, headers=owner_headers, json={"expected_revision": 6}
        )
        assert blocked.status_code == 409 and blocked.json()["code"] == "deny_review_required"
        before = await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json={"query": "hidden"}
        )
        assert before.json()["items"] == []
        search = {"query": "public"}
        assert len((await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json=search
        )).json()["items"]) == 1
        download_url = f"/api/v1/knowledge/versions/{first_id}/original/download"
        assert (await reader.post(download_url, headers=reader_headers)).content == source
        withdraw_url = f"/api/v1/knowledge/documents/{document_id}/unpublish"
        assert (await reader.post(
            withdraw_url, headers=reader_headers, json={"expected_revision": 6}
        )).status_code == 404
        assert (await owner.post(withdraw_url, json={"expected_revision": 6})).status_code == 403
        assert (await owner.post(
            withdraw_url, headers=owner_headers, json={"expected_revision": 5}
        )).status_code == 409
        withdrawn = await owner.post(
            withdraw_url, headers=owner_headers, json={"expected_revision": 6},
        )
        assert withdrawn.status_code == 200
        assert withdrawn.json()["active_version_id"] is None
        assert withdrawn.json()["last_published_version_id"] == str(first_id)
        assert (await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json=search
        )).json()["items"] == []
        assert (await reader.post(download_url, headers=reader_headers)).status_code == 404
        blocked_again = await owner.post(
            publish_url, headers=owner_headers, json={"expected_revision": 7}
        )
        assert blocked_again.status_code == 409
        assert blocked_again.json()["code"] == "deny_review_required"
        new_deny = f"/api/v1/knowledge/versions/{second_id}/units/{second_unit}/denies/{reader_id}"
        assert (await owner.put(
            new_deny, headers=owner_headers, json={"expected_revision": 7, "denied": True}
        )).status_code == 200
        report = await owner.get(report_url, params=params)
        assert report.json()["revision"] == 8
        assert report.json()["target"][0]["user_id"] == str(reader_id)
        stale = await owner.post(
            publish_url, headers=owner_headers,
            json={"expected_revision": 7, "confirm_deny_review": True},
        )
        assert stale.status_code == 409
        switched = await owner.post(
            publish_url, headers=owner_headers,
            json={"expected_revision": 8, "confirm_deny_review": True},
        )
        assert switched.status_code == 200 and switched.json()["revision"] == 9
        assert switched.json()["last_published_version_id"] == str(second_id)
        hidden = await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json={"query": "hidden"}
        )
        visible = await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json={"query": "public"}
        )
        assert hidden.json()["items"] == [] and len(visible.json()["items"]) == 1
        async with session_factory()() as db:
            audit_count = await db.scalar(
                select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.actor_id == owner_id,
                    AuditEvent.action == "document.publish_after_deny_review",
                    AuditEvent.resource_id == str(document_id),
                )
            )
            assert audit_count == 1
    finally:
        for version_id in version_ids:
            await FixtureIndex(get_settings().qdrant_url).delete_version(version_id)
        def remove():
            adapter = storage()
            try:
                for original, canonical in storage_keys:
                    adapter.delete(adapter.originals_bucket, original)
                    if canonical:
                        adapter.delete(adapter.derived_bucket, canonical)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            units = select(DisclosureUnit.id).where(DisclosureUnit.version_id.in_(version_ids))
            await db.execute(
                delete(DisclosureAnswerDeny).where(DisclosureAnswerDeny.unit_id.in_(units))
            )
            await db.execute(delete(DocumentGrant).where(DocumentGrant.document_id == document_id))
            await db.execute(
                delete(DisclosureUnit).where(DisclosureUnit.version_id.in_(version_ids))
            )
            document = await db.get(Document, document_id)
            if document:
                document.active_version_id = None
                document.last_published_version_id = None
                await db.flush()
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


async def test_access_review_pages_units_and_rejects_foreign_version(plane):
    from partner_agent.infra.models_documents import (
        DisclosureUnit,
        Document,
        DocumentGrant,
        DocumentVersion,
    )

    curator, headers, curator_id = await plane(role="curator")
    _, _, reader_id = await plane(role="user")
    _, _, other_id = await plane(role="user")
    document_id, foreign_id, version_id, foreign_version_id = [uuid.uuid4() for _ in range(4)]
    try:
        async with session_factory()() as db:
            reader = await db.get(User, reader_id)
            login = reader.normalized_login
            db.add_all([
                Document(id=document_id, owner_id=curator_id, scope="knowledge", title="Курс"),
                Document(id=foreign_id, owner_id=curator_id, scope="knowledge",
                         title="Другой курс"),
            ])
            await db.flush()
            for did, vid in ((document_id, version_id), (foreign_id, foreign_version_id)):
                db.add(DocumentVersion(
                    id=vid, document_id=did, generation=1, source_sha256="a" * 64,
                    media_type="text/plain", size_bytes=1, original_key=f"{vid}/original",
                    pipeline_version="text-v1",
                ))
            await db.flush()
            db.add_all([
                DisclosureUnit(
                    version_id=version_id, block_id=uuid.uuid4(), text=f"Текст {index}",
                    text_sha256="b" * 64, locator={"line": index + 1}, policy_revision=1,
                ) for index in range(51)
            ])
            db.add_all([
                DocumentGrant(document_id=document_id, user_id=reader_id,
                              allow_answer=True, allow_original=False),
                DocumentGrant(document_id=document_id, user_id=other_id,
                              allow_answer=False, allow_original=True),
            ])
            await db.commit()
        endpoint = f"/api/v1/knowledge/documents/{document_id}/access-review"
        unbound = await curator.get(f"/api/v1/knowledge/documents/{document_id}/audience")
        assert unbound.status_code == 200
        assert {item["user_id"] for item in unbound.json()["items"]} == {
            str(curator_id), str(reader_id), str(other_id),
        }
        body = {"login": login, "version_id": str(version_id)}
        first = await curator.post(endpoint, headers=headers, json=body)
        assert first.status_code == 200 and len(first.json()["units"]) == 50
        cursor = first.json()["next_after"]
        assert cursor is not None
        token = first.json()["snapshot_token"]
        assert token and first.json()["membership_snapshot_checked"]
        second = await curator.post(endpoint, headers=headers, json={
            **body, "after": cursor, "snapshot_token": token,
        })
        assert second.status_code == 200 and len(second.json()["units"]) == 1
        assert second.json()["next_after"] is None
        assert not {item["id"] for item in first.json()["units"]} & {
            item["id"] for item in second.json()["units"]
        }
        async with session_factory()() as db:
            await db.execute(update(User).where(User.id == reader_id).values(
                revision=User.revision + 1
            ))
            await db.commit()
        changed_subject = await curator.post(endpoint, headers=headers, json={
            **body, "after": cursor, "snapshot_token": token,
        })
        assert changed_subject.status_code == 409
        fresh = await curator.post(endpoint, headers=headers, json=body)
        token = fresh.json()["snapshot_token"]
        foreign = await curator.post(endpoint, headers=headers, json={
            "login": login, "version_id": str(foreign_version_id),
        })
        assert foreign.status_code == 404
        list_url = f"/api/v1/knowledge/documents/{document_id}/grants"
        first_grant = await curator.get(list_url, params={"limit": 1})
        assert first_grant.status_code == 200 and first_grant.json()["next_after"]
        revision = first_grant.json()["acl_revision"]
        cursor = first_grant.json()["next_after"]
        second_grant = await curator.get(list_url, params={
            "limit": 1, "after": cursor, "expected_acl_revision": revision,
        })
        assert second_grant.status_code == 200 and second_grant.json()["next_after"] is None
        assert (
            first_grant.json()["items"][0]["user_id"]
            != second_grant.json()["items"][0]["user_id"]
        )
        revoked = await curator.put(
            f"{list_url}/{reader_id}", headers=headers,
            json={"expected_revision": 1, "allow_answer": False, "allow_original": False},
        )
        assert revoked.status_code == 200
        stale_review = await curator.post(endpoint, headers=headers, json={
            **body, "after": cursor, "snapshot_token": token,
        })
        assert stale_review.status_code == 409
        stale = await curator.get(list_url, params={
            "limit": 1, "after": cursor, "expected_acl_revision": revision,
        })
        assert stale.status_code == 409
    finally:
        async with session_factory()() as db:
            await db.execute(delete(DocumentGrant).where(DocumentGrant.document_id == document_id))
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            await db.execute(
                delete(DocumentVersion).where(
                    DocumentVersion.id.in_([version_id, foreign_version_id])
                )
            )
            await db.execute(delete(Document).where(Document.id.in_([document_id, foreign_id])))
            await db.commit()


@pytest.mark.storage
async def test_long_disclosure_never_becomes_truncated_evidence(plane):
    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents.ingest import storage
    from partner_agent.documents.worker import ingest_once
    from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion

    owner, headers, _ = await plane(role="curator")
    created = await owner.post(
        "/api/v1/knowledge/documents", headers=headers, json={"title": "Длинный фрагмент"}
    )
    document_id = uuid.UUID(created.json()["id"])
    version_id = original_key = canonical_key = None
    limit_text = "Короткий " + "x" * 1591
    assert len(limit_text) == 1600
    source = (("A" * 1601) + " secret-end\n" + limit_text).encode()
    try:
        uploaded = await owner.post(
            f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=1",
            headers={**headers, "Content-Type": "text/plain"}, content=source,
        )
        version_id = uuid.UUID(uploaded.json()["id"])
        assert await ingest_once() >= 1
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            original_key, canonical_key = version.original_key, version.canonical_key
        review = (await owner.get(f"/api/v1/knowledge/versions/{version_id}/review")).json()
        approval = await owner.post(
            f"/api/v1/knowledge/versions/{version_id}/approve", headers=headers,
            json={"expected_revision": 2, "canonical_sha256": review["canonical_sha256"],
                  "block_ids": [item["id"] for item in review["blocks"]]},
        )
        assert approval.status_code == 422
        assert approval.json()["code"] == "disclosure_unit_too_large"
        async with session_factory()() as db:
            assert not (await db.scalars(
                select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
            )).all()
        at_limit = await owner.post(
            f"/api/v1/knowledge/versions/{version_id}/approve", headers=headers,
            json={"expected_revision": 2, "canonical_sha256": review["canonical_sha256"],
                  "block_ids": [review["blocks"][1]["id"]]},
        )
        assert at_limit.status_code == 202
        async with session_factory()() as db:
            version = await db.get(DocumentVersion, version_id)
            version.status = "ready_for_review"
            version.approved_canonical_sha256 = version.canonical_sha256
            version.index_manifest = {"state": "verified", "version_id": str(version_id),
                                      "policy_revision": 1, "unit_count": 2}
            db.add(DisclosureUnit(version_id=version_id, block_id=uuid.uuid4(),
                                  text=("A" * 1601) + " secret-end", text_sha256="b" * 64,
                                  locator={"line": 1}, policy_revision=1))
            await db.commit()
        version_list = await owner.get(f"/api/v1/knowledge/documents/{document_id}/versions")
        assert version_list.status_code == 200
        assert version_list.json()["items"][0]["oversized_units"]
        detail = await owner.get(f"/api/v1/knowledge/versions/{version_id}")
        assert detail.status_code == 200 and detail.json()["oversized_units"]
        publish_url = f"/api/v1/knowledge/versions/{version_id}/publish"
        rejected = await owner.post(publish_url, headers=headers, json={"expected_revision": 2})
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "disclosure_unit_too_large"
        async with session_factory()() as db:
            document = await db.get(Document, document_id)
            document.active_version_id = version_id  # Simulate a legacy publication.
            await db.commit()
        async def search(query):
            return (await owner.post(
                "/api/v1/knowledge/search", headers=headers, json={"query": query}
            )).json()["items"]
        assert await search("secret-end") == []
        assert [item["excerpt"] for item in await search("Короткий")] == [limit_text]
    finally:
        def remove():
            adapter = storage()
            try:
                if original_key:
                    adapter.delete(adapter.originals_bucket, original_key)
                if canonical_key:
                    adapter.delete(adapter.derived_bucket, canonical_key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            document = await db.get(Document, document_id)
            if document:
                document.active_version_id = None
                document.last_published_version_id = None
                await db.flush()
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            await db.execute(delete(DocumentVersion).where(DocumentVersion.id == version_id))
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


async def test_group_directory_does_not_grant_knowledge_access(plane):
    from partner_agent.infra.models import Group, GroupMember, Membership, Workspace
    from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion

    admin, headers, _ = await plane(role="admin")
    curator, curator_headers, curator_id = await plane(role="curator")
    reader, reader_headers, reader_id = await plane(role="user")
    workspace_ids, group_ids = [], []
    document_id, version_id = uuid.uuid4(), uuid.uuid4()
    try:
        assert (await reader.post(
            "/api/v1/admin/workspaces", headers=reader_headers, json={"name": "Команда"}
        )).status_code == 403
        assert (await admin.post(
            "/api/v1/admin/workspaces", json={"name": "Команда"}
        )).status_code == 403
        for name in ("Команда", "Другая организация"):
            response = await admin.post(
                "/api/v1/admin/workspaces", headers=headers, json={"name": name}
            )
            assert response.status_code == 201
            workspace_ids.append(uuid.UUID(response.json()["id"]))
        workspace, foreign = workspace_ids
        groups_url = f"/api/v1/admin/workspaces/{workspace}/groups"
        created = await admin.post(groups_url, headers=headers, json={"name": "Ученики"})
        assert created.status_code == 201
        group_id = uuid.UUID(created.json()["id"])
        group_ids.append(group_id)
        assert (await admin.get(groups_url)).json()["items"][0]["name"] == "Ученики"
        member_url = f"{groups_url}/{group_id}/members/{reader_id}"
        assert (await admin.put(member_url, headers=headers)).status_code == 422
        async with session_factory()() as db:
            db.add(GroupMember(group_id=group_id, user_id=reader_id, workspace_id=workspace))
            with pytest.raises(IntegrityError) as failure:
                await db.commit()
            assert "fk_group_member_workspace_membership" in str(failure.value.orig)
            await db.rollback()
        foreign_url = f"/api/v1/admin/workspaces/{foreign}/groups/{group_id}/members/{reader_id}"
        assert (await admin.put(foreign_url, headers=headers)).status_code == 404
        workspace_member = f"/api/v1/admin/workspaces/{workspace}/members/{reader_id}"
        assert (await admin.put(workspace_member, headers=headers)).status_code == 200
        foreign_member = f"/api/v1/admin/workspaces/{foreign}/members/{reader_id}"
        assert (await admin.put(foreign_member, headers=headers)).status_code == 200
        async with session_factory()() as db:
            db.add(GroupMember(group_id=group_id, user_id=reader_id, workspace_id=foreign))
            with pytest.raises(IntegrityError) as failure:
                await db.commit()
            assert "fk_group_member_group_workspace" in str(failure.value.orig)
            await db.rollback()
        assert (await admin.delete(foreign_member, headers=headers)).status_code == 200
        workspace_members = await admin.get(f"/api/v1/admin/workspaces/{workspace}/members")
        assert workspace_members.headers["cache-control"] == "private, no-store"
        assert workspace_members.json()["items"][0]["id"] == str(reader_id)
        results = await asyncio.gather(
            admin.put(member_url, headers=headers), admin.put(member_url, headers=headers)
        )
        assert [response.status_code for response in results] == [200, 200]
        members = await admin.get(f"{groups_url}/{group_id}/members")
        assert members.headers["cache-control"] == "private, no-store"
        assert members.status_code == 200 and members.json()["items"][0]["id"] == str(reader_id)
        async with session_factory()() as db:
            db.add(Document(id=document_id, owner_id=curator_id,
                            scope="knowledge", title="Материал"))
            await db.flush()
            version = DocumentVersion(
                id=version_id, document_id=document_id, generation=1, source_sha256="a" * 64,
                media_type="text/plain", size_bytes=1, original_key=f"{version_id}/original",
                canonical_key="canonical", canonical_sha256="b" * 64,
                approved_canonical_sha256="b" * 64, pipeline_version="canonical-v1",
                status="ready_for_review",
            )
            db.add(version)
            await db.flush()
            db.add(DisclosureUnit(version_id=version_id, block_id=uuid.uuid4(), text="Python курс",
                                  text_sha256="c" * 64, locator={"line": 1}, policy_revision=1))
            document = await db.get(Document, document_id)
            document.active_version_id = version_id
            await db.commit()
        search = {"query": "Python"}
        assert len((await curator.post(
            "/api/v1/knowledge/search", headers=curator_headers, json=search
        )).json()["items"]) == 1
        assert (await reader.post(
            "/api/v1/knowledge/search", headers=reader_headers, json=search
        )).json()["items"] == []
        assert (await admin.post(
            "/api/v1/knowledge/search", headers=headers, json=search
        )).json()["items"] == []
        assert (await admin.delete(member_url, headers=headers)).status_code == 200
        assert (await admin.delete(member_url, headers=headers)).status_code == 200
        assert (await admin.get(f"{groups_url}/{group_id}/members")).json()["items"] == []
        assert (await admin.put(member_url, headers=headers)).status_code == 200
        assert (await admin.delete(workspace_member, headers=headers)).status_code == 200
        assert (await admin.get(f"{groups_url}/{group_id}/members")).json()["items"] == []
        assert (await admin.put(member_url, headers=headers)).status_code == 422
    finally:
        async with session_factory()() as db:
            document = await db.get(Document, document_id)
            if document:
                document.active_version_id = None
                document.last_published_version_id = None
                await db.flush()
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            await db.execute(delete(DocumentVersion).where(DocumentVersion.id == version_id))
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.execute(delete(GroupMember).where(GroupMember.group_id.in_(group_ids)))
            await db.execute(delete(Group).where(Group.id.in_(group_ids)))
            await db.execute(delete(Membership).where(Membership.workspace_id.in_(workspace_ids)))
            await db.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
            await db.commit()


@pytest.mark.storage
async def test_group_grants_enforce_membership_deny_and_revoke(plane, monkeypatch):
    import hashlib
    import threading

    from starlette.concurrency import run_in_threadpool

    import partner_agent.documents.ingest as ingest
    import partner_agent.retrieval.service as retrieval
    from partner_agent.documents.ingest import storage
    from partner_agent.infra.models import Group, GroupMember, Membership, Workspace
    from partner_agent.infra.models_documents import (
        DisclosureAnswerDeny,
        DisclosureUnit,
        Document,
        DocumentGroupGrant,
        DocumentVersion,
    )

    admin, ah, _ = await plane(role="admin")
    curator, ch, curator_id = await plane(role="curator")
    reader, rh, reader_id = await plane(role="user")
    workspace_ids, group_ids = [], []
    doc_id, version_id, unit_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = f"{doc_id}/{version_id}/original"
    try:
        for name in ("Команда", "Чужая организация"):
            result = await admin.post("/api/v1/admin/workspaces", headers=ah, json={"name": name})
            workspace_ids.append(uuid.UUID(result.json()["id"]))
        workspace, foreign = workspace_ids
        for wid in workspace_ids:
            result = await admin.post(
                f"/api/v1/admin/workspaces/{wid}/groups", headers=ah, json={"name": "Ученики"}
            )
            group_ids.append(uuid.UUID(result.json()["id"]))
        group, foreign_group = group_ids
        for user in (curator_id, reader_id):
            result = await admin.put(
                f"/api/v1/admin/workspaces/{workspace}/members/{user}", headers=ah
            )
            assert result.status_code == 200
        membership = f"/api/v1/admin/workspaces/{workspace}/groups/{group}/members/{reader_id}"
        assert (await admin.put(membership, headers=ah)).status_code == 200
        source = b"synthetic file"
        def store():
            adapter = storage()
            try:
                adapter.put(adapter.originals_bucket, key, source, "text/plain")
            finally:
                adapter.client.close()
        await run_in_threadpool(store)
        async with session_factory()() as db:
            db.add(Document(id=doc_id, owner_id=curator_id, scope="knowledge", title="Курс"))
            await db.flush()
            db.add(DocumentVersion(id=version_id, document_id=doc_id, generation=1,
                source_sha256=hashlib.sha256(source).hexdigest(), media_type="text/plain",
                size_bytes=len(source), original_key=key, canonical_key="canonical",
                canonical_sha256="b" * 64, approved_canonical_sha256="b" * 64,
                pipeline_version="canonical-v1", status="ready_for_review"))
            await db.flush()
            db.add(DisclosureUnit(id=unit_id, version_id=version_id, block_id=uuid.uuid4(),
                text="Python материалы", text_sha256="c" * 64, locator={"line": 1},
                policy_revision=1))
            document = await db.get(Document, doc_id)
            document.active_version_id = version_id
            await db.commit()
        bind_url = f"/api/v1/knowledge/documents/{doc_id}/workspace"
        assert (await reader.put(bind_url, headers=rh, json={
            "expected_revision": 1, "workspace_id": str(workspace),
        })).status_code == 404
        bound = await curator.put(bind_url, headers=ch, json={
            "expected_revision": 1, "workspace_id": str(workspace),
        })
        assert bound.status_code == 200 and bound.json()["revision"] == 2
        async with session_factory()() as db:
            db.add(DocumentGroupGrant(
                document_id=doc_id, group_id=foreign_group, workspace_id=workspace,
                allow_answer=True, allow_original=False,
            ))
            with pytest.raises(IntegrityError):
                await db.commit()
            await db.rollback()
        workspaces = await curator.get("/api/v1/knowledge/audience-workspaces")
        assert workspaces.status_code == 200
        assert workspaces.headers["cache-control"] == "private, no-store"
        assert [item["id"] for item in workspaces.json()["items"]] == [str(workspace)]
        group_list = f"/api/v1/knowledge/documents/{doc_id}/group-grants"
        groups = await curator.get(group_list)
        assert groups.status_code == 200 and groups.headers["cache-control"] == "private, no-store"
        assert groups.json()["items"][0]["id"] == str(group)
        group_url = f"/api/v1/knowledge/documents/{doc_id}/group-grants/{group}"
        body = {"expected_revision": 2, "allow_answer": True, "allow_original": False}
        assert (await curator.put(group_url.replace(str(group), str(foreign_group)),
                                  headers=ch, json=body)).status_code == 404
        granted = await curator.put(group_url, headers=ch, json=body)
        assert granted.status_code == 200 and granted.json()["revision"] == 3
        assert (await curator.get(
            group_list, params={"expected_acl_revision": 2}
        )).status_code == 409
        assert (await curator.get(group_list)).json()["items"][0]["allow_answer"]
        search = "/api/v1/knowledge/search"
        async def read():
            return (await reader.post(search, headers=rh, json={"query": "Python"})).json()["items"]
        assert len(await read()) == 1
        original = f"/api/v1/knowledge/versions/{version_id}/original/download"
        assert (await reader.post(original, headers=rh)).status_code == 404
        deny_url = f"/api/v1/knowledge/versions/{version_id}/units/{unit_id}/denies/{reader_id}"
        assert (await curator.put(deny_url, headers=ch,
                                 json={"expected_revision": 3, "denied": True})).status_code == 200
        assert await read() == []
        assert (await curator.put(deny_url, headers=ch,
                                 json={"expected_revision": 4, "denied": False})).status_code == 200
        grant = await curator.put(group_url, headers=ch,
                                  json={"expected_revision": 5, "allow_answer": True,
                                        "allow_original": True})
        assert grant.status_code == 200
        assert (await reader.post(original, headers=rh)).content == source
        original_only = await curator.put(
            group_url, headers=ch, json={"expected_revision": 6, "allow_answer": False,
                                        "allow_original": True},
        )
        assert original_only.status_code == 200 and original_only.json()["revision"] == 7
        assert await read() == []
        assert (await reader.post(original, headers=rh)).content == source
        both = await curator.put(
            group_url, headers=ch, json={"expected_revision": 7, "allow_answer": True,
                                        "allow_original": True},
        )
        assert both.status_code == 200
        extra_response = await admin.post(
            f"/api/v1/admin/workspaces/{workspace}/groups", headers=ah, json={"name": "Работники"}
        )
        extra_group = uuid.UUID(extra_response.json()["id"])
        group_ids.append(extra_group)
        extra_member = (
            f"/api/v1/admin/workspaces/{workspace}/groups/{extra_group}/members/{reader_id}"
        )
        assert (await admin.put(extra_member, headers=ah)).status_code == 200
        extra_grant = await curator.put(
            f"/api/v1/knowledge/documents/{doc_id}/group-grants/{extra_group}", headers=ch,
            json={"expected_revision": 8, "allow_answer": True, "allow_original": False},
        )
        assert extra_grant.status_code == 200
        assert (await admin.delete(membership, headers=ah)).status_code == 200
        assert len(await read()) == 1
        assert (await reader.post(original, headers=rh)).status_code == 404
        assert (await admin.delete(extra_member, headers=ah)).status_code == 200
        assert await read() == []
        assert (await admin.put(membership, headers=ah)).status_code == 200
        real_storage = ingest.storage
        loaded, resume = threading.Event(), threading.Event()

        class DelayedStorage:
            def __init__(self):
                self.adapter = real_storage()
                self.client = self.adapter.client
                self.originals_bucket = self.adapter.originals_bucket

            def get(self, bucket, item_key):
                value = self.adapter.get(bucket, item_key)
                loaded.set()
                assert resume.wait(timeout=10)
                return value

        with monkeypatch.context() as context:
            context.setattr(ingest, "storage", DelayedStorage)
            download = asyncio.create_task(reader.post(original, headers=rh))
            try:
                assert await asyncio.to_thread(loaded.wait, 10)
                assert (await admin.delete(membership, headers=ah)).status_code == 200
            finally:
                resume.set()
            assert (await download).status_code == 404
        assert (await admin.put(membership, headers=ah)).status_code == 200
        real_lock = retrieval.lock_authorized_document
        async def revoke_before_final(db, actor, identifier, action):
            assert (await admin.delete(membership, headers=ah)).status_code == 200
            return await real_lock(db, actor, identifier, action)
        with monkeypatch.context() as context:
            context.setattr(retrieval, "lock_authorized_document", revoke_before_final)
            assert await read() == []
        assert (await reader.post(original, headers=rh)).status_code == 404
    finally:
        def remove():
            adapter = storage()
            try:
                adapter.delete(adapter.originals_bucket, key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            await db.execute(
                delete(DisclosureAnswerDeny).where(DisclosureAnswerDeny.unit_id == unit_id)
            )
            await db.execute(
                delete(DocumentGroupGrant).where(DocumentGroupGrant.document_id == doc_id)
            )
            document = await db.get(Document, doc_id)
            if document:
                document.active_version_id = None
                document.last_published_version_id = None
                await db.flush()
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            await db.execute(delete(DocumentVersion).where(DocumentVersion.id == version_id))
            await db.execute(delete(Document).where(Document.id == doc_id))
            await db.execute(delete(GroupMember).where(GroupMember.group_id.in_(group_ids)))
            await db.execute(delete(Group).where(Group.id.in_(group_ids)))
            await db.execute(delete(Membership).where(Membership.workspace_id.in_(workspace_ids)))
            await db.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
            await db.commit()


@pytest.mark.storage
async def test_group_unit_deny_overrides_direct_grant_and_requires_version_review(
    plane, monkeypatch
):
    import hashlib

    from starlette.concurrency import run_in_threadpool

    import partner_agent.retrieval.service as retrieval
    from partner_agent.documents.ingest import storage
    from partner_agent.infra.models import Group, GroupMember, Membership, Workspace
    from partner_agent.infra.models_documents import (
        DisclosureGroupDeny,
        DisclosureUnit,
        Document,
        DocumentGrant,
        DocumentGroupGrant,
        DocumentVersion,
    )

    admin, ah, _ = await plane(role="admin")
    curator, ch, curator_id = await plane(role="curator")
    reader, rh, reader_id = await plane(role="user")
    wid, gid, did, first, second, u1, u2 = [uuid.uuid4() for _ in range(7)]
    foreign_wid, foreign_gid = uuid.uuid4(), uuid.uuid4()
    extra_group_ids = [uuid.uuid4() for _ in range(101)]
    source = b"synthetic group original"
    key = f"{first}/original"
    try:
        def put():
            adapter = storage()
            try:
                adapter.put(adapter.originals_bucket, key, source, "text/plain")
            finally:
                adapter.client.close()
        await run_in_threadpool(put)
        async with session_factory()() as db:
            db.add_all([Workspace(id=wid, name="Учебный центр"),
                        Workspace(id=foreign_wid, name="Другая организация")])
            await db.flush()
            db.add_all([Membership(workspace_id=wid, user_id=curator_id, role="member"),
                        Membership(workspace_id=wid, user_id=reader_id, role="member"),
                        Group(id=gid, workspace_id=wid, name="Ученики"),
                        Group(id=foreign_gid, workspace_id=foreign_wid, name="Чужая группа"),
                        Document(id=did, owner_id=curator_id, workspace_id=wid,
                                 scope="knowledge", title="Курс")])
            await db.flush()
            db.add(GroupMember(group_id=gid, user_id=reader_id, workspace_id=wid))
            db.add(DocumentGrant(document_id=did, user_id=reader_id,
                                 allow_answer=True, allow_original=True))
            for generation, vid in enumerate((first, second), start=1):
                db.add(DocumentVersion(id=vid, document_id=did, generation=generation,
                    source_sha256=hashlib.sha256(source).hexdigest(), media_type="text/plain",
                    size_bytes=len(source),
                    original_key=f"{vid}/original", canonical_key=f"{vid}/canonical",
                    canonical_sha256="b" * 64, approved_canonical_sha256="b" * 64,
                    pipeline_version="canonical-v1", status="ready_for_review",
                    index_manifest={"state": "verified", "version_id": str(vid),
                                    "policy_revision": 1, "unit_count": 1}))
            await db.flush()
            for vid, uid in ((first, u1), (second, u2)):
                db.add(DisclosureUnit(id=uid, version_id=vid, block_id=uuid.uuid4(),
                    text="Python обучение", text_sha256="c" * 64, locator={"line": 1},
                    policy_revision=1))
            doc = await db.get(Document, did)
            doc.active_version_id = first
            doc.last_published_version_id = first
            await db.commit()
        for group, version, constraint in (
            (foreign_gid, first, "fk_group_deny_group_workspace"),
            (gid, second, "fk_group_deny_unit_version"),
        ):
            async with session_factory()() as db:
                db.add(DisclosureGroupDeny(
                    unit_id=u1, group_id=group, version_id=version,
                    document_id=did, workspace_id=wid,
                ))
                with pytest.raises(IntegrityError) as failure:
                    await db.commit()
                assert constraint in str(failure.value.orig)
                await db.rollback()
        search = "/api/v1/knowledge/search"
        async def read():
            return (await reader.post(search, headers=rh, json={"query": "Python"})).json()["items"]
        assert len(await read()) == 1
        deny1 = f"/api/v1/knowledge/versions/{first}/units/{u1}/group-denies/{gid}"
        foreign_deny = deny1.replace(str(gid), str(foreign_gid))
        assert (await curator.put(
            foreign_deny, headers=ch, json={"expected_revision": 1, "denied": True}
        )).status_code == 404
        assert (await reader.put(deny1, headers=rh,
                                 json={"expected_revision": 1, "denied": True})).status_code == 404
        assert (await curator.put(
            deny1, json={"expected_revision": 1, "denied": True}
        )).status_code == 403
        real_lock = retrieval.lock_authorized_document
        async def deny_before_final(db, actor, identifier, action):
            response = await curator.put(
                deny1, headers=ch, json={"expected_revision": 1, "denied": True}
            )
            assert response.status_code == 200
            return await real_lock(db, actor, identifier, action)
        with monkeypatch.context() as context:
            context.setattr(retrieval, "lock_authorized_document", deny_before_final)
            assert await read() == []
        denied = await curator.put(deny1, headers=ch, json={"expected_revision": 2, "denied": True})
        assert denied.status_code == 200 and denied.json()["revision"] == 2
        listing = await curator.get(f"/api/v1/knowledge/versions/{first}/group-denies/{gid}/units")
        assert listing.status_code == 200
        assert listing.headers["cache-control"] == "private, no-store"
        assert listing.json()["items"][0]["denied"]
        async with session_factory()() as db:
            reader_login = (await db.get(User, reader_id)).normalized_login
        access_url = f"/api/v1/knowledge/documents/{did}/access-review"
        access_body = {"login": reader_login, "version_id": str(first)}
        explanation = await curator.post(access_url, headers=ch, json=access_body)
        assert explanation.status_code == 200
        assert explanation.json()["policy_allows_answer"]
        assert explanation.json()["policy_allows_original"]
        assert explanation.json()["groups"][0]["id"] == str(gid)
        explained_unit = explanation.json()["units"][0]
        assert explained_unit["group_denied"] and not explained_unit["policy_allows_answer"]
        assert explained_unit["denying_groups"] == [{"id": str(gid), "name": "Ученики"}]
        token = explanation.json()["snapshot_token"]
        assert token and explanation.json()["membership_snapshot_checked"]
        member_url = f"/api/v1/admin/workspaces/{wid}/groups/{gid}/members/{reader_id}"
        assert (await admin.delete(member_url, headers=ah)).status_code == 200
        changed_review = await curator.post(access_url, headers=ch, json={
            **access_body, "snapshot_token": token,
        })
        assert changed_review.status_code == 409
        assert (await admin.put(member_url, headers=ah)).status_code == 200
        async with session_factory()() as db:
            db.add_all([Group(id=item, workspace_id=wid, name="Дополнительная")
                        for item in extra_group_ids])
            await db.flush()
            db.add_all([GroupMember(group_id=item, user_id=reader_id, workspace_id=wid)
                        for item in extra_group_ids])
            db.add_all([DisclosureGroupDeny(
                unit_id=u1, group_id=item, version_id=first, document_id=did, workspace_id=wid
            )
                        for item in extra_group_ids[:11]])
            await db.execute(delete(DocumentGrant).where(
                DocumentGrant.document_id == did, DocumentGrant.user_id == reader_id
            ))
            grant_group = max([gid, *extra_group_ids])
            db.add(DocumentGroupGrant(document_id=did, group_id=grant_group, workspace_id=wid,
                                      allow_answer=True, allow_original=False))
            await db.commit()
        bounded = (await curator.post(access_url, headers=ch, json=access_body)).json()
        assert len(bounded["groups"]) == 100 and bounded["groups_incomplete"]
        assert bounded["snapshot_token"] is None and not bounded["membership_snapshot_checked"]
        assert all(group["id"] != str(grant_group) for group in bounded["groups"])
        assert bounded["policy_allows_answer"]
        assert len(bounded["units"][0]["denying_groups"]) == 10
        assert bounded["units"][0]["denying_groups_incomplete"]
        assert not bounded["units"][0]["policy_allows_answer"]
        async with session_factory()() as db:
            await db.execute(
                delete(DocumentGroupGrant).where(DocumentGroupGrant.document_id == did)
            )
            db.add(DocumentGrant(document_id=did, user_id=reader_id,
                                 allow_answer=True, allow_original=True))
            await db.execute(delete(DisclosureGroupDeny).where(
                DisclosureGroupDeny.group_id.in_(extra_group_ids)
            ))
            await db.execute(delete(GroupMember).where(GroupMember.group_id.in_(extra_group_ids)))
            await db.execute(delete(Group).where(Group.id.in_(extra_group_ids)))
            await db.commit()
        async with session_factory()() as db:
            with pytest.raises(IntegrityError):
                await db.execute(delete(DisclosureUnit).where(DisclosureUnit.id == u1))
            await db.rollback()
        assert await read() == []
        original = await reader.post(
            f"/api/v1/knowledge/versions/{first}/original/download", headers=rh,
        )
        assert original.status_code == 200 and original.content == source
        withdraw = await curator.post(f"/api/v1/knowledge/documents/{did}/unpublish", headers=ch,
                                      json={"expected_revision": 2})
        assert withdraw.status_code == 200
        publish = f"/api/v1/knowledge/versions/{second}/publish"
        blocked = await curator.post(publish, headers=ch, json={"expected_revision": 3})
        assert blocked.status_code == 409 and blocked.json()["code"] == "deny_review_required"
        deny2 = f"/api/v1/knowledge/versions/{second}/units/{u2}/group-denies/{gid}"
        assert (await curator.put(deny2, headers=ch,
                                 json={"expected_revision": 3, "denied": True})).status_code == 200
        report = await curator.get(f"/api/v1/knowledge/documents/{did}/publication-acl-review",
                                   params={"target_version_id": str(second)})
        assert report.status_code == 200
        assert report.json()["previous"][0]["subject_type"] == "group"
        assert report.json()["target"][0]["subject_type"] == "group"
        assert report.json()["incomplete"] is False
        curator_membership = f"/api/v1/admin/workspaces/{wid}/members/{curator_id}"
        assert (await admin.delete(curator_membership, headers=ah)).status_code == 200
        hidden_report = await curator.get(
            f"/api/v1/knowledge/documents/{did}/publication-acl-review",
            params={"target_version_id": str(second)},
        )
        assert hidden_report.status_code == 403
        assert (await curator.post(publish, headers=ch, json={
            "expected_revision": 4, "confirm_deny_review": True,
        })).status_code == 403
        assert (await admin.put(curator_membership, headers=ah)).status_code == 200
        assert (await curator.post(publish, headers=ch, json={
            "expected_revision": 4, "confirm_deny_review": True,
        })).status_code == 200
        assert await read() == []
        assert (await admin.delete(curator_membership, headers=ah)).status_code == 200
        hidden = await curator.post(access_url, headers=ch, json={
            "login": reader_login, "version_id": str(second),
        })
        assert hidden.status_code == 200 and hidden.json()["groups_hidden"]
        assert not hidden.json()["membership_snapshot_checked"]
        assert hidden.json()["groups"] == []
        assert hidden.json()["units"][0]["group_denied"]
        assert hidden.json()["units"][0]["denying_groups"] == []
        assert (await admin.put(curator_membership, headers=ah)).status_code == 200
        member_url = f"/api/v1/admin/workspaces/{wid}/groups/{gid}/members/{reader_id}"
        assert (await admin.delete(member_url, headers=ah)).status_code == 200
        assert len(await read()) == 1  # Direct grant survives removal from denied group.
        explanation = await curator.post(access_url, headers=ch, json={
            "login": reader_login, "version_id": str(second),
        })
        assert explanation.json()["groups"] == []
        assert explanation.json()["units"][0]["policy_allows_answer"]
    finally:
        def remove():
            adapter = storage()
            try:
                adapter.delete(adapter.originals_bucket, key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            await db.execute(
                delete(DocumentGroupGrant).where(DocumentGroupGrant.document_id == did)
            )
            await db.execute(delete(DisclosureGroupDeny).where(
                DisclosureGroupDeny.group_id.in_(extra_group_ids)
            ))
            await db.execute(delete(GroupMember).where(GroupMember.group_id.in_(extra_group_ids)))
            await db.execute(delete(Group).where(Group.id.in_(extra_group_ids)))
            await db.execute(delete(DisclosureGroupDeny).where(DisclosureGroupDeny.group_id == gid))
            await db.execute(delete(DocumentGrant).where(DocumentGrant.document_id == did))
            doc = await db.get(Document, did)
            if doc:
                doc.active_version_id = None
                doc.last_published_version_id = None
                await db.flush()
            await db.execute(
                delete(DisclosureUnit).where(DisclosureUnit.version_id.in_([first, second]))
            )
            await db.execute(delete(DocumentVersion).where(DocumentVersion.document_id == did))
            await db.execute(delete(Document).where(Document.id == did))
            await db.execute(delete(GroupMember).where(GroupMember.group_id == gid))
            await db.execute(delete(Group).where(Group.id == gid))
            await db.execute(delete(Group).where(Group.id == foreign_gid))
            await db.execute(delete(Membership).where(Membership.workspace_id == wid))
            await db.execute(delete(Workspace).where(Workspace.id == wid))
            await db.execute(delete(Workspace).where(Workspace.id == foreign_wid))
            await db.commit()


async def test_lexical_search_scans_past_first_page_and_reports_scope_limit(plane, monkeypatch):
    import partner_agent.retrieval.service as retrieval
    from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion

    owner, headers, owner_id = await plane(role="curator")
    stranger, stranger_headers, _ = await plane(role="user")
    document_id, version_id = uuid.uuid4(), uuid.uuid4()
    base = uuid.uuid4().int & ~((1 << 16) - 1)
    try:
        async with session_factory()() as db:
            document = Document(id=document_id, owner_id=owner_id, scope="knowledge", title="QA")
            db.add(document)
            await db.flush()
            db.add(DocumentVersion(
                id=version_id, document_id=document_id, generation=1, source_sha256="a" * 64,
                media_type="text/plain", size_bytes=1, original_key=f"{version_id}/original",
                canonical_key="canonical", canonical_sha256="b" * 64,
                approved_canonical_sha256="b" * 64, pipeline_version="canonical-v1",
                status="ready_for_review",
            ))
            await db.flush()
            db.add_all([
                DisclosureUnit(
                    id=uuid.UUID(int=base + number), version_id=version_id, block_id=uuid.uuid4(),
                    text="курс EDU-204" if number == 1001 else "курс EDU-2040",
                    text_sha256="c" * 64, locator={"line": number}, policy_revision=1,
                )
                for number in range(1, 1002)
            ])
            document.active_version_id = version_id
            await db.commit()
        url = "/api/v1/knowledge/search"
        body = {"query": "курс EDU-204"}
        with monkeypatch.context() as context:
            context.setattr(retrieval, "MAX_SCAN_CANDIDATES", 1000)
            limited = await owner.post(url, headers=headers, json=body)
            assert limited.status_code == 503
            assert limited.json()["code"] == "search_scope_too_large"
        result = await owner.post(url, headers=headers, json=body)
        assert result.status_code == 200
        assert len(result.json()["items"]) == 1
        assert result.json()["items"][0]["excerpt"] == "курс EDU-204"
        assert result.json()["candidate_limit"] == 10000
        denied = await stranger.post(url, headers=stranger_headers, json=body)
        assert denied.status_code == 200 and denied.json()["items"] == []
    finally:
        async with session_factory()() as db:
            document = await db.get(Document, document_id)
            if document:
                document.active_version_id = None
                await db.flush()
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            await db.execute(delete(DocumentVersion).where(DocumentVersion.id == version_id))
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


@pytest.mark.parametrize("change", ["disable", "demote", "tombstone"])
@pytest.mark.storage
async def test_upload_rechecks_owner_and_document_after_garage_write(plane, monkeypatch, change):
    from starlette.concurrency import run_in_threadpool

    import partner_agent.documents.ingest as ingest
    from partner_agent.infra.models_documents import Document, DocumentVersion

    client, headers, owner_id = await plane(role="curator")
    created = await client.post(
        "/api/v1/knowledge/documents", headers=headers, json={"title": "Synthetic upload race"}
    )
    document_id = uuid.UUID(created.json()["id"])
    real_store = ingest.store_original
    stored_keys = []

    async def change_after_write(key, data, media_type):
        await real_store(key, data, media_type)
        stored_keys.append(key)
        async with session_factory()() as db:
            if change == "tombstone":
                await db.execute(update(Document).where(Document.id == document_id).values(
                    deleted_at=utcnow()
                ))
            else:
                values = {"disabled": True} if change == "disable" else {"role": "user"}
                await db.execute(update(User).where(User.id == owner_id).values(**values))
            await db.commit()

    try:
        with monkeypatch.context() as context:
            context.setattr(ingest, "store_original", change_after_write)
            result = await client.post(
                f"/api/v1/knowledge/documents/{document_id}/versions?expected_revision=1",
                headers={**headers, "Content-Type": "text/plain"}, content=b"synthetic race",
            )
        assert result.status_code == (404 if change == "tombstone" else 403)
        assert "id" not in result.json()
        assert len(stored_keys) == 1
        async with session_factory()() as db:
            version = await db.scalar(
                select(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            assert version is not None and version.original_key == stored_keys[0]
    finally:
        def remove():
            adapter = ingest.storage()
            try:
                for key in stored_keys:
                    adapter.delete(adapter.originals_bucket, key)
            finally:
                adapter.client.close()
        await run_in_threadpool(remove)
        async with session_factory()() as db:
            await db.execute(
                delete(DocumentVersion).where(DocumentVersion.document_id == document_id)
            )
            await db.execute(delete(Document).where(Document.id == document_id))
            await db.commit()


async def test_direct_subject_directory_includes_deny_only_and_guards_pages(plane):
    from partner_agent.infra.models_documents import (
        DisclosureAnswerDeny,
        DisclosureUnit,
        Document,
        DocumentGrant,
        DocumentVersion,
    )

    owner, headers, owner_id = await plane(role="curator")
    reader, reader_headers, reader_id = await plane(role="user")
    _, _, denied_id = await plane(role="user")
    admin, _, _ = await plane(role="admin")
    doc_id, foreign_id, first, second, unit_id, foreign_unit = [uuid.uuid4() for _ in range(6)]
    try:
        async with session_factory()() as db:
            db.add_all([
                Document(id=doc_id, owner_id=owner_id, scope="knowledge", title="One"),
                Document(id=foreign_id, owner_id=owner_id, scope="knowledge", title="Two"),
            ])
            await db.flush()
            for document_id, version_id in ((doc_id, first), (foreign_id, second)):
                db.add(DocumentVersion(
                    id=version_id, document_id=document_id, generation=1, source_sha256="a" * 64,
                    media_type="text/plain", size_bytes=1, original_key=f"{version_id}/original",
                    pipeline_version="canonical-v1", status="pending",
                ))
            await db.flush()
            for version_id, uid in ((first, unit_id), (second, foreign_unit)):
                db.add(DisclosureUnit(
                    id=uid, version_id=version_id, block_id=uuid.uuid4(), text="synthetic",
                    text_sha256="b" * 64, locator={"line": 1}, policy_revision=1,
                ))
            await db.flush()
            db.add_all([
                DocumentGrant(document_id=doc_id, user_id=reader_id,
                              allow_answer=True, allow_original=False),
                DisclosureAnswerDeny(unit_id=unit_id, user_id=denied_id),
                DisclosureAnswerDeny(unit_id=foreign_unit, user_id=owner_id),
            ])
            await db.execute(update(User).where(User.id == denied_id).values(disabled=True))
            await db.commit()
        url = f"/api/v1/knowledge/documents/{doc_id}/direct-subjects"
        assert (await reader.get(url)).status_code == 404
        assert (await admin.get(url)).status_code == 404
        first_page = await owner.get(url, params={"limit": 1})
        assert first_page.status_code == 200
        assert first_page.headers["cache-control"] == "private, no-store"
        data = first_page.json()
        assert data["next_after"] and data["acl_revision"] == 1
        other_page = await owner.get(url, params={
            "limit": 1, "after": data["next_after"], "expected_acl_revision": 1,
        })
        subjects = data["items"] + other_page.json()["items"]
        assert {item["user_id"] for item in subjects} == {str(reader_id), str(denied_id)}
        denied_user = next(item for item in subjects if item["user_id"] == str(denied_id))
        assert denied_user["disabled"] and denied_user["has_denies"]
        assert not denied_user["allow_answer"] and not denied_user["allow_original"]
        legacy = await owner.get(f"/api/v1/knowledge/documents/{doc_id}/grants")
        assert [item["user_id"] for item in legacy.json()["items"]] == [str(reader_id)]
        revoke = await owner.put(
            f"/api/v1/knowledge/versions/{first}/units/{unit_id}/denies/{denied_id}",
            headers=headers, json={"expected_revision": 1, "denied": False},
        )
        assert revoke.status_code == 200
        stale = await owner.get(url, params={
            "limit": 1, "after": data["next_after"], "expected_acl_revision": 1,
        })
        assert stale.status_code == 409
        current = await owner.get(url)
        assert [item["user_id"] for item in current.json()["items"]] == [str(reader_id)]
    finally:
        async with session_factory()() as db:
            await db.execute(delete(DisclosureAnswerDeny).where(
                DisclosureAnswerDeny.unit_id.in_([unit_id, foreign_unit])
            ))
            await db.execute(delete(DocumentGrant).where(DocumentGrant.document_id == doc_id))
            await db.execute(
                delete(DisclosureUnit).where(DisclosureUnit.id.in_([unit_id, foreign_unit]))
            )
            await db.execute(delete(DocumentVersion).where(DocumentVersion.id.in_([first, second])))
            await db.execute(delete(Document).where(Document.id.in_([doc_id, foreign_id])))
            await db.commit()


async def test_document_group_members_are_scoped_and_revision_guarded(plane, monkeypatch):
    import partner_agent.documents.audience as audience_service
    from partner_agent.auth.audience_lock import audience_read_lock
    from partner_agent.infra.models import Group, GroupMember, Membership, Workspace
    from partner_agent.infra.models_documents import (
        DisclosureAnswerDeny,
        DisclosureGroupDeny,
        DisclosureUnit,
        Document,
        DocumentGrant,
        DocumentGroupGrant,
        DocumentVersion,
    )

    admin, ah, _ = await plane(role="admin")
    owner, oh, owner_id = await plane(role="curator")
    reader, _, reader_id = await plane(role="user")
    _, _, other_id = await plane(role="user")
    _, _, denied_only_id = await plane(role="user")
    _, _, unrelated_user_id = await plane(role="user")
    wid, foreign_wid, gid, deny_gid, unrelated_gid, foreign_gid, did, vid, uid = [
        uuid.uuid4() for _ in range(9)
    ]
    gids = [gid, deny_gid, unrelated_gid, foreign_gid]
    try:
        async with session_factory()() as db:
            db.add_all([Workspace(id=wid, name="QA"), Workspace(id=foreign_wid, name="Foreign")])
            await db.flush()
            db.add_all([
                Group(id=group_id, workspace_id=foreign_wid if group_id == foreign_gid else wid,
                      name="Synthetic") for group_id in gids
            ])
            db.add(Document(id=did, owner_id=owner_id, workspace_id=wid,
                            scope="knowledge", title="Synthetic"))
            await db.flush()
            db.add(DocumentVersion(id=vid, document_id=did, generation=1, source_sha256="a" * 64,
                media_type="text/plain", size_bytes=1, original_key=f"{vid}/original",
                pipeline_version="canonical-v1", status="pending"))
            await db.flush()
            db.add(DisclosureUnit(id=uid, version_id=vid, block_id=uuid.uuid4(), text="synthetic",
                                  text_sha256="b" * 64, locator={"line": 1}, policy_revision=1))
            db.add(DocumentGroupGrant(document_id=did, group_id=gid, workspace_id=wid,
                                      allow_answer=True, allow_original=False))
            db.add(DocumentGrant(document_id=did, user_id=other_id,
                                 allow_answer=False, allow_original=True))
            await db.flush()
            db.add(DisclosureGroupDeny(unit_id=uid, group_id=deny_gid, version_id=vid,
                                       document_id=did, workspace_id=wid))
            db.add(DisclosureAnswerDeny(unit_id=uid, user_id=denied_only_id))
            await db.execute(update(User).where(User.id == denied_only_id).values(disabled=True))
            await db.commit()
        for user_id in (owner_id, reader_id, other_id, unrelated_user_id):
            response = await admin.put(f"/api/v1/admin/workspaces/{wid}/members/{user_id}",
                                       headers=ah)
            assert response.status_code == 200
        for group_id, user_id in ((gid, reader_id), (gid, other_id), (deny_gid, reader_id),
                                  (unrelated_gid, unrelated_user_id)):
            url = f"/api/v1/admin/workspaces/{wid}/groups/{group_id}/members/{user_id}"
            assert (await admin.put(url, headers=ah)).status_code == 200
        base = f"/api/v1/knowledge/documents/{did}/group-grants"
        url = f"{base}/{gid}/members"
        assert (await admin.get(url)).status_code == 404
        assert (await reader.get(url)).status_code == 404
        assert (await owner.get(f"{base}/{foreign_gid}/members")).status_code == 404
        assert (await owner.get(f"{base}/{unrelated_gid}/members")).status_code == 404
        assert (await owner.get(f"{base}/{deny_gid}/members")).status_code == 200
        group_page = await owner.get(base)
        deny_group = next(
            item for item in group_page.json()["items"] if item["id"] == str(deny_gid)
        )
        assert deny_group["has_denies"] and not deny_group["allow_answer"]
        audience_url = f"/api/v1/knowledge/documents/{did}/audience"
        assert (await admin.get(audience_url)).status_code == 404
        assert (await reader.get(audience_url)).status_code == 404
        audience_first = await owner.get(audience_url, params={"limit": 1})
        assert audience_first.status_code == 200
        assert audience_first.headers["cache-control"] == "private, no-store"
        audience_data = audience_first.json()
        audience_token = audience_data["snapshot_token"]
        assert audience_token and audience_data["next_after"]
        assert (await owner.get(audience_url, params={
            "after": audience_data["next_after"],
        })).status_code == 422
        audience_items = list(audience_data["items"])
        next_audience = audience_data["next_after"]
        while next_audience:
            page = await owner.get(audience_url, params={
                "limit": 1, "after": next_audience, "snapshot_token": audience_token,
            })
            assert page.status_code == 200
            audience_items.extend(page.json()["items"])
            next_audience = page.json()["next_after"]
        assert {item["user_id"] for item in audience_items} == {
            str(owner_id), str(reader_id), str(other_id), str(denied_only_id),
        }
        by_user = {item["user_id"]: item for item in audience_items}
        assert by_user[str(owner_id)]["owner"] and by_user[str(owner_id)]["policy_allows_original"]
        assert by_user[str(reader_id)]["group_answer"] and by_user[str(reader_id)]["has_denies"]
        assert by_user[str(other_id)]["group_answer"] and by_user[str(other_id)]["direct_original"]
        assert by_user[str(denied_only_id)]["disabled"]
        assert by_user[str(denied_only_id)]["has_denies"]
        assert not by_user[str(denied_only_id)]["policy_allows_answer"]
        with monkeypatch.context() as context:
            context.setattr(audience_service, "MAX_RELATED_GROUPS", 1)
            too_large = await owner.get(audience_url)
            assert too_large.status_code == 409
            assert too_large.json()["code"] == "audience_scope_too_large"
        unrelated_member = (
            f"/api/v1/admin/workspaces/{wid}/groups/{unrelated_gid}/members/{unrelated_user_id}"
        )
        assert (await admin.delete(unrelated_member, headers=ah)).status_code == 200
        assert (await owner.get(audience_url, params={
            "after": audience_data["next_after"], "snapshot_token": audience_token,
        })).status_code == 200
        assert (await admin.put(unrelated_member, headers=ah)).status_code == 200
        first_page = await owner.get(url, params={"limit": 1})
        assert first_page.status_code == 200
        assert first_page.headers["cache-control"] == "private, no-store"
        first = first_page.json()
        assert first["next_after"] and first["membership_revision"] == 3
        # Idempotent additions do not invalidate a membership snapshot.
        member_url = f"/api/v1/admin/workspaces/{wid}/groups/{gid}/members/{reader_id}"
        assert (await admin.put(member_url, headers=ah)).status_code == 200
        second_page = await owner.get(url, params={
            "limit": 1, "after": first["next_after"],
            "expected_acl_revision": first["acl_revision"],
            "expected_membership_revision": first["membership_revision"],
        })
        assert second_page.status_code == 200
        assert {first["items"][0]["user_id"], second_page.json()["items"][0]["user_id"]} == {
            str(reader_id), str(other_id)
        }
        async with session_factory()() as db:
            await db.execute(update(User).where(User.id == other_id).values(disabled=True))
            await db.commit()
        statuses = await owner.get(url)
        assert next(item for item in statuses.json()["items"]
                    if item["user_id"] == str(other_id))["disabled"]
        async with session_factory()() as db:
            await db.execute(update(User).where(User.id == other_id).values(disabled=False))
            await db.commit()
        # Removing workspace membership removes both group memberships and advances both revisions.
        assert (await admin.delete(f"/api/v1/admin/workspaces/{wid}/members/{reader_id}",
                                   headers=ah)).status_code == 200
        stale = await owner.get(url, params={
            "after": first["next_after"], "expected_acl_revision": first["acl_revision"],
            "expected_membership_revision": first["membership_revision"],
        })
        assert stale.status_code == 409
        stale_audience = await owner.get(audience_url, params={
            "after": audience_data["next_after"], "snapshot_token": audience_token,
        })
        assert stale_audience.status_code == 409
        async with session_factory()() as db:
            assert (await db.get(Group, gid)).membership_revision == 4
            assert (await db.get(Group, deny_gid)).membership_revision == 3
        other_member = f"/api/v1/admin/workspaces/{wid}/groups/{gid}/members/{other_id}"
        async with session_factory()() as db:
            await audience_read_lock(db)
            removal = asyncio.create_task(admin.delete(other_member, headers=ah))
            try:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(removal), timeout=0.2)
                assert (await db.get(Group, gid)).membership_revision == 4
            finally:
                await db.commit()
            assert (await removal).status_code == 200
        assert (await admin.put(other_member, headers=ah)).status_code == 200
        async with session_factory()() as db:
            assert (await db.get(Group, gid)).membership_revision == 6
        fresh_audience = (await owner.get(audience_url, params={"limit": 1})).json()
        grant = await owner.put(f"{base}/{gid}", headers=oh, json={
            "expected_revision": 1, "allow_answer": False, "allow_original": False,
        })
        assert grant.status_code == 200
        assert (await owner.get(audience_url, params={
            "after": fresh_audience["next_after"],
            "snapshot_token": fresh_audience["snapshot_token"],
        })).status_code == 409
        assert (await owner.get(url, params={"expected_acl_revision": 1})).status_code == 409
        assert (await owner.get(url)).status_code == 404
        assert (await admin.delete(f"/api/v1/admin/workspaces/{wid}/members/{owner_id}",
                                   headers=ah)).status_code == 200
        assert (await owner.get(url)).status_code == 403
        assert (await owner.get(audience_url)).status_code == 403
    finally:
        async with session_factory()() as db:
            await db.execute(
                delete(DisclosureAnswerDeny).where(DisclosureAnswerDeny.unit_id == uid)
            )
            await db.execute(delete(DisclosureGroupDeny).where(DisclosureGroupDeny.unit_id == uid))
            await db.execute(delete(DocumentGrant).where(DocumentGrant.document_id == did))
            await db.execute(
                delete(DocumentGroupGrant).where(DocumentGroupGrant.document_id == did)
            )
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.id == uid))
            await db.execute(delete(DocumentVersion).where(DocumentVersion.id == vid))
            await db.execute(delete(Document).where(Document.id == did))
            await db.execute(delete(GroupMember).where(GroupMember.group_id.in_(gids)))
            await db.execute(delete(Group).where(Group.id.in_(gids)))
            await db.execute(
                delete(Membership).where(Membership.workspace_id.in_([wid, foreign_wid]))
            )
            await db.execute(delete(Workspace).where(Workspace.id.in_([wid, foreign_wid])))
            await db.commit()
