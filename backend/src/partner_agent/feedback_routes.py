"""Explicit rating/comment submission grants managers no private-chat access."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Select, select

from partner_agent.auth.admin import Admin
from partner_agent.auth.routes import DB, api_rate_limit, authenticated, unsafe_session
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import AuditEvent, utcnow
from partner_agent.infra.models_stage2 import Admission, Chat, Run
from partner_agent.runs.service import body_hash, lock_chat


def private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(
    prefix="/api/v1",
    tags=["feedback"],
    dependencies=[Depends(api_rate_limit), Depends(private_response)],
)


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    run_id: UUID
    rating: int = Field(ge=1, le=5, strict=True)
    comment: str = Field(default="", max_length=2000)

    @field_validator("comment")
    @classmethod
    def valid_comment(cls, value: str) -> str:
        if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("Invalid Unicode text")
        return value


def view(row: Admission) -> dict[str, Any]:
    item = row.response["feedback"]
    return {key: item[key] for key in ("id", "rating", "comment", "author", "created_at")}


def feedback_query() -> Select[Admission]:
    return select(Admission).where(
        Admission.operation == "product_feedback", Admission.key == "once"
    )


@router.get("/chats/{chat_id}/feedback")
async def existing_feedback(chat_id: UUID, request: Request, db: DB) -> dict[str, Any]:
    _, actor = await authenticated(request, db)
    await lock_chat(db, chat_id, actor)
    row = await db.scalar(
        feedback_query().where(Admission.chat_id == chat_id, Admission.user_id == actor.id)
    )
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
    row = await db.scalar(
        feedback_query().where(Admission.chat_id == chat_id, Admission.user_id == actor.id)
    )
    normalized = body.model_dump(mode="json")
    normalized["comment"] = body.comment.strip()
    digest = body_hash(normalized)
    if row:
        if row.id != body.id or row.body_hash != digest:
            raise APIError(409, "feedback_exists", "Feedback already submitted")
        return {"feedback": view(row)}
    run = await db.get(Run, body.run_id)
    if not run or run.chat_id != chat_id or run.user_id != actor.id or run.status != "succeeded":
        raise APIError(422, "feedback_answer_required", "Choose a completed answer in this chat")
    if await db.get(Admission, body.id):
        raise APIError(409, "feedback_exists", "Submission identifier already used")
    now = utcnow()
    row = Admission(
        id=body.id,
        chat_id=chat_id,
        user_id=actor.id,
        run_id=run.id,
        operation="product_feedback",
        key="once",
        body_hash=digest,
        created_at=now,
        response={
            "feedback": {
                "id": str(body.id),
                "rating": body.rating,
                "comment": normalized["comment"],
                "author": actor.normalized_login,
                "created_at": now.isoformat(),
            }
        },
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
            feedback_query()
            .join(Chat, Chat.id == Admission.chat_id)
            .where(Chat.deleted_at.is_(None))
            .order_by(Admission.created_at.desc(), Admission.id.desc())
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    return {
        "items": [view(row) for row in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }
