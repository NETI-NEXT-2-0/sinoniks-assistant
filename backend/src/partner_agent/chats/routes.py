"""Owner-scoped chat commands, branch prefixes, consistent snapshots and durable SSE."""

import asyncio
import base64
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from partner_agent.auth.routes import DB, api_rate_limit, authenticated, unsafe_session
from partner_agent.design_service import fork_project_base
from partner_agent.infra.db import session_factory
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import Session, User, utcnow
from partner_agent.infra.models_designs import RunDesignContext
from partner_agent.infra.models_stage2 import Chat, ChatBranch, Message, Run, RunEvent
from partner_agent.runs.service import (
    TERMINAL,
    admission_response,
    admit_run,
    finish_run,
    lock_chat,
    message_dto,
    replay_admission,
    run_dto,
    save_admission,
    visible_messages,
)

router = APIRouter(prefix="/api/v1/chats", tags=["chats"], dependencies=[Depends(api_rate_limit)])


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="Новый чат", min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Title is empty")
        return value.strip()


class RenameBody(ChatBody):
    expected_revision: int = Field(ge=1)


class BranchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_branch_id: uuid.UUID
    message_id: uuid.UUID
    mode: Literal["edit", "regenerate"]
    revised_text: str | None = Field(default=None, min_length=1, max_length=32768)

    @field_validator("revised_text")
    @classmethod
    def bounded_text(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value.encode()) > 32768):
            raise ValueError("Invalid revised message")
        return value


def chat_dto(chat: Chat) -> dict[str, Any]:
    return {
        "id": str(chat.id),
        "title": chat.title,
        "revision": chat.revision,
        "default_branch_id": str(chat.default_branch_id),
        "active_run_id": str(chat.active_run_id) if chat.active_run_id else None,
        "created_at": chat.created_at.isoformat(),
        "updated_at": chat.updated_at.isoformat(),
    }


def branch_dto(branch: ChatBranch) -> dict[str, Any]:
    return {
        "id": str(branch.id),
        "chat_id": str(branch.chat_id),
        "revision": branch.revision,
        "parent_branch_id": str(branch.parent_branch_id) if branch.parent_branch_id else None,
        "fork_message_id": str(branch.fork_message_id) if branch.fork_message_id else None,
        "head_message_id": str(branch.head_message_id) if branch.head_message_id else None,
        "created_at": branch.created_at.isoformat(),
    }


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(value: str) -> str:
    try:
        if len(value) > 256:
            raise ValueError("Oversized cursor")
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        ).decode()
    except (ValueError, UnicodeError) as exc:
        raise APIError(422, "invalid_cursor", "Invalid cursor") from exc


async def selected_branch(db: AsyncSession, chat: Chat, branch_id: uuid.UUID | None) -> ChatBranch:
    branch = await db.get(ChatBranch, branch_id or chat.default_branch_id)
    if branch is None or branch.chat_id != chat.id:
        raise APIError(404, "not_found", "Branch not found")
    return branch


@router.get("")
async def list_chats(
    request: Request,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: str | None = None,
) -> dict[str, Any]:
    _, user = await authenticated(request, db)
    query = select(Chat).where(Chat.owner_id == user.id, Chat.deleted_at.is_(None))
    if cursor:
        try:
            raw_at, raw_id = decode_cursor(cursor).split("|", 1)
            before_at, before_id = datetime.fromisoformat(raw_at), uuid.UUID(raw_id)
        except ValueError as exc:
            raise APIError(422, "invalid_cursor", "Invalid cursor") from exc
        query = query.where(tuple_(Chat.created_at, Chat.id) < (before_at, before_id))
    rows = list(
        (
            await db.scalars(
                query.order_by(Chat.created_at.desc(), Chat.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    page = rows[:limit]
    result = {
        "items": [chat_dto(row) for row in page],
        "next_cursor": encode_cursor(page[-1].created_at.isoformat() + "|" + str(page[-1].id))
        if len(rows) > limit
        else None,
    }
    await db.commit()
    return result


@router.post("", status_code=201)
async def create_chat(body: ChatBody, request: Request, db: DB) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, user = await authenticated(request, db)
    chat = Chat(
        id=uuid.uuid4(),
        owner_id=user.id,
        title=body.title,
        revision=1,
        queue_seq=0,
        event_seq=0,
        event_floor_seq=0,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(chat)
    await db.flush()
    branch = ChatBranch(id=uuid.uuid4(), chat_id=chat.id, revision=1, created_at=utcnow())
    db.add(branch)
    chat.default_branch_id = branch.id
    await db.commit()
    return chat_dto(chat)


@router.get("/{chat_id}")
async def get_chat(chat_id: uuid.UUID, request: Request, db: DB) -> dict[str, Any]:
    _, user = await authenticated(request, db)
    chat = await lock_chat(db, chat_id, user)
    result = chat_dto(chat)
    await db.commit()
    return result


@router.patch("/{chat_id}")
async def rename_chat(
    chat_id: uuid.UUID, body: RenameBody, request: Request, db: DB
) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, user = await authenticated(request, db)
    chat = await lock_chat(db, chat_id, user)
    if chat.revision != body.expected_revision:
        raise APIError(409, "revision_conflict", "Chat changed; reload before editing")
    chat.title, chat.revision, chat.updated_at = body.title, chat.revision + 1, utcnow()
    await db.commit()
    return chat_dto(chat)


@router.delete("/{chat_id}", status_code=204)
async def delete_chat(chat_id: uuid.UUID, request: Request, db: DB) -> Response:
    await unsafe_session(request, db)
    _, user = await authenticated(request, db)
    chat = await lock_chat(db, chat_id, user)
    chat.deleted_at = utcnow()
    runs = (
        await db.scalars(
            select(Run)
            .where(Run.chat_id == chat.id, Run.status.not_in(TERMINAL))
            .order_by(Run.queue_seq)
            .with_for_update()
        )
    ).all()
    for run in runs:
        await finish_run(db, chat, run, "cancelled", {"reason": "chat_deleted"})
    chat.active_run_id = None
    chat.revision += 1
    await db.commit()
    return Response(status_code=204)


@router.get("/{chat_id}/messages")
async def list_messages(
    chat_id: uuid.UUID,
    request: Request,
    db: DB,
    branch_id: uuid.UUID | None = None,
    before: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, Any]:
    _, user = await authenticated(request, db)
    chat = await lock_chat(db, chat_id, user)
    branch = await selected_branch(db, chat, branch_id)
    rows = await visible_messages(db, branch)
    if before:
        message_id = decode_cursor(before)
        positions = [i for i, message in enumerate(rows) if str(message.id) == message_id]
        if not positions:
            raise APIError(422, "invalid_cursor", "Cursor does not belong to this branch")
        rows = rows[: positions[0]]
    page = rows[-limit:]
    result = {
        "items": [message_dto(m) for m in page],
        "next_cursor": encode_cursor(str(page[0].id)) if len(rows) > limit else None,
    }
    await db.commit()
    return result


@router.get("/{chat_id}/snapshot")
async def snapshot(
    chat_id: uuid.UUID, request: Request, db: DB, branch_id: uuid.UUID | None = None
) -> dict[str, Any]:
    _, user = await authenticated(request, db)
    # Every authoritative write takes this same lock: cursor and rows share one frontier.
    chat = await lock_chat(db, chat_id, user)
    branch = await selected_branch(db, chat, branch_id)
    messages = await visible_messages(db, branch)
    page = messages[-30:]
    branches = (
        await db.scalars(
            select(ChatBranch)
            .where(ChatBranch.chat_id == chat.id)
            .order_by(ChatBranch.created_at, ChatBranch.id)
        )
    ).all()
    runs = (
        await db.scalars(select(Run).where(Run.chat_id == chat.id).order_by(Run.queue_seq))
    ).all()
    selected_runs = [r for r in runs if r.status not in TERMINAL]
    selected_runs += [r for r in runs if r.status in TERMINAL and r.branch_id == branch.id][-30:]
    result = {
        "chat": chat_dto(chat),
        "branches": [branch_dto(b) for b in branches],
        "messages": [message_dto(m) for m in page],
        "messages_next_cursor": encode_cursor(str(page[0].id)) if len(messages) > 30 else None,
        "runs": [await run_dto(db, chat, run) for run in selected_runs],
        "selected_branch_id": str(branch.id),
        "cursor": chat.event_seq,
    }
    await db.commit()
    return result


@router.post("/{chat_id}/branches", status_code=201)
async def fork(
    chat_id: uuid.UUID,
    body: BranchBody,
    request: Request,
    db: DB,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, user = await authenticated(request, db)
    chat = await lock_chat(db, chat_id, user)
    normalized = body.model_dump(mode="json")
    replay = await replay_admission(db, chat, user, "branch", idempotency_key or "", normalized)
    if replay is not None:
        await db.commit()
        return replay
    parent = await selected_branch(db, chat, body.parent_branch_id)
    visible = await visible_messages(db, parent)
    selected = next((m for m in visible if m.id == body.message_id), None)
    if selected is None:
        raise APIError(404, "not_found", "Message not found")
    if body.mode == "regenerate":
        if selected.role != "assistant":
            raise APIError(422, "invalid_branch_target", "Regenerate requires an assistant message")
        original = await db.get(Run, selected.run_id)
        selected = await db.get(Message, original.input_message_id) if original else None
    if selected is None or selected.role != "user" or selected.ordinal != 0:
        raise APIError(422, "invalid_branch_target", "Edit requires an original user message")
    if body.mode == "edit" and body.revised_text is None:
        raise APIError(422, "message_required", "Revised text is required")
    index = next(i for i, message in enumerate(visible) if message.id == selected.id)
    prefix_head = visible[index - 1].id if index else None
    branch = ChatBranch(
        id=uuid.uuid4(),
        chat_id=chat.id,
        parent_branch_id=parent.id,
        fork_message_id=prefix_head,
        head_message_id=prefix_head,
        revision=1,
        created_at=utcnow(),
    )
    db.add(branch)
    await db.flush()
    original_run = await db.get(Run, selected.run_id)
    project_base = await fork_project_base(db, chat.id, original_run, visible[:index])
    run = await admit_run(
        db,
        chat,
        branch,
        user,
        (body.revised_text or "") if body.mode == "edit" else selected.content,
        origin=body.mode,
        request_mode=original_run.request_mode if original_run else "auto",
        attachment_ids=[uuid.UUID(value) for value in
                        ((original_run.attachment_ids or []) if original_run else [])],
    )
    if project_base is None:
        run.outcome = {"design_input_version": (original_run.outcome or {}).get(
            "design_input_version", 2,
        ) if original_run else 2}
    if body.mode == "regenerate" and original_run is not None and (
        "project_answer" in (original_run.outcome or {})
    ):
        run.outcome = {**(run.outcome or {}),
                       "project_answer": (original_run.outcome or {})["project_answer"]}
    result = {**await admission_response(db, chat, run), "branch_id": str(branch.id)}
    db.add(RunDesignContext(
        run_id=run.id, base_revision_id=project_base.id if project_base else None,
    ))
    save_admission(db, chat, user, "branch", idempotency_key or "", normalized, run, result)
    await db.commit()
    return result


def event_envelope(event: RunEvent) -> dict[str, Any]:
    return {
        "v": 1,
        "chat_id": str(event.chat_id),
        "branch_id": str(event.branch_id) if event.branch_id else None,
        "run_id": str(event.run_id) if event.run_id else None,
        "seq": event.seq,
        "at": event.at.isoformat(),
        "payload": event.payload,
    }


def encode_event(event_type: str, seq: int, data: dict[str, Any]) -> str:
    return f"id: {seq}\nevent: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_authorized(chat_id: uuid.UUID, session_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    async with session_factory()() as db:
        current = await db.scalar(
            select(Session.id)
            .join(User, User.id == Session.user_id)
            .join(Chat, Chat.owner_id == User.id)
            .where(
                Session.id == session_id,
                Session.user_id == user_id,
                Session.revoked_at.is_(None),
                Session.expires_at > func.clock_timestamp(),
                Session.absolute_expires_at > func.clock_timestamp(),
                User.disabled.is_(False),
                Chat.id == chat_id,
                Chat.deleted_at.is_(None),
            )
        )
        return current is not None


async def event_stream(
    request: Request, chat_id: uuid.UUID, session_id: uuid.UUID, user_id: uuid.UUID, after: int
) -> AsyncIterator[str]:
    heartbeat = time.monotonic()
    while not await request.is_disconnected():
        batch: list[tuple[str, int, dict[str, Any]]] = []
        reset = False
        # A fresh, short transaction per poll; never hold request sessions across yields.
        async with session_factory()() as db:
            session = await db.get(Session, session_id)
            user = await db.get(User, user_id)
            now = utcnow()
            if (
                session is None
                or session.user_id != user_id
                or session.revoked_at is not None
                or session.expires_at <= now
                or session.absolute_expires_at <= now
                or user is None
                or user.disabled
            ):
                return  # Client revalidates /auth/me after transport close; no post-header 401.
            try:
                chat = await lock_chat(db, chat_id, user)
            except APIError:
                return
            # Retention and floor advance atomically with the same chat cursor lock.
            expired = (
                await db.scalars(
                    select(RunEvent.seq)
                    .where(RunEvent.chat_id == chat.id, RunEvent.at < now - timedelta(days=7))
                    .order_by(RunEvent.seq)
                )
            ).all()
            if expired:
                floor = max(expired)
                await db.execute(
                    delete(RunEvent).where(RunEvent.chat_id == chat.id, RunEvent.seq <= floor)
                )
                chat.event_floor_seq = max(chat.event_floor_seq, floor)
            if after < chat.event_floor_seq or after > chat.event_seq:
                reset = True
                data = {
                    "v": 1,
                    "chat_id": str(chat.id),
                    "branch_id": None,
                    "run_id": None,
                    "seq": chat.event_seq,
                    "at": now.isoformat(),
                    "payload": {
                        "snapshot_url": f"/api/v1/chats/{chat.id}/snapshot",
                        "cursor": chat.event_seq,
                    },
                }
                batch.append(("stream.reset", chat.event_seq, data))
            else:
                events = (
                    await db.scalars(
                        select(RunEvent)
                        .where(RunEvent.chat_id == chat.id, RunEvent.seq > after)
                        .order_by(RunEvent.seq)
                        .limit(100)
                    )
                ).all()
                batch = [(event.type, event.seq, event_envelope(event)) for event in events]
            await db.commit()
        # No database connection/transaction remains checked out during slow client writes.
        for event_type, seq, data in batch:
            if not await stream_authorized(chat_id, session_id, user_id):
                return
            yield encode_event(event_type, seq, data)
            after = seq
        if reset:
            return
        if time.monotonic() - heartbeat >= 15:
            yield ": heartbeat\n\n"
            heartbeat = time.monotonic()
        if not batch:
            await asyncio.sleep(1)


@router.get("/{chat_id}/events")
async def events(
    chat_id: uuid.UUID,
    request: Request,
    db: DB,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    session, user = await authenticated(request, db)
    await lock_chat(db, chat_id, user)
    if last_event_id is not None:
        try:
            after = int(last_event_id)
            if after < 0:
                raise ValueError("Negative cursor")
        except ValueError as exc:
            raise APIError(422, "invalid_cursor", "Invalid Last-Event-ID") from exc
    session_id, user_id = session.id, user.id
    await db.commit()
    await db.close()
    return StreamingResponse(
        event_stream(request, chat_id, session_id, user_id, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
