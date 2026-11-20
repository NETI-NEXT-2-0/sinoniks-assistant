"""Revision construction shared by HTTP editing and fenced run completion.

Callers must hold the chat lock and enforce ownership before using these helpers.
"""

from uuid import UUID, uuid4

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from partner_agent.infra.errors import APIError
from partner_agent.infra.models_designs import DesignRevision, RunDesignContext
from partner_agent.infra.models_stage2 import ChatBranch, Message, Run
from partner_agent.sinoniks import clarification_questions, plan
from partner_agent.sinoniks_contract import DesignInput, SourceReference
from partner_agent.sinoniks_export import export_files


async def latest_revision(
    db: AsyncSession, chat_id: UUID, branch_id: UUID,
) -> DesignRevision | None:
    own = await db.scalar(select(DesignRevision).where(
        DesignRevision.chat_id == chat_id, DesignRevision.branch_id == branch_id,
    ).order_by(DesignRevision.revision.desc()).limit(1))
    if own is not None:
        return own
    first_context = await db.scalar(select(RunDesignContext).join(
        Run, Run.id == RunDesignContext.run_id,
    ).where(Run.chat_id == chat_id, Run.branch_id == branch_id).order_by(Run.queue_seq).limit(1))
    if first_context is not None:
        return await context_revision(db, chat_id, first_context)
    # Compatibility for branches created before context snapshots existed.
    from partner_agent.runs.service import visible_messages

    branch = await db.get(ChatBranch, branch_id)
    if branch is None or branch.chat_id != chat_id:
        raise APIError(404, "not_found", "Branch not found")
    messages = await visible_messages(db, branch, up_to=branch.head_message_id, include_all=False)
    return await visible_project(db, chat_id, messages)


async def context_revision(
    db: AsyncSession, chat_id: UUID, context: RunDesignContext,
) -> DesignRevision | None:
    if context.base_revision_id is None:
        return None
    row = await db.get(DesignRevision, context.base_revision_id)
    if row is None or row.chat_id != chat_id:
        raise APIError(409, "design_context_invalid", "Project context is unavailable")
    return row


async def visible_project(
    db: AsyncSession, chat_id: UUID, messages: list[Message],
) -> DesignRevision | None:
    identifiers = [message.id for message in messages if message.role == "assistant"]
    if not identifiers:
        return None
    return await db.scalar(select(DesignRevision).join(
        Run, Run.outcome["design_id"].astext == cast(DesignRevision.id, String),
    ).where(
        Run.chat_id == chat_id, DesignRevision.chat_id == chat_id, Run.status == "succeeded",
        Run.assistant_message_id.in_(identifiers),
    ).order_by(Run.queue_seq.desc()).limit(1))


async def capture_design_context(db: AsyncSession, run: Run) -> RunDesignContext:
    context = await db.get(RunDesignContext, run.id)
    if context is None:
        project = await latest_revision(db, run.chat_id, run.branch_id)
        context = RunDesignContext(run_id=run.id, base_revision_id=project.id if project else None)
        db.add(context)
        await db.flush()
    await context_revision(db, run.chat_id, context)
    return context


async def fork_project_base(
    db: AsyncSession, chat_id: UUID, original: Run | None, prefix: list[Message],
) -> DesignRevision | None:
    if original is not None:
        context = await db.get(RunDesignContext, original.id)
        if context is not None:
            return await context_revision(db, chat_id, context)
        # Pre-snapshot model results already record their exact input parent.
        identifier = (original.outcome or {}).get("design_id")
        if original.status == "succeeded" and isinstance(identifier, str):
            produced = await db.scalar(select(DesignRevision).where(
                DesignRevision.chat_id == chat_id, DesignRevision.branch_id == original.branch_id,
                DesignRevision.idempotency_key == original.id,
                cast(DesignRevision.id, String) == identifier,
            ))
            if produced is not None:
                return await context_revision(db, chat_id, RunDesignContext(
                    run_id=original.id, base_revision_id=produced.parent_id,
                ))
    return await visible_project(db, chat_id, prefix)


async def create_revision(
    db: AsyncSession, *, chat_id: UUID, branch_id: UUID, parent_id: UUID | None,
    conditions: DesignInput, key: UUID, digest: str, message_id: UUID | None = None,
) -> DesignRevision:
    previous = await latest_revision(db, chat_id, branch_id)
    if parent_id != (previous.id if previous else None):
        raise APIError(409, "design_revision_conflict", "Reload the project before saving")
    conditions = conditions.model_copy(deep=True)
    if message_id is not None:
        # Provenance is assigned by the server, never accepted from the model.
        old = DesignInput.model_validate(previous.input).exchanges if previous else []
        by_id = {flow.requirement_id: flow for flow in old if flow.requirement_id}
        for flow in conditions.exchanges:
            prior = by_id.get(flow.requirement_id) if flow.requirement_id else None
            flow.sources = list(prior.sources) if prior else []
            if prior is None:
                flow.requirement_id = "req-" + uuid4().hex
            if prior is None or flow.model_dump(exclude={"sources"}) != prior.model_dump(
                exclude={"sources"}
            ):
                flow.sources.append(SourceReference(kind="message", reference=str(message_id)))
            flow.sources = flow.sources[-20:]
    questions = clarification_questions(conditions)
    design = None if questions else plan(conditions)
    row = DesignRevision(
        id=uuid4(), chat_id=chat_id, branch_id=branch_id, parent_id=parent_id,
        revision=previous.revision + 1 if previous else 1,
        idempotency_key=key, request_sha256=digest,
        input=conditions.model_dump(mode="json"),
        questions=[question.model_dump() for question in questions],
        result=design.model_dump(mode="json") if design else None,
        artifacts=export_files(design) if design else {},
    )
    db.add(row)
    await db.flush()
    return row
