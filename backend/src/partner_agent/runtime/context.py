from typing import Any
from uuid import UUID

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from partner_agent.documents.access import access_predicate, unit_answer_predicate
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import User
from partner_agent.infra.models_designs import DesignRevision
from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion
from partner_agent.infra.models_stage2 import ContextManifest, Message, Run
from partner_agent.retrieval.service import validate_evidence_access


async def project_evidence(
    db: AsyncSession, actor: User, chat_id: UUID, project: DesignRevision,
) -> list[dict[str, Any]]:
    """Revalidate sources behind derived conditions, even outside trimmed history."""
    from partner_agent.documents.chat_files import restore_file_evidence

    lineage = select(
        DesignRevision.id, DesignRevision.parent_id, DesignRevision.idempotency_key,
    ).where(DesignRevision.id == project.id, DesignRevision.chat_id == chat_id).cte(
        "project_lineage", recursive=True,
    )
    lineage = lineage.union(select(
        DesignRevision.id, DesignRevision.parent_id, DesignRevision.idempotency_key,
    ).join(lineage, DesignRevision.id == lineage.c.parent_id).where(
        DesignRevision.chat_id == chat_id,
    ))
    manifests = (await db.scalars(select(ContextManifest).join(
        Run, Run.id == ContextManifest.run_id,
    ).join(lineage, Run.id == lineage.c.idempotency_key).where(
        Run.chat_id == chat_id, Run.outcome["design_id"].astext == cast(lineage.c.id, String),
    ))).all()
    identifiers = sorted({
        identifier for manifest in manifests for identifier in manifest.evidence_ids
    })
    restored = []
    for identifier in identifiers:
        if identifier.startswith("attachment:"):
            try:
                restored.append(await restore_file_evidence(db, actor, chat_id, identifier))
            except APIError:
                raise APIError(409, "source_context_changed", "Project sources changed") from None
            continue
        try:
            unit_id = UUID(identifier)
        except ValueError:
            raise APIError(409, "source_context_changed", "Project sources changed") from None
        row = (await db.execute(select(DisclosureUnit, DocumentVersion.document_id).join(
            DocumentVersion, DocumentVersion.id == DisclosureUnit.version_id,
        ).join(Document, Document.id == DocumentVersion.document_id).where(
            DisclosureUnit.id == unit_id, Document.active_version_id == DisclosureUnit.version_id,
            access_predicate(actor, "answer"), unit_answer_predicate(actor),
        ))).first()
        if row is None:
            raise APIError(409, "source_context_changed", "Project sources changed")
        unit, document_id = row
        restored.append({"evidence_id": str(unit.id), "document_id": str(document_id),
                         "version_id": str(unit.version_id), "excerpt": unit.text,
                         "locator": unit.locator})
    await validate_evidence_access(db, actor, restored, chat_id=chat_id)
    return restored


async def answer_history(
    db: AsyncSession, actor: User, run_id: UUID
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    from partner_agent.documents.chat_files import restore_file_evidence

    current_run = await db.get(Run, run_id)
    if current_run is None or current_run.user_id != actor.id:
        raise APIError(403, "access_revoked", "Access revoked")
    manifest = await db.scalar(select(ContextManifest).where(ContextManifest.run_id == run_id))
    if manifest is None:
        raise RuntimeError("Context manifest is missing")
    identifiers = manifest.message_ids[-200:]
    ids = [UUID(value) for value in identifiers]
    rows = (await db.scalars(select(Message).where(Message.id.in_(ids)))).all() if ids else []
    by_id = {str(item.id): item for item in rows}
    exchanges: dict[UUID, list[Message]] = {}
    for identifier in identifiers:
        message = by_id.get(identifier)
        if message is None or message.revision != manifest.message_revisions.get(identifier):
            raise APIError(409, "history_context_changed", "History context changed")
        if message.run_id != run_id:
            exchanges.setdefault(message.run_id, []).append(message)
    selected: list[list[dict[str, str]]] = []
    sources: dict[str, dict[str, Any]] = {}
    size = 0
    for previous_id, messages in reversed(list(exchanges.items())):
        previous = await db.get(Run, previous_id)
        if previous is None or previous.status != "succeeded" or previous.user_id != actor.id:
            continue
        if current_run.request_mode == "reference" and (
            previous.request_mode == "design"
            or (previous.outcome or {}).get("answer_mode") in ("design", "project_explanation")
        ):
            continue
        questions = [item.content for item in messages if item.role == "user"]
        answers = [item.content for item in messages if item.role == "assistant"]
        if not questions or len(answers) != 1:
            continue
        pair = [{"role": "user", "content": "\n".join(questions)},
                {"role": "assistant", "content": answers[0]}]
        pair_size = sum(len(item["content"]) for item in pair)
        if len(selected) >= 10 or size + pair_size > 12000:
            break
        previous_context = await db.scalar(
            select(ContextManifest).where(ContextManifest.run_id == previous_id)
        )
        evidence_ids = previous_context.evidence_ids if previous_context else []
        if ((previous.outcome or {}).get("profile") != "offline_fixture" and not evidence_ids
                and (previous.outcome or {}).get("answer_mode") not in (
                    "conversation", "design", "project_explanation", "insufficient",
                )):
            continue
        if len(set(sources) | set(evidence_ids)) > 80:
            break
        evidence = []
        for evidence_id in evidence_ids:
            if evidence_id.startswith("attachment:"):
                try:
                    evidence.append(await restore_file_evidence(
                        db, actor, current_run.chat_id, evidence_id
                    ))
                except APIError:
                    break
                continue
            row = (await db.execute(
                select(DisclosureUnit, DocumentVersion.document_id)
                .join(DocumentVersion, DocumentVersion.id == DisclosureUnit.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(DisclosureUnit.id == UUID(evidence_id),
                       Document.active_version_id == DisclosureUnit.version_id,
                       access_predicate(actor, "answer"), unit_answer_predicate(actor))
            )).first()
            if row is None:
                break
            unit, document_id = row
            evidence.append({"evidence_id": str(unit.id), "document_id": str(document_id),
                             "version_id": str(unit.version_id), "excerpt": unit.text,
                             "locator": unit.locator})
        if len(evidence) != len(evidence_ids):
            continue
        selected.append(pair)
        size += pair_size
        sources.update({item["evidence_id"]: item for item in evidence})
    try:
        await validate_evidence_access(
            db, actor, list(sources.values()), chat_id=current_run.chat_id
        )
    except APIError as error:
        if error.code != "source_context_changed":
            raise
        return [], []
    return [message for pair in reversed(selected) for message in pair], list(sources.values())
