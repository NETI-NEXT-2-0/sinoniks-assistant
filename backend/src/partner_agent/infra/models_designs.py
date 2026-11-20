"""Immutable operator-reviewed design revisions scoped to a chat branch."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from partner_agent.infra.models import Base, utcnow


class DesignRevision(Base):
    __tablename__ = "design_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["branch_id", "chat_id"], ["chat_branches.id", "chat_branches.chat_id"],
            ondelete="CASCADE", name="fk_design_branch_chat",
        ),
        CheckConstraint("revision >= 1", name="ck_design_revision"),
        UniqueConstraint("branch_id", "revision", name="uq_design_branch_revision"),
        UniqueConstraint("chat_id", "idempotency_key", name="uq_design_request_key"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), index=True,
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("design_revisions.id"))
    revision: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[uuid.UUID] = mapped_column()
    request_sha256: Mapped[str] = mapped_column(String(64))
    input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    questions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    artifacts: Mapped[dict[str, str]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunDesignContext(Base):
    """Presence records a snapshot, including the explicit absence of a project."""

    __tablename__ = "run_design_contexts"
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True,
    )
    base_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("design_revisions.id"),
    )
