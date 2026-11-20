"""Chat data plane. Content revisions are immutable; edit creates a new branch/message."""

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from partner_agent.infra.models import Base, utcnow
from partner_agent.runs.types import RequestMode


class Chat(Base):
    __tablename__ = "chats"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200), default="Новый чат")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    default_branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    active_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    queue_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    event_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    event_floor_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "revision >= 1 AND queue_seq >= 0 "
            "AND event_seq >= event_floor_seq AND event_floor_seq >= 0"
        ),
    )


class ChatBranch(Base):
    __tablename__ = "chat_branches"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    parent_branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chat_branches.id"))
    fork_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    head_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("id", "chat_id", name="uq_branch_chat"),)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_branches.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    input_message_id: Mapped[uuid.UUID] = mapped_column(UUID)
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    request_mode: Mapped[RequestMode] = mapped_column(
        String(16), default="auto", server_default="auto",
    )
    queue_seq: Mapped[int] = mapped_column(BigInteger)
    graph_thread_id: Mapped[str] = mapped_column(String(36), unique=True)
    graph_version: Mapped[str] = mapped_column(String(64))
    owner_token: Mapped[uuid.UUID | None] = mapped_column(UUID)
    fence: Mapped[int] = mapped_column(BigInteger, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_applied: Mapped[bool] = mapped_column(default=False)
    input_checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    dispatch_generation: Mapped[int] = mapped_column(Integer, default=0)
    partial_text: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attachment_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    clarification_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    clarification_question: Mapped[str | None] = mapped_column(Text)
    clarification_options: Mapped[list[str]] = mapped_column(JSONB, default=list)
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_resume_command_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    active_duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("chat_id", "queue_seq", name="uq_run_queue"),
        CheckConstraint(
            "request_mode IN ('auto','design','reference')", name="ck_run_request_mode",
        ),
        CheckConstraint(
            "status IN ('queued','running','waiting_input','succeeded','failed','cancelled')"
        ),
        CheckConstraint(
            "queue_seq > 0 AND fence >= 0 AND dispatch_generation >= 0 AND active_duration_ms >= 0"
        ),
        CheckConstraint("graph_thread_id = id::text", name="ck_run_thread_id"),
        Index(
            "uq_one_running_chat",
            "chat_id",
            unique=True,
            postgresql_where=text("status IN ('running','waiting_input')"),
        ),
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_branches.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="completed")
    origin: Mapped[str] = mapped_column(String(24), default="user")
    content: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_message_run_ordinal"),
        CheckConstraint("role IN ('user','assistant','tool','system')"),
        CheckConstraint("revision >= 1 AND ordinal >= 0"),
        Index(
            "uq_assistant_final_run",
            "run_id",
            unique=True,
            postgresql_where=text("role = 'assistant' AND status = 'completed'"),
        ),
    )


class RunAttempt(Base):
    __tablename__ = "run_attempts"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    owner_token: Mapped[uuid.UUID] = mapped_column(UUID)
    fence: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), default="running")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("run_id", "attempt_no", name="uq_run_attempt"),)


class Admission(Base):
    __tablename__ = "admissions"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    operation: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(128))
    body_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("user_id", "chat_id", "operation", "key", name="uq_admission_scope_key"),
    )


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    logical_key: Mapped[str] = mapped_column(String(128), unique=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    dispatch_generation: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, str]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    scheduler_id: Mapped[str | None] = mapped_column(String(128))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("run_id", "dispatch_generation", name="uq_outbox_run_generation"),
        CheckConstraint("status IN ('pending','claimed','dispatched')"),
        Index("ix_outbox_due", "status", "next_attempt_at"),
    )


class RunEvent(Base):
    __tablename__ = "run_events"
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chat_branches.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_run_events_at", "at"),)


class ResumeCommand(Base):
    __tablename__ = "resume_commands"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    clarification_id: Mapped[uuid.UUID] = mapped_column(UUID)
    answer_message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"))
    input_applied: Mapped[bool] = mapped_column(default=False)
    checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("run_id", "clarification_id", name="uq_clarification_command"),
    )


class ContextManifest(Base):
    __tablename__ = "context_manifests"
    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), unique=True
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_branches.id"))
    head_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID)
    message_ids: Mapped[list[str]] = mapped_column(JSONB)
    message_revisions: Mapped[dict[str, int]] = mapped_column(JSONB)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    policy_revision: Mapped[str] = mapped_column(String(64), default="history-v1")
    graph_version: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64), default="offline-fixture-v1")
    model_version: Mapped[str] = mapped_column(String(64), default="offline_fixture")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# Pointers form cycles; deferred constraints allow one atomic admission/finalization.

cast(Table, Run.__table__).append_constraint(UniqueConstraint("id", "chat_id", name="uq_run_chat"))
cast(Table, Message.__table__).append_constraint(
    UniqueConstraint("id", "chat_id", name="uq_message_chat")
)
for table, columns, target, name in (
    (
        Chat.__table__,
        ["default_branch_id", "id"],
        ["chat_branches.id", "chat_branches.chat_id"],
        "fk_chat_default_branch",
    ),
    (Chat.__table__, ["active_run_id", "id"], ["runs.id", "runs.chat_id"], "fk_chat_active_run"),
    (
        ChatBranch.__table__,
        ["fork_message_id", "chat_id"],
        ["messages.id", "messages.chat_id"],
        "fk_branch_fork",
    ),
    (
        ChatBranch.__table__,
        ["head_message_id", "chat_id"],
        ["messages.id", "messages.chat_id"],
        "fk_branch_head",
    ),
    (
        Run.__table__,
        ["input_message_id", "chat_id"],
        ["messages.id", "messages.chat_id"],
        "fk_run_input",
    ),
    (
        Run.__table__,
        ["assistant_message_id", "chat_id"],
        ["messages.id", "messages.chat_id"],
        "fk_run_assistant",
    ),
    (Run.__table__, ["pending_resume_command_id"], ["resume_commands.id"], "fk_run_resume"),
):
    cast(Table, table).append_constraint(
        ForeignKeyConstraint(
            columns, target, name=name, use_alter=True, deferrable=True, initially="DEFERRED"
        )
    )
