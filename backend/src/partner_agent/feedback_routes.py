"""Explicit rating/comment submission grants managers no private-chat access."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from partner_agent.auth.admin import Admin
from partner_agent.auth.routes import DB, api_rate_limit, authenticated, unsafe_session
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import AuditEvent
from partner_agent.infra.models_feedback import ProductFeedback
from partner_agent.infra.models_stage2 import Chat, Run
from partner_agent.runs.service import lock_chat

router = APIRouter(prefix="/api/v1", tags=["feedback"], dependencies=[Depends(api_rate_limit)])


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    run_id: UUID
    rating: int = Field(ge=1, le=5, strict=True)
    comment: str = Field(default="", max_length=2000)


def view(row: ProductFeedback) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "rating": row.rating,
        "comment": row.comment,
        "author": row.author,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/chats/{chat_id}/feedback")
async def existing_feedback(chat_id: UUID, request: Request, db: DB) -> dict[str, Any]:
    _, actor = await authenticated(request, db)
    await lock_chat(db, chat_id, actor)
    row = await db.scalar(select(ProductFeedback).where(ProductFeedback.chat_id == chat_id))
    return {"feedback": view(row) if row else None}


@router.post("/chats/{chat_id}/feedback")
async def submit_feedback(
    chat_id: UUID, body: Submission, request: Request, db: DB
) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    await lock_chat(db, chat_id, actor)
    # Lock the owner-scoped chat before checking the unique submission; concurrent
    # clicks and transport retries cannot insert duplicate rows.
    row = await db.scalar(select(ProductFeedback).where(ProductFeedback.chat_id == chat_id))
    if row:
        if (row.id, row.run_id, row.rating, row.comment) != (
            body.id,
            body.run_id,
            body.rating,
            body.comment.strip(),
        ):
            raise APIError(409, "feedback_exists", "Feedback already submitted")
        return {"feedback": view(row)}
    run = await db.get(Run, body.run_id)
    if not run or run.chat_id != chat_id or run.user_id != actor.id or run.status != "succeeded":
        raise APIError(422, "feedback_answer_required", "Choose a completed answer in this chat")
    if await db.get(ProductFeedback, body.id):
        raise APIError(409, "feedback_exists", "Submission identifier already used")
    row = ProductFeedback(
        id=body.id,
        chat_id=chat_id,
        user_id=actor.id,
        run_id=run.id,
        rating=body.rating,
        comment=body.comment.strip(),
        author=actor.normalized_login,
    )
    db.add(row)
    db.add(
        AuditEvent(
            actor_id=actor.id, action="feedback.submit", resource_id=str(row.id), result="success"
        )
    )
    await db.commit()
    return {"feedback": view(row)}


@router.get("/admin/feedback")
async def list_feedback(
    admin: Admin,
    db: DB,
    offset: int = Query(default=0, ge=0, le=100000),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    rows = (
        await db.scalars(
            select(ProductFeedback)
            .join(Chat, Chat.id == ProductFeedback.chat_id)
            .where(Chat.deleted_at.is_(None))
            .order_by(ProductFeedback.created_at.desc(), ProductFeedback.id.desc())
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    return {
        "items": [view(row) for row in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }
