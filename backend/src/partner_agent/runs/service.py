"""Transactional data-plane primitives. Callers own commit/rollback and chat→run locks."""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from partner_agent.auth.policy import Principal, Resource, authorize
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import User, utcnow
from partner_agent.infra.models_stage2 import (
    Admission,
    Chat,
    ChatBranch,
    ContextManifest,
    Message,
    Outbox,
    Run,
    RunAttempt,
    RunEvent,
)
from partner_agent.infra.settings import get_settings
from partner_agent.runs.types import RequestMode

TERMINAL = frozenset(("succeeded", "failed", "cancelled"))


@dataclass(frozen=True)
class Ownership:
    run_id: uuid.UUID
    chat_id: uuid.UUID
    branch_id: uuid.UUID
    owner_token: uuid.UUID
    fence: int


class StaleOwnership(RuntimeError):
    pass


async def server_now(db: AsyncSession) -> datetime:
    value = await db.scalar(select(func.clock_timestamp()))
    assert value is not None
    return cast(datetime, value)


async def lock_chat(db: AsyncSession, chat_id: uuid.UUID, user: User | None = None) -> Chat:
    chat = await db.scalar(select(Chat).where(Chat.id == chat_id).with_for_update())
    if chat is None or chat.deleted_at is not None:
        raise APIError(404, "not_found", "Chat not found")
    if user is not None and not authorize(
        Principal(user.id, user.role, user.disabled), "chat.read", Resource(owner_id=chat.owner_id)
    ):
        raise APIError(404, "not_found", "Chat not found")
    return chat


async def lock_run(db: AsyncSession, run_id: uuid.UUID) -> tuple[Chat, Run]:
    chat_id = await db.scalar(select(Run.chat_id).where(Run.id == run_id))
    if chat_id is None:
        raise APIError(404, "not_found", "Run not found")
    chat = await lock_chat(db, chat_id)
    run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run is None:
        raise APIError(404, "not_found", "Run not found")
    return chat, run


async def assert_ownership(db: AsyncSession, run: Run, ownership: Ownership) -> None:
    now = await server_now(db)
    chat = await db.get(Chat, run.chat_id)
    disabled = await db.scalar(select(User.disabled).where(User.id == run.user_id))
    if (
        run.id != ownership.run_id
        or run.chat_id != ownership.chat_id
        or run.branch_id != ownership.branch_id
        or run.owner_token != ownership.owner_token
        or run.fence != ownership.fence
        or run.status != "running"
        or run.lease_expires_at is None
        or run.lease_expires_at <= now
        or disabled is not False
        or chat is None
        or chat.deleted_at is not None
        or chat.active_run_id != run.id
    ):
        raise StaleOwnership("Run ownership is no longer valid")


def append_event(
    db: AsyncSession, chat: Chat, run: Run | None, event_type: str, payload: dict[str, Any]
) -> RunEvent:
    chat.event_seq += 1
    chat.updated_at = utcnow()
    event = RunEvent(
        chat_id=chat.id,
        seq=chat.event_seq,
        branch_id=run.branch_id if run else None,
        run_id=run.id if run else None,
        type=event_type,
        payload=payload,
        at=utcnow(),
    )
    db.add(event)
    return event


async def enqueue_dispatch(db: AsyncSession, run: Run) -> Outbox:
    key = f"{run.id}:{run.dispatch_generation}"
    existing = await db.scalar(select(Outbox).where(Outbox.logical_key == key))
    if existing is not None:
        return existing
    event = Outbox(
        id=uuid.uuid4(),
        logical_key=key,
        run_id=run.id,
        dispatch_generation=run.dispatch_generation,
        payload={"run_id": str(run.id), "chat_id": str(run.chat_id)},
        status="pending",
        next_attempt_at=utcnow(),
        attempts=0,
    )
    db.add(event)
    return event


async def dispatch_head(db: AsyncSession, chat: Chat) -> Run | None:
    if chat.deleted_at is not None or chat.active_run_id is not None:
        return None
    candidate = await db.scalar(
        select(Run)
        .where(Run.chat_id == chat.id, Run.status == "queued")
        .order_by(Run.queue_seq)
        .limit(1)
        .with_for_update()
    )
    if candidate is not None:
        chat.active_run_id = candidate.id
        await enqueue_dispatch(db, candidate)
    return candidate


def message_dto(message: Message) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "branch_id": str(message.branch_id),
        "run_id": str(message.run_id),
        "role": message.role,
        "origin": message.origin,
        "ordinal": message.ordinal,
        "status": message.status,
        "content": message.content,
        "revision": message.revision,
        "created_at": message.created_at.isoformat(),
    }


async def publish_prefix(
    db: AsyncSession, chat: Chat, run: Run, ownership: Ownership, target_text: str
) -> None:
    await assert_ownership(db, run, ownership)
    current = run.partial_text
    if current.startswith(target_text):
        return
    if not target_text.startswith(current):
        raise ValueError("Published text must extend the durable prefix")
    suffix = target_text[len(current) :]
    run.partial_text = target_text
    append_event(
        db,
        chat,
        run,
        "message.delta",
        {"text": suffix, "offset": len(current.encode("utf-16-le")) // 2, "offset_unit": "utf16"},
    )
    await db.flush()
    await assert_ownership(db, run, ownership)


async def finish_run(
    db: AsyncSession,
    chat: Chat,
    run: Run,
    status: str,
    outcome: dict[str, Any],
    *,
    ownership: Ownership | None = None,
    text: str | None = None,
) -> Run:
    if status not in TERMINAL:
        raise ValueError("Expected terminal state")
    if run.status in TERMINAL:
        return run
    lease = run.lease_expires_at
    if ownership is not None:
        await assert_ownership(db, run, ownership)
    now = await server_now(db)
    branch = await db.get(ChatBranch, run.branch_id)
    assert branch is not None
    latest_user = await db.scalar(
        select(Message)
        .where(Message.run_id == run.id, Message.role == "user")
        .order_by(Message.ordinal.desc())
        .limit(1)
    )
    if status == "succeeded":
        ordinal = await db.scalar(select(func.max(Message.ordinal)).where(Message.run_id == run.id))
        assistant = Message(
            id=uuid.uuid4(),
            chat_id=chat.id,
            branch_id=run.branch_id,
            run_id=run.id,
            role="assistant",
            status="completed",
            origin=str(outcome.get("profile", "offline_fixture")),
            content=run.partial_text if text is None else text,
            revision=1,
            ordinal=(ordinal or 0) + 1,
            created_at=now,
        )
        db.add(assistant)
        run.assistant_message_id = assistant.id
        run.partial_text = assistant.content
        branch.head_message_id = assistant.id
        append_event(db, chat, run, "message.completed", {"message": message_dto(assistant)})
    elif chat.active_run_id == run.id and latest_user is not None:
        # A cancelled partial is not a completed assistant/tool exchange.
        branch.head_message_id = latest_user.id
    branch.revision += 1
    if run.running_since is not None:
        run.active_duration_ms += max(0, int((now - run.running_since).total_seconds() * 1000))
    run.running_since = None
    run.status, run.outcome, run.finished_at = status, outcome, now
    run.owner_token, run.lease_expires_at = None, None
    run.fence += 1
    attempts = (
        await db.scalars(
            select(RunAttempt).where(RunAttempt.run_id == run.id, RunAttempt.status == "running")
        )
    ).all()
    for attempt in attempts:
        attempt.status, attempt.ended_at, attempt.lease_expires_at = status, now, None
    append_event(db, chat, run, f"run.{status}", {"status": status, "outcome": outcome})
    if chat.active_run_id == run.id:
        chat.active_run_id = None
        await db.flush()
        await dispatch_head(db, chat)
    await db.flush()
    if ownership is not None and (lease is None or await server_now(db) >= lease):
        raise StaleOwnership("Lease expired during finalization")
    return run


async def visible_messages(
    db: AsyncSession,
    branch: ChatBranch,
    *,
    up_to: uuid.UUID | None = None,
    include_all: bool = True,
) -> list[Message]:
    """Resolve ancestry prefixes; a fork can never import a sibling or ancestor suffix."""
    lineage: list[ChatBranch] = []
    current: ChatBranch | None = branch
    seen: set[uuid.UUID] = set()
    while current is not None:
        if current.id in seen or current.chat_id != branch.chat_id:
            raise APIError(409, "invalid_branch", "Invalid branch ancestry")
        seen.add(current.id)
        lineage.append(current)
        current = (
            await db.get(ChatBranch, current.parent_branch_id) if current.parent_branch_id else None
        )
    result: list[Message] = []
    for item in reversed(lineage):
        if item.parent_branch_id is not None:
            if item.fork_message_id is None:
                result = []
            else:
                positions = [
                    i for i, message in enumerate(result) if message.id == item.fork_message_id
                ]
                if not positions:
                    raise APIError(409, "invalid_branch", "Fork prefix is unavailable")
                result = result[: positions[0] + 1]
        own = (
            await db.scalars(
                select(Message)
                .join(Run, Message.run_id == Run.id)
                .where(Message.branch_id == item.id, Message.chat_id == branch.chat_id)
                .order_by(Run.queue_seq, Message.ordinal)
            )
        ).all()
        result.extend(own)
    if not include_all or up_to is not None:
        if up_to is None:
            return []
        positions = [i for i, message in enumerate(result) if message.id == up_to]
        if not positions:
            raise APIError(409, "invalid_frontier", "Branch frontier is unavailable")
        result = result[: positions[0] + 1]
    return result


async def build_context(db: AsyncSession, chat: Chat, run: Run) -> list[Message]:
    """Capture once under the chat lock, after the preceding run has committed."""
    existing = await db.scalar(select(ContextManifest).where(ContextManifest.run_id == run.id))
    if existing is not None:
        ids = [uuid.UUID(value) for value in existing.message_ids]
        rows = (await db.scalars(select(Message).where(Message.id.in_(ids)))).all() if ids else []
        by_id = {message.id: message for message in rows}
        if len(by_id) != len(ids):
            raise StaleOwnership("Context originals are unavailable")
        return [by_id[key] for key in ids]
    branch = await db.get(ChatBranch, run.branch_id)
    assert branch is not None
    messages = await visible_messages(db, branch, up_to=branch.head_message_id, include_all=False)
    # Only completed earlier turns; later queued input may already be persisted.
    earlier_run_ids = set(
        (
            await db.scalars(
                select(Run.id).where(
                    Run.chat_id == chat.id,
                    Run.queue_seq < run.queue_seq,
                )
            )
        ).all()
    )
    messages = [m for m in messages if m.run_id in earlier_run_ids]
    input_message = await db.get(Message, run.input_message_id)
    assert input_message is not None
    messages.append(input_message)
    db.add(
        ContextManifest(
            id=uuid.uuid4(),
            run_id=run.id,
            branch_id=run.branch_id,
            head_message_id=branch.head_message_id,
            message_ids=[str(m.id) for m in messages],
            message_revisions={str(m.id): m.revision for m in messages},
            evidence_ids=[],
            graph_version=run.graph_version,
        )
    )
    return messages


async def queue_position(db: AsyncSession, chat: Chat, run: Run) -> int:
    if run.status in TERMINAL or chat.active_run_id == run.id:
        return 0
    count = await db.scalar(
        select(func.count())
        .select_from(Run)
        .where(Run.chat_id == chat.id, Run.queue_seq < run.queue_seq, Run.status.not_in(TERMINAL))
    )
    return int(count or 0)


async def run_dto(db: AsyncSession, chat: Chat, run: Run) -> dict[str, Any]:
    from partner_agent.documents.chat_files import file_dto
    from partner_agent.infra.models_chat_files import ChatFile

    files = (await db.scalars(select(ChatFile).where(
        ChatFile.id.in_([uuid.UUID(value) for value in (run.attachment_ids or [])]),
        ChatFile.chat_id == chat.id, ChatFile.owner_id == chat.owner_id,
        ChatFile.deleted_at.is_(None),
    ))).all()
    return {
        "id": str(run.id),
        "input_message_id": str(run.input_message_id),
        "branch_id": str(run.branch_id),
        "status": run.status,
        "request_mode": run.request_mode,
        "queue_seq": run.queue_seq,
        "queue_position": await queue_position(db, chat, run),
        "partial_text": run.partial_text,
        "clarification_id": str(run.clarification_id) if run.clarification_id else None,
        "clarification_question": run.clarification_question,
        "clarification_options": run.clarification_options,
        "outcome": run.outcome,
        "attachments": [file_dto(item) for item in files],
        "runtime_profile": (run.outcome or {}).get("profile", get_settings().runtime_profile),
    }


def body_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


async def replay_admission(
    db: AsyncSession, chat: Chat, user: User, operation: str, key: str, body: dict[str, Any]
) -> dict[str, Any] | None:
    if not key or len(key) > 128 or not key.isascii() or any(ord(c) < 33 for c in key):
        raise APIError(422, "idempotency_key_required", "A valid Idempotency-Key is required")
    prior = await db.scalar(
        select(Admission).where(
            Admission.chat_id == chat.id,
            Admission.user_id == user.id,
            Admission.operation == operation,
            Admission.key == key,
        )
    )
    if prior is None:
        return None
    if prior.body_hash != body_hash(body):
        raise APIError(409, "idempotency_conflict", "Idempotency key belongs to another request")
    return prior.response


async def admit_run(
    db: AsyncSession,
    chat: Chat,
    branch: ChatBranch,
    user: User,
    content: str,
    *,
    origin: str = "user",
    attachment_ids: list[uuid.UUID] | None = None,
    request_mode: RequestMode = "auto",
) -> Run:
    from partner_agent.documents.chat_files import validate_selection

    await validate_selection(db, user, chat.id, attachment_ids or [])
    chat.queue_seq += 1
    run_id, message_id = uuid.uuid4(), uuid.uuid4()
    run = Run(
        id=run_id,
        chat_id=chat.id,
        branch_id=branch.id,
        user_id=user.id,
        input_message_id=message_id,
        status="queued",
        request_mode=request_mode,
        queue_seq=chat.queue_seq,
        graph_thread_id=str(run_id),
        graph_version=get_settings().runtime_graph_version,
        fence=0,
        input_applied=False,
        dispatch_generation=0,
        partial_text="",
        attachment_ids=[str(value) for value in (attachment_ids or [])],
        clarification_options=[],
        active_duration_ms=0,
        created_at=utcnow(),
    )
    db.add(run)
    await db.flush()
    db.add(
        Message(
            id=message_id,
            chat_id=chat.id,
            branch_id=branch.id,
            run_id=run_id,
            role="user",
            origin=origin,
            status="completed",
            content=content,
            ordinal=0,
            revision=1,
            created_at=utcnow(),
        )
    )
    append_event(db, chat, run, "run.queued", {"status": "queued"})
    await db.flush()
    await dispatch_head(db, chat)
    return run


async def admission_response(db: AsyncSession, chat: Chat, run: Run) -> dict[str, Any]:
    return {
        "run_id": str(run.id),
        "message_id": str(run.input_message_id),
        "status": run.status,
        "queue_position": await queue_position(db, chat, run),
        "events_url": f"/api/v1/chats/{chat.id}/events",
        "request_mode": run.request_mode,
    }


def save_admission(
    db: AsyncSession,
    chat: Chat,
    user: User,
    operation: str,
    key: str,
    body: dict[str, Any],
    run: Run,
    response: dict[str, Any],
) -> None:
    db.add(
        Admission(
            user_id=user.id,
            chat_id=chat.id,
            operation=operation,
            key=key,
            body_hash=body_hash(body),
            run_id=run.id,
            response=response,
        )
    )
