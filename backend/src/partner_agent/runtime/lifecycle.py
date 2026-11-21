"""Run ownership lifecycle. Every session belongs to exactly one operation."""

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from langgraph.types import Command
from sqlalchemy import func, select, update

from partner_agent.design_service import capture_design_context, create_revision, revision_status
from partner_agent.infra.db import session_factory
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import User
from partner_agent.infra.models_stage2 import (
    ContextManifest,
    Message,
    Outbox,
    ResumeCommand,
    RunAttempt,
)
from partner_agent.infra.settings import get_settings
from partner_agent.requirements_service import requirements_answer
from partner_agent.retrieval.service import validate_evidence_access
from partner_agent.runs.service import (
    Ownership,
    StaleOwnership,
    append_event,
    assert_ownership,
    build_context,
    finish_run,
    lock_run,
)
from partner_agent.runtime.fixture import FixtureState
from partner_agent.sinoniks_contract import DesignInput
from partner_agent.sinoniks_contract19 import Requirements19


@dataclass(frozen=True)
class Claim:
    ownership: Ownership
    input_message_id: UUID
    graph_input: FixtureState | Command[Any] | None
    remaining_seconds: float
    user_id: UUID
    project_answer: dict[str, Any] | None = None


async def claim_run(
    run_id: UUID, dispatch_generation: int, scheduler_id: str | None = None
) -> Claim | None:
    settings = get_settings()
    async with session_factory()() as db:
        chat, run = await lock_run(db, run_id)
        if (
            chat.deleted_at is not None
            or chat.active_run_id != run.id
            or run.status != "queued"
            or run.dispatch_generation != dispatch_generation
        ):
            return None
        disabled = await db.scalar(select(User.disabled).where(User.id == run.user_id))
        if disabled is not False:
            await finish_run(db, chat, run, "failed", {"code": "access_revoked"})
            await db.commit()
            return None
        if run.graph_version != settings.runtime_graph_version:
            await finish_run(db, chat, run, "failed", {"code": "graph_version_incompatible"})
            await db.commit()
            return None
        remaining = settings.runtime_wall_seconds - run.active_duration_ms / 1000
        if remaining <= 0:
            await finish_run(db, chat, run, "failed", {"code": "runtime_timeout"})
            await db.commit()
            return None
        now = await db.scalar(select(func.clock_timestamp()))
        assert now is not None
        run.status = "running"
        run.owner_token = uuid4()
        run.fence += 1
        run.lease_expires_at = now + timedelta(seconds=settings.runtime_lease_seconds)
        run.running_since = now
        run.started_at = run.started_at or now
        number = await db.scalar(
            select(func.max(RunAttempt.attempt_no)).where(RunAttempt.run_id == run.id)
        )
        db.add(
            RunAttempt(
                run_id=run.id,
                attempt_no=(number or 0) + 1,
                owner_token=run.owner_token,
                fence=run.fence,
                lease_expires_at=run.lease_expires_at,
                started_at=now,
            )
        )
        ownership = Ownership(run.id, chat.id, run.branch_id, run.owner_token, run.fence)
        if scheduler_id is not None:
            event = await db.scalar(
                select(Outbox)
                .where(
                    Outbox.run_id == run.id,
                    Outbox.dispatch_generation == dispatch_generation,
                )
                .with_for_update()
            )
            if event is not None:
                event.scheduler_id = scheduler_id
                event.status = "dispatched"
                event.claim_token = None
                event.lease_expires_at = None
                event.last_error_code = None
        history = await build_context(db, chat, run)
        await capture_design_context(db, run)
        message = await db.get(Message, run.input_message_id)
        if message is None:
            raise RuntimeError("Admitted input is missing")
        graph_input: FixtureState | Command[Any] | None = None
        if not run.input_applied:
            graph_input = FixtureState(
                input_message_id=str(message.id),
                question=message.content,
                history=[
                    {"id": str(item.id), "role": item.role, "content": item.content}
                    for item in history
                ],
            )
        elif run.pending_resume_command_id is not None:
            command = await db.get(ResumeCommand, run.pending_resume_command_id)
            if command is None:
                raise RuntimeError("Pending clarification command is missing")
            if not command.input_applied:
                answer = await db.get(Message, command.answer_message_id)
                if answer is None:
                    raise RuntimeError("Clarification answer is missing")
                graph_input = Command(
                    resume={
                        "resume_command_id": str(command.id),
                        "answer_message_id": str(answer.id),
                        "answer": answer.content,
                    }
                )
        append_event(
            db,
            chat,
            run,
            "run.started",
            {
                "attempt_no": (number or 0) + 1,
                "profile": settings.runtime_profile,
            },
        )
        await db.flush()
        await assert_ownership(db, run, ownership)
        await db.commit()
        return Claim(ownership, message.id, graph_input, remaining, run.user_id,
                     (run.outcome or {}).get("project_answer"))


async def heartbeat(ownership: Ownership) -> None:
    async with session_factory()() as db:
        _, run = await lock_run(db, ownership.run_id)
        await assert_ownership(db, run, ownership)
        now = await db.scalar(select(func.clock_timestamp()))
        assert now is not None
        run.lease_expires_at = now + timedelta(seconds=get_settings().runtime_lease_seconds)
        await db.execute(
            update(RunAttempt)
            .where(
                RunAttempt.run_id == run.id,
                RunAttempt.owner_token == ownership.owner_token,
                RunAttempt.fence == ownership.fence,
            )
            .values(lease_expires_at=run.lease_expires_at)
        )
        await db.flush()
        await assert_ownership(db, run, ownership)
        await db.commit()


def validated_vendor_wishes(value: object, message: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 5:
        return []
    wishes = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind, text = item.get("kind"), item.get("text")
        if (
            kind in ("unsupported", "free_text") and isinstance(text, str)
            and 0 < len(text) <= 500 and text.strip() and text in message
        ):
            wish = {"kind": kind, "text": text}
            if wish not in wishes:
                wishes.append(wish)
    return wishes


async def complete(
    ownership: Ownership, status: str, outcome: dict[str, Any], text: str | None = None,
    *, evidence: list[dict[str, Any]] | None = None,
    design_input: DesignInput | Requirements19 | None = None, design_parent_id: UUID | None = None,
    vendor_wishes: object = None,
) -> str:
    async with session_factory()() as db:
        chat, run = await lock_run(db, ownership.run_id)
        if status == "succeeded" and outcome.get("answer_mode") == "project_explanation" and (
            run.request_mode == "reference" or design_parent_id is None or design_input is not None
        ):
            raise APIError(409, "request_mode_violation", "Project explanation must be read-only")
        if status == "succeeded" and evidence is not None:
            await assert_ownership(db, run, ownership)
            actor = await db.get(User, run.user_id)
            if actor is None:
                raise APIError(403, "access_revoked", "Access revoked")
            await validate_evidence_access(db, actor, evidence, chat_id=chat.id)
            manifest = await db.scalar(
                select(ContextManifest).where(ContextManifest.run_id == run.id)
            )
            if manifest is None:
                raise RuntimeError("Context manifest is missing")
            manifest.evidence_ids = sorted({item["evidence_id"] for item in evidence})
        if status == "succeeded" and design_input is not None:
            if run.request_mode == "reference":
                raise APIError(409, "request_mode_violation", "Reference cannot change a project")
            await assert_ownership(db, run, ownership)
            row = await create_revision(
                db, chat_id=chat.id, branch_id=run.branch_id, parent_id=design_parent_id,
                conditions=design_input, key=run.id,
                digest=hashlib.sha256(design_input.model_dump_json().encode()).hexdigest(),
                message_id=run.input_message_id,
            )
            outcome = {**outcome, "design_id": str(row.id), "design_revision": row.revision,
                       "design_status": revision_status(row),
                       "design_schema_version": row.input.get("schema_version", 2),
                       "design_has_diagram": "diagram" in row.artifacts,
                       "design_has_result": row.result is not None,
                       "design_has_questions": bool(row.questions)
                       and row.input.get("schema_version") == 3}
            if row.input.get("schema_version") == 3:
                text = (str(row.result["explanation"]) if row.result else
                        requirements_answer(Requirements19.model_validate(row.input)))
                pending = [question["text"] for question in row.questions
                           if question["reason"] in ("missing", "conflict")
                           and not question.get("answered_unknown")]
                if pending:
                    text += "\n\nУточните:\n" + "\n".join(f"• {item}" for item in pending)
            elif row.questions:
                text = "Сохранил условия проекта. Для продолжения уточните:\n\n" + "\n".join(
                    f"• {question['text']}" for question in row.questions
                )
            else:
                assert row.result is not None
                text = row.result["explanation"] + "\n\nСхема и файлы доступны в карточке проекта."
        if status == "succeeded" and vendor_wishes is not None:
            source = await db.get(Message, run.input_message_id)
            if source is not None:
                wishes = validated_vendor_wishes(vendor_wishes, source.content)
                if wishes:
                    outcome = {**outcome, "vendor_wishes": wishes}
                    if text is not None:
                        text += "\n\nПожелания сохранены в сессии. Их можно выгрузить отдельно."
        metadata = run.outcome or {}
        if "design_input_version" in metadata:
            outcome = {"design_input_version": metadata["design_input_version"], **outcome}
        await finish_run(db, chat, run, status, outcome, ownership=ownership, text=text)
        await db.execute(
            update(RunAttempt)
            .where(
                RunAttempt.run_id == run.id,
                RunAttempt.owner_token == ownership.owner_token,
                RunAttempt.fence == ownership.fence,
            )
            .values(
                status=run.status, ended_at=func.clock_timestamp(), error_code=outcome.get("code")
            )
        )
        await db.commit()
        return run.status


async def wait_for_input(
    ownership: Ownership, question: str, options: list[str], interrupt_id: str
) -> None:
    async with session_factory()() as db:
        chat, run = await lock_run(db, ownership.run_id)
        await assert_ownership(db, run, ownership)
        lease = run.lease_expires_at
        now = await db.scalar(select(func.clock_timestamp()))
        assert now is not None
        if run.running_since is not None:
            run.active_duration_ms += int((now - run.running_since).total_seconds() * 1000)
        run.running_since = None
        run.status = "waiting_input"
        run.waiting_since = now
        run.clarification_id = uuid4()
        run.clarification_question = question
        run.clarification_options = options
        run.pending_resume_command_id = None
        run.outcome = {"profile": "offline_fixture", "interrupt_id": interrupt_id}
        run.owner_token = None
        run.lease_expires_at = None
        run.fence += 1
        append_event(
            db,
            chat,
            run,
            "run.waiting_input",
            {
                "clarification_id": str(run.clarification_id),
                "question": question,
                "options": options,
            },
        )
        await db.execute(
            update(RunAttempt)
            .where(
                RunAttempt.run_id == run.id,
                RunAttempt.owner_token == ownership.owner_token,
                RunAttempt.fence == ownership.fence,
            )
            .values(status="waiting_input", ended_at=now)
        )
        await db.flush()
        final_now = await db.scalar(select(func.clock_timestamp()))
        if lease is None or final_now is None or final_now >= lease:
            raise StaleOwnership("Lease expired while publishing clarification")
        await db.commit()
