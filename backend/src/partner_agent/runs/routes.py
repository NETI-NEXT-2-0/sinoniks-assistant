"""Admission is a single PG transaction; scheduler delivery is exclusively outbox work."""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select

from partner_agent.auth.routes import DB, api_rate_limit, authenticated, unsafe_session
from partner_agent.chats.routes import selected_branch
from partner_agent.design_service import latest_revision, validate_project_sources
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import utcnow
from partner_agent.infra.models_stage2 import Message, ResumeCommand, Run
from partner_agent.project_answers import ProjectAnswerCommand19, apply_answer
from partner_agent.runs.service import (
    admission_response,
    admit_run,
    append_event,
    enqueue_dispatch,
    finish_run,
    lock_chat,
    lock_run,
    replay_admission,
    run_dto,
    save_admission,
)
from partner_agent.runs.types import RequestMode
from partner_agent.sinoniks_contract19 import Requirements19

router = APIRouter(prefix="/api/v1", tags=["runs"], dependencies=[Depends(api_rate_limit)])


class SubmittedWish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["free_text"] = "free_text"
    text: str = Field(min_length=1, max_length=500)

    @field_validator("text")
    @classmethod
    def original_text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Wish must contain original text")
        value.encode("utf-8")
        return value


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    branch_id: uuid.UUID
    message: str = Field(min_length=1, max_length=32768)
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5)
    web_mode: Literal["off", "public", "official"] = "off"
    request_mode: RequestMode = "auto"
    project_answer: ProjectAnswerCommand19 | None = None
    vendor_wish: SubmittedWish | None = None

    @model_validator(mode="after")
    def wish_is_user_text(self) -> "RunBody":
        if self.vendor_wish is not None and (
            self.vendor_wish.text not in self.message or self.project_answer is not None
        ):
            raise ValueError("A free-text wish must be part of the submitted message")
        return self

    @field_validator("message")
    @classmethod
    def byte_limit(cls, value: str) -> str:
        if not value.strip() or len(value.encode()) > 32768:
            raise ValueError("Message exceeds byte limit or is empty")
        return value


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clarification_id: uuid.UUID
    answer: str = Field(min_length=1, max_length=32768)

    @field_validator("answer")
    @classmethod
    def byte_limit(cls, value: str) -> str:
        return RunBody.byte_limit(value)


@router.post("/chats/{chat_id}/runs", status_code=202)
async def submit(
    chat_id: uuid.UUID,
    body: RunBody,
    request: Request,
    db: DB,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, user = await authenticated(request, db)
    chat = await lock_chat(db, chat_id, user)
    normalized = body.model_dump(mode="json")
    if body.project_answer is None:
        normalized.pop("project_answer")
    if body.vendor_wish is None:
        normalized.pop("vendor_wish")
    if body.request_mode == "auto":
        # Keep pre-0027 command hashes replayable, including explicit auto.
        normalized.pop("request_mode")
    replay = await replay_admission(db, chat, user, "run", idempotency_key or "", normalized)
    if replay is not None:
        await db.commit()
        return replay
    if body.web_mode != "off":
        raise APIError(503, "stage_unavailable", "Web tools are not enabled yet")
    branch = await selected_branch(db, chat, body.branch_id)
    if body.project_answer is not None:
        if body.request_mode == "reference" or body.attachment_ids:
            raise APIError(422, "project_answer_mode_invalid", "Use a project field answer")
        project = await latest_revision(db, chat.id, branch.id)
        if project is None or project.id != body.project_answer.base_revision_id:
            raise APIError(409, "design_revision_conflict", "Reload before answering")
        await validate_project_sources(db, user, chat.id, project)
        if project.input.get("schema_version") != 3:
            raise APIError(409, "project_answer_version_invalid", "Use requirements 1.9")
        apply_answer(Requirements19.model_validate(project.input), body.project_answer)
    run = await admit_run(
        db, chat, branch, user, body.message, attachment_ids=body.attachment_ids,
        request_mode=body.request_mode,
    )
    if body.project_answer is not None:
        run.outcome = {"project_answer": body.project_answer.model_dump(mode="json")}
    result = await admission_response(db, chat, run)
    if body.vendor_wish is not None:
        # The immutable admission receipt survives cancellation, crashes and outcome replacement.
        result = {**result, "vendor_wish": body.vendor_wish.model_dump(mode="json")}
    save_admission(db, chat, user, "run", idempotency_key or "", normalized, run, result)
    await db.commit()
    return result


@router.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID, request: Request, db: DB) -> dict[str, Any]:
    _, user = await authenticated(request, db)
    # First authorize the chat without returning another principal's resource existence.
    chat_id = await db.scalar(select(Run.chat_id).where(Run.id == run_id))
    if chat_id is None:
        raise APIError(404, "not_found", "Run not found")
    chat = await lock_chat(db, chat_id, user)
    run = await db.get(Run, run_id)
    assert run is not None
    result = await run_dto(db, chat, run)
    await db.commit()
    return result


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: uuid.UUID, request: Request, db: DB) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, user = await authenticated(request, db)
    chat_id = await db.scalar(select(Run.chat_id).where(Run.id == run_id))
    if chat_id is None:
        raise APIError(404, "not_found", "Run not found")
    await lock_chat(db, chat_id, user)
    chat, run = await lock_run(db, run_id)
    outcome: dict[str, Any] = {"reason": "user_cancelled"}
    if "project_answer" in (run.outcome or {}):
        # This admitted operation cannot call a model, including after claim.
        outcome.update({"execution": "local_project_answer", "provider_calls": 0,
                        "project_answer": (run.outcome or {})["project_answer"]})
        if "design_input_version" in (run.outcome or {}):
            outcome["design_input_version"] = (run.outcome or {})["design_input_version"]
    await finish_run(db, chat, run, "cancelled", outcome)
    result = await run_dto(db, chat, run)
    await db.commit()
    return result


@router.post("/runs/{run_id}/resume", status_code=202)
async def resume(
    run_id: uuid.UUID,
    body: ResumeBody,
    request: Request,
    db: DB,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, user = await authenticated(request, db)
    chat_id = await db.scalar(select(Run.chat_id).where(Run.id == run_id))
    if chat_id is None:
        raise APIError(404, "not_found", "Run not found")
    chat = await lock_chat(db, chat_id, user)
    normalized = {"run_id": str(run_id), **body.model_dump(mode="json")}
    replay = await replay_admission(db, chat, user, "resume", idempotency_key or "", normalized)
    if replay is not None:
        await db.commit()
        return replay
    _, run = await lock_run(db, run_id)
    if (
        run.status != "waiting_input"
        or run.clarification_id != body.clarification_id
        or chat.active_run_id != run.id
    ):
        raise APIError(409, "clarification_conflict", "This clarification is no longer pending")
    if run.waiting_since is not None and (utcnow() - run.waiting_since).total_seconds() >= 86400:
        await finish_run(db, chat, run, "cancelled", {"reason": "input_timeout"})
        await db.commit()
        raise APIError(409, "input_timeout", "Clarification expired")
    ordinal = await db.scalar(select(func.max(Message.ordinal)).where(Message.run_id == run.id))
    answer = Message(
        id=uuid.uuid4(),
        chat_id=chat.id,
        branch_id=run.branch_id,
        run_id=run.id,
        role="user",
        status="completed",
        origin="clarification",
        content=body.answer,
        ordinal=(ordinal or 0) + 1,
        revision=1,
        created_at=utcnow(),
    )
    db.add(answer)
    await db.flush()
    command = ResumeCommand(
        id=uuid.uuid4(),
        run_id=run.id,
        clarification_id=body.clarification_id,
        answer_message_id=answer.id,
        input_applied=False,
    )
    db.add(command)
    run.pending_resume_command_id = command.id
    run.status = "queued"
    run.dispatch_generation += 1
    run.waiting_since = None
    append_event(db, chat, run, "run.queued", {"status": "queued", "resumed": True})
    await enqueue_dispatch(db, run)
    result = await admission_response(db, chat, run)
    save_admission(db, chat, user, "resume", idempotency_key or "", normalized, run, result)
    await db.commit()
    return result
