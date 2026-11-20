"""Private chat files, independent of the published knowledge corpus."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from partner_agent.infra.models import Base, utcnow


class ChatFile(Base):
    __tablename__ = "chat_files"
    __table_args__ = (
        UniqueConstraint("chat_id", "upload_key", name="uq_chat_file_upload"),
        CheckConstraint("status IN ('pending','ready','failed')", name="ck_chat_file_status"),
        CheckConstraint("size_bytes > 0 AND size_bytes <= 26214400", name="ck_chat_file_size"),
        CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_chat_file_attempts"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    upload_key: Mapped[uuid.UUID] = mapped_column()
    name: Mapped[str] = mapped_column(String(200))
    media_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    source_sha256: Mapped[str] = mapped_column(String(64))
    original_key: Mapped[str] = mapped_column(String(200), unique=True)
    canonical_sha256: Mapped[str | None] = mapped_column(String(64))
    chunks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    warnings: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    error_code: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
