"""Durable per-attempt usage. No prompts, credentials or answer payloads."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from partner_agent.infra.models import Base, utcnow


class ModelCall(Base):
    __tablename__ = "model_calls"
    __table_args__ = (CheckConstraint(
        "status IN ('started','succeeded','failed','cancelled')", name="ck_model_call_status",
    ),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True,
    )
    model: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str | None] = mapped_column(String(200))
    purpose: Mapped[str] = mapped_column(String(40), default="answer_and_design")
    status: Mapped[str] = mapped_column(String(24), default="started")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_id: Mapped[str | None] = mapped_column(String(200))
    tariff: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80))


class TurnTiming(Base):
    __tablename__ = "turn_timings"
    __table_args__ = (CheckConstraint("elapsed_ms >= 0", name="ck_turn_timing_nonnegative"),)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True,
    )
    elapsed_ms: Mapped[int] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
