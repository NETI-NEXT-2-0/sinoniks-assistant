"""Explicit curator selection becomes draft units only after provenance checks."""

import hashlib
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from partner_agent.auth.policy import MANAGER_ROLES
from partner_agent.documents.canonical import CanonicalDocument
from partner_agent.documents.docling_adapter import (
    ParsedDocument,
    SourceBlock,
    approved_context,
    assert_reviewable,
)
from partner_agent.documents.edited_copy import EDITED_FORMAT, ReviewedCopy, copy_key, render_copy
from partner_agent.documents.limits import MAX_EVIDENCE_CHARS
from partner_agent.documents.service import lock_document
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import AuditEvent, User
from partner_agent.infra.models_documents import DisclosureUnit, DocumentVersion


def render_approved_docling(
    canonical: ParsedDocument, approved_block_ids: frozenset[UUID]
) -> list[tuple[UUID, str, str, dict[str, Any]]]:
    assert_reviewable(canonical)
    blocks = approved_context(canonical, approved_block_ids)
    by_path = {block.positions[0].element_path: block for block in blocks}
    note_by_text: dict[str, SourceBlock] = {}
    for block in blocks:
        if block.kind == "footnote":
            note_by_text.setdefault(block.text, block)
    global_notes = [note_by_text[note.text] for note in canonical.footnote_relationships]
    contexts: dict[UUID, list[SourceBlock]] = {}
    attached_notes: set[UUID] = set()
    for block in blocks:
        seen = {block.id}
        queue = [by_path[path] for path in block.related_paths]
        if block.kind != "footnote":
            queue.extend(global_notes)
        found: list[SourceBlock] = []
        while queue:
            current = queue.pop()
            if current.id in seen:
                continue
            seen.add(current.id)
            found.append(current)
            queue.extend(by_path[path] for path in current.related_paths)
        found.sort(key=lambda item: item.ordinal)
        contexts[block.id] = found
        if block.kind != "footnote":
            attached_notes.update(item.id for item in found if item.kind == "footnote")

    rendered: list[tuple[UUID, str, str, dict[str, Any]]] = []
    for block in blocks:
        if block.kind == "footnote" and block.id in attached_notes:
            continue
        context = contexts[block.id]
        text = block.text
        if context:
            label = (
                "Контекст источника (связь примечаний с фрагментом не определена):"
                if canonical.footnote_relationships
                else "Связанный контекст источника:"
            )
            text += "\n\n" + label + "\n" + "\n\n".join(item.text for item in context)
            if len(text) > MAX_EVIDENCE_CHARS:
                raise APIError(
                    422,
                    "disclosure_context_too_large",
                    "Linked context exceeds the current evidence limit",
                )
        locator: dict[str, Any] = {
            "positions": [position.model_dump(mode="json") for position in block.positions],
            "context": [
                {
                    "block_id": str(item.id),
                    "text_sha256": item.text_sha256,
                    "positions": [position.model_dump(mode="json") for position in item.positions],
                }
                for item in context
            ],
        }
        rendered.append((block.id, text, hashlib.sha256(text.encode()).hexdigest(), locator))
    return rendered


async def _prepare_approved_disclosure(
    db: AsyncSession,
    actor: User,
    canonical: ParsedDocument | CanonicalDocument,
    approved_block_ids: frozenset[UUID],
    expected_revision: int,
    artifact_hash: str,
    edited_copy: ReviewedCopy | None = None,
    copy_digest: str | None = None,
) -> tuple[DisclosureUnit, ...]:
    document = await lock_document(db, canonical.document_id, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    if document.revision != expected_revision:
        raise APIError(409, "revision_conflict", "Document changed")
    version = await db.scalar(
        select(DocumentVersion)
        .where(
            DocumentVersion.id == canonical.version_id,
            DocumentVersion.document_id == document.id,
        )
        .with_for_update()
    )
    if version is None:
        raise APIError(404, "not_found", "Version not found")
    if version.status != "pending" or version.source_sha256 != canonical.source_sha256:
        raise APIError(409, "version_state_conflict", "Version cannot accept this canonical input")
    if not version.canonical_key or version.canonical_sha256 != artifact_hash:
        raise APIError(409, "canonical_artifact_required", "Persist canonical artifact first")
    if version.pipeline_version != canonical.pipeline_version:
        raise APIError(409, "canonical_provenance_mismatch", "Canonical provenance mismatch")
    if len(approved_block_ids) > 1000:
        raise APIError(422, "too_many_blocks", "Select at most 1000 blocks")
    units: list[DisclosureUnit] = []
    if edited_copy is not None and copy_digest is not None:
        if isinstance(canonical, ParsedDocument):
            assert_reviewable(canonical)
        selected = iter(render_copy(edited_copy, copy_digest))
    elif isinstance(canonical, ParsedDocument):
        selected = iter(render_approved_docling(canonical, approved_block_ids))
    else:
        by_id = {block.id: block for block in canonical.blocks}
        if not approved_block_ids <= by_id.keys():
            from partner_agent.documents.canonical import CanonicalError

            raise CanonicalError("unknown_approved_block")
        selected = (
            (block.id, block.text, block.text_sha256, block.locator.model_dump())
            for block in canonical.blocks
            if block.id in approved_block_ids
        )
    for block_id, text, digest, locator in selected:
        if len(text) > MAX_EVIDENCE_CHARS:
            raise APIError(
                422,
                "disclosure_unit_too_large",
                "A selected fragment exceeds the current evidence limit",
            )
        units.append(
            DisclosureUnit(
                id=uuid5(version.id, f"disclosure:{block_id}:{document.policy_revision}"),
                version_id=version.id,
                block_id=block_id,
                text=text,
                text_sha256=digest,
                locator=locator,
                policy_revision=document.policy_revision,
            )
        )
    if not units:
        raise APIError(422, "empty_disclosure", "Select at least one block")
    db.add_all(units)
    db.add(
        AuditEvent(
            actor_id=actor.id,
            action="document.approve_disclosure",
            resource_id=str(version.id),
            result="success",
        )
    )
    version.approved_canonical_sha256 = artifact_hash
    version.disclosure_format = (
        EDITED_FORMAT
        if edited_copy is not None
        else "docling-context-v2"
        if isinstance(canonical, ParsedDocument)
        else "text-v1"
    )
    version.reviewed_copy_sha256 = copy_digest
    version.status = "processing"
    await db.flush()
    return tuple(units)


async def load_review_canonical(
    db: AsyncSession, actor: User, document_id: UUID, version_id: UUID
) -> tuple[ParsedDocument | CanonicalDocument, str]:
    from pydantic import ValidationError
    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents.ingest import storage

    document = await lock_document(db, document_id, actor)
    version = await db.get(DocumentVersion, version_id)
    if version is None or version.document_id != document_id:
        raise APIError(404, "not_found", "Version not found")
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    if version.status != "pending":
        raise APIError(409, "version_state_conflict", "Version cannot be reviewed")
    if (
        not version.canonical_key
        or not version.canonical_sha256
        or version.pipeline_version not in ("docling-adapter-v1", "canonical-v1")
    ):
        raise APIError(409, "canonical_artifact_required", "Canonical artifact required")
    key, source_hash = version.canonical_key, version.source_sha256
    artifact_hash = version.canonical_sha256
    pipeline = version.pipeline_version
    media_type = version.media_type
    await db.commit()

    def load() -> ParsedDocument | CanonicalDocument:
        adapter = storage()
        try:
            stored = adapter.get(adapter.derived_bucket, key)
            if hashlib.sha256(stored.data).hexdigest() != artifact_hash:
                raise APIError(409, "canonical_digest_mismatch", "Canonical artifact changed")
            if pipeline == "canonical-v1":
                return CanonicalDocument.model_validate_json(stored.data)
            return ParsedDocument.model_validate_json(stored.data)
        finally:
            adapter.client.close()

    try:
        canonical = await run_in_threadpool(load)
    except ValidationError:
        raise APIError(
            409, "canonical_artifact_invalid", "Canonical artifact requires repair"
        ) from None
    if (
        canonical.document_id != document_id
        or canonical.version_id != version_id
        or canonical.source_sha256 != source_hash
        or canonical.pipeline_version != pipeline
        or (isinstance(canonical, CanonicalDocument) and canonical.media_type != media_type)
    ):
        raise APIError(409, "canonical_provenance_mismatch", "Canonical provenance mismatch")
    document = await lock_document(db, document_id, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    current = await db.scalar(
        select(DocumentVersion)
        .where(DocumentVersion.id == version_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current is None or (
        current.canonical_key != key
        or current.source_sha256 != source_hash
        or current.canonical_sha256 != artifact_hash
        or current.pipeline_version != pipeline
        or current.media_type != media_type
    ):
        raise APIError(
            409, "canonical_artifact_changed", "Canonical artifact changed during review"
        )
    if current.status != "pending":
        raise APIError(409, "version_state_conflict", "Version cannot be reviewed")
    return canonical, artifact_hash


async def approve_stored_canonical(
    db: AsyncSession,
    actor: User,
    document_id: UUID,
    version_id: UUID,
    approved_block_ids: frozenset[UUID],
    expected_revision: int,
    expected_canonical_sha256: str,
    reviewed_text: str | None = None,
) -> tuple[DisclosureUnit, ...]:
    canonical, artifact_hash = await load_review_canonical(db, actor, document_id, version_id)
    if expected_canonical_sha256 != artifact_hash:
        raise APIError(409, "canonical_review_changed", "Review the current artifact")
    edited_copy, copy_digest = None, None
    if reviewed_text is not None:
        from starlette.concurrency import run_in_threadpool

        from partner_agent.documents.ingest import storage

        if approved_block_ids:
            raise APIError(422, "ambiguous_review", "Choose either source blocks or an edited copy")
        if isinstance(canonical, ParsedDocument):
            assert_reviewable(canonical)
        edited_copy = ReviewedCopy(
            document_id=document_id,
            version_id=version_id,
            canonical_sha256=artifact_hash,
            text=reviewed_text,
        )
        data = edited_copy.model_dump_json().encode()
        copy_digest = hashlib.sha256(data).hexdigest()
        render_copy(edited_copy, copy_digest)
        key = copy_key(document_id, version_id, copy_digest)

        def persist_copy() -> None:
            adapter = storage()
            try:
                adapter.put(adapter.derived_bucket, key, data, "application/json")
            finally:
                adapter.client.close()

        await run_in_threadpool(persist_copy)
    return await _prepare_approved_disclosure(
        db,
        actor,
        canonical,
        approved_block_ids,
        expected_revision,
        artifact_hash,
        edited_copy,
        copy_digest,
    )
