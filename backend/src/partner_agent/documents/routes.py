"""Curator-owned knowledge metadata, review and publication endpoints."""

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, select

from partner_agent.auth.policy import MANAGER_ROLES
from partner_agent.auth.routes import DB, api_rate_limit, authenticated, unsafe_session
from partner_agent.documents.service import (
    create_document,
    lock_document,
    publish_version,
    unpublish,
)
from partner_agent.documents.web_source import normalize_source_url
from partner_agent.infra.errors import APIError
from partner_agent.infra.models_documents import Document, DocumentVersion

router = APIRouter(
    prefix="/api/v1/knowledge", tags=["documents"], dependencies=[Depends(api_rate_limit)]
)


class CreateDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)


class RevisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


def document_dto(document: Document) -> dict[str, Any]:
    return {
        "id": str(document.id),
        "title": document.title,
        "scope": document.scope,
        "workspace_id": str(document.workspace_id) if document.workspace_id else None,
        "revision": document.revision,
        "policy_revision": document.policy_revision,
        "acl_revision": document.acl_revision,
        "active_version_id": str(document.active_version_id)
        if document.active_version_id
        else None,
        "last_published_version_id": str(document.last_published_version_id)
        if document.last_published_version_id
        else None,
    }


@router.post("/documents", status_code=201)
async def create(body: CreateDocument, request: Request, db: DB) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    document = await create_document(db, actor, body.title, "knowledge")
    result = document_dto(document)
    await db.commit()
    return result


@router.get("/documents")
async def list_documents(
    request: Request,
    db: DB,
    before: UUID | None = None,
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    _, actor = await authenticated(request, db)
    query = select(Document).where(
        Document.owner_id == actor.id, Document.scope == "knowledge", Document.deleted_at.is_(None)
    )
    if before is not None:
        # Cursor IDs are ordering boundaries; no foreign title/content is loaded.
        query = query.where(Document.id < before)
    rows = list((await db.scalars(query.order_by(Document.id.desc()).limit(limit + 1))).all())
    result = {
        "items": [document_dto(row) for row in rows[:limit]],
        "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
    }
    await db.commit()
    return result


@router.get("/documents/{document_id}")
async def get_document(document_id: UUID, request: Request, db: DB) -> dict[str, Any]:
    _, actor = await authenticated(request, db)
    document = await lock_document(db, document_id, actor)
    result = document_dto(document)
    await db.commit()
    return result


@router.get("/summary")
async def corpus_summary(request: Request, response: Response, db: DB) -> dict[str, int]:
    from partner_agent.retrieval.service import knowledge_corpus_summary

    _, actor = await authenticated(request, db)
    if actor.role not in MANAGER_ROLES:
        raise APIError(403, "curator_required", "Curator permission required")
    result = await knowledge_corpus_summary(db, actor)
    response.headers["Cache-Control"] = "private, no-store"
    await db.commit()
    return result


@router.get("/versions/{version_id}")
async def get_version(version_id: UUID, request: Request, db: DB) -> dict[str, Any]:
    from sqlalchemy import func

    from partner_agent.documents.limits import MAX_EVIDENCE_CHARS
    from partner_agent.infra.models_documents import DisclosureUnit

    _, actor = await authenticated(request, db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    document = await lock_document(db, identifier, actor)
    version = await db.get(DocumentVersion, version_id)
    assert version is not None
    oversized = await db.scalar(
        select(DisclosureUnit.id).where(
            DisclosureUnit.version_id == version_id,
            DisclosureUnit.policy_revision == document.policy_revision,
            func.char_length(DisclosureUnit.text) > MAX_EVIDENCE_CHARS,
        ).limit(1)
    )
    result = {
        "id": str(version.id),
        "document_id": str(identifier),
        "generation": version.generation,
        "web_source": version.web_source(),
        "status": version.status,
        "error_code": version.error_code,
        "index_error": (
            version.index_manifest.get("code")
            if version.index_manifest and version.index_manifest.get("state") in ("error", "failed")
            else None
        ),
        "needs_reingest": bool(
            version.pipeline_version == "docling-adapter-v1"
            and version.status in ("processing", "ready_for_review")
            and version.disclosure_format not in ("docling-context-v2", "curator-edited-v1")
        ),
        "oversized_units": oversized is not None,
        "parser_attempts": version.parser_attempts,
        "text_attempts": version.text_attempts,
        "text_retry_at": version.text_retry_at.isoformat() if version.text_retry_at else None,
        "index_attempts": version.index_attempts,
        "index_retry_at": version.index_retry_at.isoformat() if version.index_retry_at else None,
        "parser_retry_at": (
            version.parser_retry_at.isoformat() if version.parser_retry_at else None
        ),
        "review_available": bool(
            version.status == "pending" and version.canonical_key and version.canonical_sha256
        ),
        "created_at": version.created_at.isoformat(),
    }
    await db.commit()
    return result


class PublishBody(RevisionBody):
    confirm_deny_review: bool = False


@router.post("/versions/{version_id}/publish")
async def publish(version_id: UUID, body: PublishBody, request: Request, db: DB) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    document = await publish_version(
        db, actor, identifier, version_id, body.expected_revision, body.confirm_deny_review
    )
    result = document_dto(document)
    await db.commit()
    return result


@router.post("/documents/{document_id}/unpublish")
async def withdraw(
    document_id: UUID, body: RevisionBody, request: Request, db: DB
) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    document = await unpublish(db, actor, document_id, body.expected_revision)
    result = document_dto(document)
    await db.commit()
    return result


@router.post("/documents/{document_id}/versions", status_code=202)
async def upload_version(
    document_id: UUID,
    request: Request,
    db: DB,
    expected_revision: int = Query(ge=1),
    source_url: str | None = Query(default=None, max_length=2048),
    source_captured_on: date | None = None,
) -> dict[str, Any]:
    from partner_agent.documents.canonical import MAX_FILE_BYTES, CanonicalError
    from partner_agent.documents.ingest import (
        DOCLING_MEDIA_TYPES,
        TEXT_MEDIA_TYPES,
        admit_version,
        store_original,
    )

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    await lock_document(db, document_id, actor)
    await db.commit()  # Do not hold a row lock while receiving a potentially slow body.
    raw_key = request.headers.get("idempotency-key")
    try:
        idempotency_key = UUID(raw_key) if raw_key is not None else None
    except ValueError:
        raise APIError(422, "invalid_idempotency_key", "A UUID upload key is required") from None
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if media_type not in TEXT_MEDIA_TYPES and media_type not in DOCLING_MEDIA_TYPES:
        raise APIError(415, "unsupported_media_type", "Unsupported document type")
    if (source_url is None) != (source_captured_on is None):
        raise APIError(422, "invalid_web_source", "Source URL and capture date are both required")
    if source_url is not None:
        try:
            source_url = normalize_source_url(source_url)
        except ValueError:
            raise APIError(
                422, "invalid_web_source", "A valid HTTPS page URL is required"
            ) from None
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_FILE_BYTES:
            raise APIError(413, "file_size_limit", "File exceeds 25 MiB")
        data.extend(chunk)
    try:
        version, created = await admit_version(
            db, actor, document_id, expected_revision, bytes(data), media_type, idempotency_key,
            source_url=source_url, source_captured_on=source_captured_on,
        )
    except CanonicalError:
        raise APIError(422, "invalid_text_source", "Valid nonempty UTF-8 text required") from None
    result = {
        "id": str(version.id), "document_id": str(document_id), "status": version.status,
        "reused": not created,
    }
    key = version.original_key
    needs_original = version.status in ("pending", "failed") and version.canonical_key is None
    await db.commit()
    # Replaying exact bytes also restores a pending upload interrupted before PUT.
    if needs_original:
        await store_original(key, bytes(data), media_type)
        document = await lock_document(db, document_id, actor)
        if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
            raise APIError(403, "curator_required", "Curator permission required")
        await db.commit()
    return result


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=32768)
    limit: int = Field(default=8, ge=1, le=100)


@router.post("/search")
async def search_documents(body: SearchBody, request: Request, db: DB) -> dict[str, Any]:
    from partner_agent.retrieval.ranking import PROFILE
    from partner_agent.retrieval.service import MAX_SCAN_CANDIDATES, search_knowledge

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    items = await search_knowledge(db, actor, body.query, body.limit)
    await db.commit()
    return {"items": items, "profile": PROFILE,
            "candidate_limit": MAX_SCAN_CANDIDATES}


class ApproveBody(RevisionBody):
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    block_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    reviewed_text: str | None = Field(default=None, min_length=1, max_length=1_000_000)


@router.post("/versions/{version_id}/approve", status_code=202)
async def approve_version(
    version_id: UUID, body: ApproveBody, request: Request, db: DB
) -> dict[str, Any]:
    from partner_agent.documents.canonical import CanonicalError
    from partner_agent.documents.disclosure import approve_stored_canonical

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    try:
        units = await approve_stored_canonical(
            db, actor, identifier, version_id, frozenset(body.block_ids), body.expected_revision,
            body.canonical_sha256, body.reviewed_text,
        )
    except CanonicalError:
        raise APIError(
            409, "canonical_review_required", "Canonical quality/context requires review"
        ) from None
    await db.commit()
    return {"version_id": str(version_id), "status": "processing", "unit_count": len(units)}


@router.get("/versions/{version_id}/review")
async def review_version(
    version_id: UUID, request: Request, response: Response, db: DB,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    """Return source blocks only to their curator for an explicit selection."""
    from partner_agent.documents.disclosure import load_review_canonical
    from partner_agent.documents.docling_adapter import ParsedDocument, review_context
    from partner_agent.documents.quality import has_mixed_script_identifier

    _, actor = await authenticated(request, db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    canonical, artifact_hash = await load_review_canonical(db, actor, identifier, version_id)
    response.headers["Cache-Control"] = "private, no-store"
    blocks = canonical.blocks[offset : offset + limit]
    related, parents, required_notes, unresolved, quality_block = (
        review_context(canonical)
        if isinstance(canonical, ParsedDocument) else ({}, {}, (), False, None)
    )
    result = {
        "version_id": str(version_id),
        "canonical_sha256": artifact_hash,
        "total_blocks": len(canonical.blocks),
        "warnings": list(canonical.warnings),
        "blocks": [
            {
                **block.model_dump(mode="json"),
                "required_context": related.get(block.id, ()),
                "parent_context": parents.get(block.id, ()),
                "mixed_script_identifier": (
                    isinstance(canonical, ParsedDocument)
                    and has_mixed_script_identifier(block.text)
                ),
                "needs_parent": bool(
                    isinstance(canonical, ParsedDocument) and block.kind == "footnote"
                    and canonical.footnote_relationships and not parents.get(block.id)
                ),
            }
            for block in blocks
        ],
        "required_notes": required_notes,
        "unresolved_notes": unresolved,
        "quality_block": quality_block,
        "next_offset": (
            offset + len(blocks) if offset + len(blocks) < len(canonical.blocks) else None
        ),
    }
    await db.commit()
    return result


@router.put("/versions/{version_id}/original", status_code=202)
async def restore_original(
    version_id: UUID, request: Request, db: DB, expected_revision: int = Query(ge=1)
) -> dict[str, Any]:
    """Retry an interrupted upload only with bytes matching the admitted source."""
    import hashlib

    from partner_agent.documents.canonical import MAX_FILE_BYTES
    from partner_agent.documents.ingest import store_original
    from partner_agent.infra.models import AuditEvent

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    document = await lock_document(db, identifier, actor)
    version = await db.scalar(
        select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update()
    )
    if version is None:
        raise APIError(404, "not_found", "Version not found")
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    if document.revision != expected_revision:
        raise APIError(409, "revision_conflict", "Document changed")
    if version.status not in ("pending", "failed") or version.canonical_key is not None:
        raise APIError(409, "version_state_conflict", "Original cannot be restored")
    key, media_type, digest, size = (
        version.original_key, version.media_type, version.source_sha256, version.size_bytes
    )
    await db.commit()
    supplied_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if supplied_type != media_type:
        raise APIError(415, "unsupported_media_type", "Original media type required")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > min(MAX_FILE_BYTES, size):
            raise APIError(413, "file_size_limit", "Original size exceeded")
        data.extend(chunk)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise APIError(409, "source_checksum_mismatch", "Original bytes do not match")
    document = await lock_document(db, identifier, actor)
    version = await db.scalar(
        select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        version is None or version.document_id != identifier or version.original_key != key
        or version.source_sha256 != digest or version.size_bytes != size
        or version.media_type != media_type or version.status not in ("pending", "failed")
        or version.canonical_key is not None or document.revision != expected_revision
        or actor.role not in MANAGER_ROLES or document.scope != "knowledge"
    ):
        raise APIError(409, "version_state_conflict", "Version changed during upload")
    await db.commit()
    await store_original(key, bytes(data), media_type)
    document = await lock_document(db, identifier, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    db.add(AuditEvent(
        actor_id=actor.id, action="document.restore_original", resource_id=str(version_id),
        result="success",
    ))
    await db.commit()
    return {"version_id": str(version_id), "status": "original_stored"}


@router.post("/versions/{version_id}/retry", status_code=202)
async def retry_ingestion(
    version_id: UUID, body: RevisionBody, request: Request, db: DB
) -> dict[str, Any]:
    """Reset the appropriate ingestion retry budget after its cause is resolved."""
    from partner_agent.infra.models import AuditEvent

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    document = await lock_document(db, identifier, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    if document.revision != body.expected_revision:
        raise APIError(409, "revision_conflict", "Document changed")
    version = await db.scalar(
        select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update()
    )
    if version is None or (
        version.document_id != identifier
        or version.pipeline_version not in ("canonical-v1", "docling-adapter-v1")
        or version.status not in ("pending", "failed") or version.canonical_key is not None
        or not version.error_code
    ):
        raise APIError(409, "version_state_conflict", "Ingestion cannot be retried")
    version.status = "pending"
    version.error_code = None
    if version.pipeline_version == "canonical-v1":
        version.text_attempts = 0
        version.text_retry_at = None
        action = "document.retry_text"
    else:
        version.parser_attempts = 0
        version.parser_retry_at = None
        action = "document.retry_parser"
    document.revision += 1
    db.add(AuditEvent(
        actor_id=actor.id, action=action, resource_id=str(version_id),
        result="success",
    ))
    await db.commit()
    return {"version_id": str(version_id), "status": "pending", "revision": document.revision}


class DirectGrantBody(RevisionBody):
    allow_answer: bool
    allow_original: bool


class WorkspaceBindingBody(RevisionBody):
    workspace_id: UUID


@router.put("/documents/{document_id}/workspace")
async def bind_workspace(
    document_id: UUID, body: WorkspaceBindingBody, request: Request, db: DB
) -> dict[str, Any]:
    from partner_agent.auth.audience_lock import audience_write_lock
    from partner_agent.infra.models import AuditEvent, Membership

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    await audience_write_lock(db)
    document = await lock_document(db, document_id, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    if document.revision != body.expected_revision:
        raise APIError(409, "revision_conflict", "Document changed")
    if document.workspace_id is not None and document.workspace_id != body.workspace_id:
        raise APIError(409, "workspace_immutable", "Document already belongs to a workspace")
    if await db.get(Membership, (body.workspace_id, actor.id)) is None:
        raise APIError(404, "not_found", "Workspace not found")
    if document.workspace_id is None:
        document.workspace_id = body.workspace_id
        document.revision += 1
        document.acl_revision += 1
        db.add(AuditEvent(actor_id=actor.id, action="document.workspace.bind",
                          resource_id=str(document_id), result="success"))
    result = document_dto(document)
    await db.commit()
    return result


@router.put("/documents/{document_id}/group-grants/{group_id}")
async def set_group_grant(
    document_id: UUID, group_id: UUID, body: DirectGrantBody, request: Request, db: DB
) -> dict[str, Any]:
    from partner_agent.auth.audience_lock import audience_write_lock
    from partner_agent.infra.models import AuditEvent, Group, Membership
    from partner_agent.infra.models_documents import DocumentGroupGrant

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    await audience_write_lock(db)
    document = await lock_document(db, document_id, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    if document.revision != body.expected_revision:
        raise APIError(409, "revision_conflict", "Document changed")
    group = await db.get(Group, group_id)
    if (document.workspace_id is None or group is None
            or group.workspace_id != document.workspace_id):
        raise APIError(404, "not_found", "Group not found")
    if await db.get(Membership, (document.workspace_id, actor.id)) is None:
        raise APIError(403, "workspace_membership_required", "Workspace membership required")
    grant = await db.get(DocumentGroupGrant, (document_id, group_id))
    changed = False
    if grant is None and (body.allow_answer or body.allow_original):
        db.add(DocumentGroupGrant(document_id=document_id, group_id=group_id,
                                  workspace_id=document.workspace_id,
                                  allow_answer=body.allow_answer,
                                  allow_original=body.allow_original))
        changed = True
    elif grant is not None and not (body.allow_answer or body.allow_original):
        await db.delete(grant)
        changed = True
    elif grant is not None and (grant.allow_answer, grant.allow_original) != (
        body.allow_answer, body.allow_original
    ):
        grant.allow_answer, grant.allow_original = body.allow_answer, body.allow_original
        changed = True
    if changed:
        document.revision += 1
        document.acl_revision += 1
        db.add(AuditEvent(actor_id=actor.id, action="document.group_grant.update",
                          resource_id=f"{document_id}:{group_id}", result="success"))
    result = {"document_id": str(document_id), "group_id": str(group_id),
              "allow_answer": body.allow_answer, "allow_original": body.allow_original,
              "revision": document.revision, "acl_revision": document.acl_revision}
    await db.commit()
    return result


@router.get("/audience-workspaces")
async def list_audience_workspaces(
    request: Request, response: Response, db: DB,
    after: UUID | None = None, limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    from partner_agent.auth.audience_lock import audience_read_lock
    from partner_agent.infra.models import Membership, Workspace

    _, actor = await authenticated(request, db)
    await audience_read_lock(db)
    await db.refresh(actor)
    if actor.disabled:
        raise APIError(403, "access_revoked", "Access revoked")
    if actor.role not in MANAGER_ROLES:
        raise APIError(403, "curator_required", "Curator permission required")
    query = (select(Workspace).join(Membership, Membership.workspace_id == Workspace.id)
             .where(Membership.user_id == actor.id).order_by(Workspace.id).limit(limit + 1))
    if after is not None:
        query = query.where(Workspace.id > after)
    rows = list((await db.scalars(query)).all())
    response.headers["Cache-Control"] = "private, no-store"
    result = {"items": [{"id": str(item.id), "name": item.name} for item in rows[:limit]],
              "next_after": str(rows[limit - 1].id) if len(rows) > limit else None}
    await db.commit()
    return result


@router.get("/documents/{document_id}/group-grants")
async def list_group_grants(
    document_id: UUID, request: Request, response: Response, db: DB,
    after: UUID | None = None, limit: int = Query(default=30, ge=1, le=100),
    expected_acl_revision: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    from sqlalchemy import exists

    from partner_agent.auth.audience_lock import audience_read_lock
    from partner_agent.infra.models import Group, Membership
    from partner_agent.infra.models_documents import DisclosureGroupDeny, DocumentGroupGrant

    _, actor = await authenticated(request, db)
    await audience_read_lock(db)
    document = await lock_document(db, document_id, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    if document.workspace_id is None:
        raise APIError(409, "workspace_required", "Document has no workspace")
    if await db.get(Membership, (document.workspace_id, actor.id)) is None:
        raise APIError(403, "workspace_membership_required", "Workspace membership required")
    if expected_acl_revision is not None and expected_acl_revision != document.acl_revision:
        raise APIError(409, "revision_conflict", "Permissions changed")
    restricted = exists(select(DisclosureGroupDeny.group_id).where(
        DisclosureGroupDeny.document_id == document_id, DisclosureGroupDeny.group_id == Group.id
    ).correlate(Group))
    query = (select(Group, DocumentGroupGrant, restricted)
             .outerjoin(DocumentGroupGrant, and_(DocumentGroupGrant.group_id == Group.id,
                                                  DocumentGroupGrant.document_id == document_id))
             .where(Group.workspace_id == document.workspace_id)
             .order_by(Group.id).limit(limit + 1))
    if after is not None:
        query = query.where(Group.id > after)
    rows = (await db.execute(query)).all()
    response.headers["Cache-Control"] = "private, no-store"
    result = {"acl_revision": document.acl_revision,
              "items": [{"id": str(group.id), "name": group.name,
                         "allow_answer": bool(grant and grant.allow_answer),
                         "allow_original": bool(grant and grant.allow_original),
                         "has_denies": has_denies}
                        for group, grant, has_denies in rows[:limit]],
              "next_after": str(rows[limit - 1][0].id) if len(rows) > limit else None}
    await db.commit()
    return result


@router.get("/documents/{document_id}/group-grants/{group_id}/members")
async def list_document_group_members(
    document_id: UUID, group_id: UUID, request: Request, response: Response, db: DB,
    after: UUID | None = None, limit: int = Query(default=50, ge=1, le=100),
    expected_acl_revision: int | None = Query(default=None, ge=1),
    expected_membership_revision: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """List a document-related group's members without granting any content access."""
    from sqlalchemy import exists

    from partner_agent.auth.audience_lock import audience_read_lock
    from partner_agent.documents.access import document_access_allowed
    from partner_agent.infra.models import Group, GroupMember, Membership, User
    from partner_agent.infra.models_documents import DisclosureGroupDeny, DocumentGroupGrant

    _, actor = await authenticated(request, db)
    await audience_read_lock(db)
    document = await lock_document(db, document_id, actor)
    if document.scope != "knowledge" or not document_access_allowed(actor, document, None, "acl"):
        raise APIError(403, "curator_required", "Curator permission required")
    if document.workspace_id is None:
        raise APIError(404, "not_found", "Group not found")
    if await db.get(Membership, (document.workspace_id, actor.id)) is None:
        raise APIError(403, "workspace_membership_required", "Workspace membership required")
    group = await db.get(Group, group_id)
    if group is None or group.workspace_id != document.workspace_id:
        raise APIError(404, "not_found", "Group not found")
    if (
        (expected_acl_revision is not None and expected_acl_revision != document.acl_revision)
        or (expected_membership_revision is not None
            and expected_membership_revision != group.membership_revision)
    ):
        raise APIError(409, "revision_conflict", "Audience changed")
    grant = await db.get(DocumentGroupGrant, (document_id, group_id))
    restricted = await db.scalar(select(exists().where(
        DisclosureGroupDeny.document_id == document_id,
        DisclosureGroupDeny.group_id == group_id,
    )))
    if grant is None and not restricted:
        raise APIError(404, "not_found", "Group not linked to document")
    query = select(User).join(GroupMember, GroupMember.user_id == User.id).where(
        GroupMember.group_id == group_id, GroupMember.workspace_id == document.workspace_id,
    )
    if after is not None:
        query = query.where(User.id > after)
    rows = list((await db.scalars(query.order_by(User.id).limit(limit + 1))).all())
    response.headers["Cache-Control"] = "private, no-store"
    result = {
        "group_id": str(group_id), "acl_revision": document.acl_revision,
        "membership_revision": group.membership_revision,
        "items": [{"user_id": str(user.id), "login": user.normalized_login,
                   "disabled": user.disabled} for user in rows[:limit]],
        "next_after": str(rows[limit - 1].id) if len(rows) > limit else None,
    }
    await db.commit()
    return result


class SubjectReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(min_length=1, max_length=128)
    version_id: UUID | None = None
    after: UUID | None = None
    snapshot_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@router.post("/documents/{document_id}/access-review")
async def access_review(
    document_id: UUID, body: SubjectReviewBody, request: Request, response: Response, db: DB
) -> dict[str, Any]:
    """Review one exact account and one version under the document lock."""
    import hashlib
    import json

    from sqlalchemy import and_

    from partner_agent.auth.audience_lock import audience_read_lock
    from partner_agent.auth.security import normalize_login
    from partner_agent.documents.access import (
        document_access_allowed,
        group_access_allowed,
        group_unit_denied,
    )
    from partner_agent.infra.models import Group, GroupMember, Membership, User
    from partner_agent.infra.models_documents import (
        DisclosureAnswerDeny,
        DisclosureGroupDeny,
        DisclosureUnit,
        DocumentGrant,
        DocumentGroupGrant,
    )

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    await audience_read_lock(db)
    document = await lock_document(db, document_id, actor)
    if document.scope != "knowledge" or not document_access_allowed(actor, document, None, "acl"):
        raise APIError(403, "curator_required", "Curator permission required")
    try:
        login = normalize_login(body.login)
    except ValueError as exc:
        raise APIError(422, "invalid_grantee", "Active user required") from exc
    target = await db.scalar(select(User).where(User.normalized_login == login))
    if target is None:
        raise APIError(404, "subject_not_found", "User not found")
    grant = await db.get(DocumentGrant, (document_id, target.id))
    group_answer = await group_access_allowed(db, target, document, "answer")
    group_original = await group_access_allowed(db, target, document, "original")
    policy_answer = document_access_allowed(target, document, grant, "answer", group_answer)
    policy_original = document_access_allowed(target, document, grant, "original", group_original)
    memberships: list[dict[str, Any]] = []
    groups_incomplete = False
    groups_hidden = document.workspace_id is not None and (
        await db.get(Membership, (document.workspace_id, actor.id)) is None
    )
    if document.workspace_id is not None and not groups_hidden:
        group_rows = (await db.execute(
            select(Group, DocumentGroupGrant)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .join(Membership, and_(Membership.workspace_id == Group.workspace_id,
                                   Membership.user_id == GroupMember.user_id))
            .outerjoin(DocumentGroupGrant, and_(DocumentGroupGrant.group_id == Group.id,
                                                 DocumentGroupGrant.document_id == document_id))
            .where(Group.workspace_id == document.workspace_id, GroupMember.user_id == target.id)
            .order_by(Group.id).limit(101)
        )).all()
        memberships = [{"id": str(group.id), "name": group.name,
                        "allow_answer": bool(item and item.allow_answer),
                        "allow_original": bool(item and item.allow_original)}
                       for group, item in group_rows[:100]]
        groups_incomplete = len(group_rows) > 100
    # Never fingerprint undisclosed group IDs: an unkeyed token would allow
    # guessing them. If the list is truncated, consistent paging is unavailable.
    member_ids: list[str] = []
    snapshot_complete = not groups_incomplete
    membership_snapshot_checked = document.workspace_id is None
    if document.workspace_id is not None and not groups_hidden and snapshot_complete:
        member_ids = [str(group.id) for group, _ in group_rows]
        membership_snapshot_checked = True
    snapshot_token = hashlib.sha256(json.dumps({
        "actor": str(actor.id), "document": str(document.id),
        "acl_revision": document.acl_revision, "user": str(target.id),
        "user_revision": target.revision, "version": str(body.version_id),
        "groups_hidden": groups_hidden, "group_ids": member_ids,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest() if snapshot_complete else None
    if body.snapshot_token is not None and body.snapshot_token != snapshot_token:
        raise APIError(409, "revision_conflict", "Access review changed")
    entries: list[dict[str, Any]] = []
    next_after = None
    if body.version_id is not None:
        version = await db.get(DocumentVersion, body.version_id)
        if version is None or version.document_id != document_id:
            raise APIError(404, "not_found", "Version not found")
        query = (
            select(DisclosureUnit, DisclosureAnswerDeny.user_id)
            .outerjoin(DisclosureAnswerDeny, and_(
                DisclosureAnswerDeny.unit_id == DisclosureUnit.id,
                DisclosureAnswerDeny.user_id == target.id,
            ))
            .where(DisclosureUnit.version_id == version.id)
        )
        if body.after is not None:
            query = query.where(DisclosureUnit.id > body.after)
        rows = (await db.execute(query.order_by(DisclosureUnit.id).limit(51))).all()
        for unit, denied in rows[:50]:
            group_denied = await group_unit_denied(db, target, document, unit.id)
            denying = (await db.execute(
                select(Group.id, Group.name)
                .join(DisclosureGroupDeny, DisclosureGroupDeny.group_id == Group.id)
                .join(GroupMember, GroupMember.group_id == Group.id)
                .join(Membership, and_(Membership.workspace_id == Group.workspace_id,
                                       Membership.user_id == GroupMember.user_id))
                .where(DisclosureGroupDeny.unit_id == unit.id,
                       Group.workspace_id == document.workspace_id,
                       GroupMember.user_id == target.id)
                .order_by(Group.id).limit(11)
            )).all() if group_denied and not groups_hidden else []
            entries.append({"id": str(unit.id), "excerpt": unit.text[:1600],
                            "truncated": len(unit.text) > 1600, "denied": denied is not None,
                            "group_denied": group_denied,
                            "denying_groups": [{"id": str(row.id), "name": row.name}
                                               for row in denying[:10]],
                            "denying_groups_incomplete": len(denying) > 10,
                            "policy_allows_answer": policy_answer and denied is None
                            and not group_denied})
        if len(rows) > 50:
            next_after = str(rows[49][0].id)
    response.headers["Cache-Control"] = "private, no-store"
    result = {
        "user_id": str(target.id), "login": target.normalized_login,
        "owner": target.id == document.owner_id, "disabled": target.disabled,
        "revision": document.revision,
        "allow_answer": bool(grant and grant.allow_answer),
        "allow_original": bool(grant and grant.allow_original),
        "policy_allows_answer": policy_answer, "policy_allows_original": policy_original,
        "groups": memberships, "groups_incomplete": groups_incomplete,
        "groups_hidden": groups_hidden, "snapshot_token": snapshot_token,
        "membership_snapshot_checked": membership_snapshot_checked,
        "units": entries, "next_after": next_after,
    }
    await db.commit()
    return result


@router.put("/documents/{document_id}/grants/{user_id}")
async def set_direct_grant(
    document_id: UUID, user_id: UUID, body: DirectGrantBody, request: Request, db: DB
) -> dict[str, Any]:
    """Grant or revoke independent answer and original permissions for one user."""
    from partner_agent.documents.access import document_access_allowed
    from partner_agent.infra.models import AuditEvent, User
    from partner_agent.infra.models_documents import DocumentGrant

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    document = await lock_document(db, document_id, actor)
    if document.scope != "knowledge" or not document_access_allowed(actor, document, None, "acl"):
        raise APIError(403, "curator_required", "Curator permission required")
    if document.revision != body.expected_revision:
        raise APIError(409, "revision_conflict", "Document changed")
    if user_id == actor.id:
        raise APIError(422, "invalid_grantee", "Owner permission is implicit")
    grant = await db.get(DocumentGrant, (document_id, user_id))
    if body.allow_answer or body.allow_original:
        target = await db.get(User, user_id)
        increases = grant is None or (
            (body.allow_answer and not grant.allow_answer)
            or (body.allow_original and not grant.allow_original)
        )
        if target is None or (target.disabled and increases):
            raise APIError(422, "invalid_grantee", "Active user required")
    changed = False
    if grant is None and (body.allow_answer or body.allow_original):
        db.add(DocumentGrant(
            document_id=document_id, user_id=user_id,
            allow_answer=body.allow_answer, allow_original=body.allow_original,
        ))
        changed = True
    elif grant is not None and not (body.allow_answer or body.allow_original):
        await db.delete(grant)
        changed = True
    elif grant is not None and (
        grant.allow_answer != body.allow_answer or grant.allow_original != body.allow_original
    ):
        grant.allow_answer, grant.allow_original = body.allow_answer, body.allow_original
        changed = True
    if changed:
        document.acl_revision += 1
        document.revision += 1
        grant_action = (
            "both" if body.allow_answer and body.allow_original
            else "answer" if body.allow_answer else "original" if body.allow_original else "revoke"
        )
        db.add(AuditEvent(
            actor_id=actor.id, action=f"document.grant.{grant_action}",
            resource_id=f"{document_id}:{user_id}", result="success",
        ))
    result = {
        "document_id": str(document_id), "user_id": str(user_id),
        "allow_answer": body.allow_answer, "allow_original": body.allow_original,
        "revision": document.revision, "acl_revision": document.acl_revision,
    }
    await db.commit()
    return result


@router.post("/versions/{version_id}/original/download")
async def download_original(version_id: UUID, request: Request, db: DB) -> Response:
    """Original access is independent from answer permission and always audited."""
    import hashlib

    from starlette.concurrency import run_in_threadpool

    from partner_agent.documents.access import lock_authorized_document
    from partner_agent.documents.ingest import storage
    from partner_agent.infra.models import AuditEvent

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    document = await lock_authorized_document(db, actor, identifier, "original")
    version = await db.get(DocumentVersion, version_id)
    if version is None or version.document_id != identifier:
        raise APIError(404, "not_found", "Version not found")
    if not (document.owner_id == actor.id and actor.role in MANAGER_ROLES) and (
        document.active_version_id != version_id or version.status != "ready_for_review"
        or not version.canonical_sha256
        or version.approved_canonical_sha256 != version.canonical_sha256
    ):
        raise APIError(404, "not_found", "Version not found")
    key, digest, size, media_type = (
        version.original_key, version.source_sha256, version.size_bytes, version.media_type
    )
    await db.commit()

    def read() -> bytes:
        adapter = storage()
        try:
            return adapter.get(adapter.originals_bucket, key).data
        finally:
            adapter.client.close()

    data = await run_in_threadpool(read)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise APIError(409, "source_checksum_mismatch", "Original requires repair")
    document = await lock_authorized_document(db, actor, identifier, "original")
    version = await db.scalar(
        select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if version is None or (
        version.document_id != identifier or version.original_key != key
        or version.source_sha256 != digest or version.size_bytes != size
        or version.media_type != media_type
        or (not (document.owner_id == actor.id and actor.role in MANAGER_ROLES) and (
            document.active_version_id != version_id or version.status != "ready_for_review"
            or not version.canonical_sha256
            or version.approved_canonical_sha256 != version.canonical_sha256
        ))
    ):
        raise APIError(409, "version_state_conflict", "Version changed during download")
    db.add(AuditEvent(
        actor_id=actor.id, action="document.download_original", resource_id=str(version_id),
        result="success",
    ))
    await db.commit()
    extension = {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "image/png": "png", "image/jpeg": "jpg",
        "text/plain": "txt", "text/markdown": "md",
    }.get(media_type, "bin")
    return Response(
        content=data, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="document.{extension}"',
                 "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/documents/{document_id}/grants")
async def list_direct_grants(
    document_id: UUID, request: Request, response: Response, db: DB,
    after: UUID | None = None, limit: int = Query(default=50, ge=1, le=100),
    expected_acl_revision: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    from partner_agent.documents.access import document_access_allowed
    from partner_agent.infra.models import User
    from partner_agent.infra.models_documents import DocumentGrant

    _, actor = await authenticated(request, db)
    document = await lock_document(db, document_id, actor)
    if document.scope != "knowledge" or not document_access_allowed(actor, document, None, "acl"):
        raise APIError(403, "curator_required", "Curator permission required")
    if expected_acl_revision is not None and expected_acl_revision != document.acl_revision:
        raise APIError(409, "revision_conflict", "Permissions changed")
    query = (select(DocumentGrant, User).join(User, User.id == DocumentGrant.user_id)
             .where(DocumentGrant.document_id == document_id))
    if after is not None:
        query = query.where(DocumentGrant.user_id > after)
    grants = (await db.execute(query.order_by(DocumentGrant.user_id).limit(limit + 1))).all()
    response.headers["Cache-Control"] = "private, no-store"
    result = {
        "acl_revision": document.acl_revision,
        "items": [
            {"user_id": str(grant.user_id), "login": user.normalized_login,
             "disabled": user.disabled, "allow_answer": grant.allow_answer,
             "allow_original": grant.allow_original}
            for grant, user in grants[:limit]
        ],
        "next_after": str(grants[limit - 1][0].user_id) if len(grants) > limit else None,
    }
    await db.commit()
    return result


@router.get("/documents/{document_id}/audience")
async def list_document_audience(
    document_id: UUID, request: Request, response: Response, db: DB,
    after: UUID | None = None, limit: int = Query(default=50, ge=1, le=100),
    snapshot_token: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
) -> dict[str, Any]:
    """Review relevant direct/group subjects; this is not a content authorization check."""
    from partner_agent.auth.audience_lock import audience_read_lock
    from partner_agent.documents.access import document_access_allowed
    from partner_agent.documents.audience import audience_page
    from partner_agent.infra.models import Membership

    _, actor = await authenticated(request, db)
    await audience_read_lock(db)
    document = await lock_document(db, document_id, actor)
    if document.scope != "knowledge" or not document_access_allowed(actor, document, None, "acl"):
        raise APIError(403, "curator_required", "Curator permission required")
    if document.workspace_id is not None and await db.get(
        Membership, (document.workspace_id, actor.id)
    ) is None:
        raise APIError(403, "workspace_membership_required", "Workspace membership required")
    result = await audience_page(db, actor, document, after, limit, snapshot_token)
    response.headers["Cache-Control"] = "private, no-store"
    await db.commit()
    return result


@router.get("/documents/{document_id}/direct-subjects")
async def list_direct_subjects(
    document_id: UUID, request: Request, response: Response, db: DB,
    after: UUID | None = None, limit: int = Query(default=50, ge=1, le=100),
    expected_acl_revision: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """Discover explicit personal grants or unit restrictions, without group membership."""
    from sqlalchemy import exists, or_

    from partner_agent.documents.access import document_access_allowed
    from partner_agent.infra.models import User
    from partner_agent.infra.models_documents import (
        DisclosureAnswerDeny,
        DisclosureUnit,
        DocumentGrant,
    )

    _, actor = await authenticated(request, db)
    document = await lock_document(db, document_id, actor)
    if document.scope != "knowledge" or not document_access_allowed(actor, document, None, "acl"):
        raise APIError(403, "curator_required", "Curator permission required")
    if expected_acl_revision is not None and expected_acl_revision != document.acl_revision:
        raise APIError(409, "revision_conflict", "Permissions changed")
    denied = exists(
        select(DisclosureAnswerDeny.user_id)
        .join(DisclosureUnit, DisclosureUnit.id == DisclosureAnswerDeny.unit_id)
        .join(DocumentVersion, DocumentVersion.id == DisclosureUnit.version_id)
        .where(DocumentVersion.document_id == document_id, DisclosureAnswerDeny.user_id == User.id)
        .correlate(User)
    )
    query = (
        select(User, DocumentGrant, denied.label("has_denies"))
        .outerjoin(DocumentGrant, and_(DocumentGrant.user_id == User.id,
                                      DocumentGrant.document_id == document_id))
        .where(or_(DocumentGrant.user_id.is_not(None), denied))
    )
    if after is not None:
        query = query.where(User.id > after)
    rows = (await db.execute(query.order_by(User.id).limit(limit + 1))).all()
    response.headers["Cache-Control"] = "private, no-store"
    result = {
        "acl_revision": document.acl_revision,
        "items": [
            {"user_id": str(user.id), "login": user.normalized_login, "disabled": user.disabled,
             "allow_answer": bool(grant and grant.allow_answer),
             "allow_original": bool(grant and grant.allow_original), "has_denies": has_denies}
            for user, grant, has_denies in rows[:limit]
        ],
        "next_after": str(rows[limit - 1][0].id) if len(rows) > limit else None,
    }
    await db.commit()
    return result


@router.get("/documents/{document_id}/versions")
async def list_versions(
    document_id: UUID, request: Request, db: DB,
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    from sqlalchemy import func

    from partner_agent.documents.limits import MAX_EVIDENCE_CHARS
    from partner_agent.infra.models_documents import DisclosureUnit

    _, actor = await authenticated(request, db)
    document = await lock_document(db, document_id, actor)
    if document.scope != "knowledge" or actor.role not in MANAGER_ROLES:
        raise APIError(403, "curator_required", "Curator permission required")
    query = select(DocumentVersion).where(DocumentVersion.document_id == document_id)
    if before is not None:
        query = query.where(DocumentVersion.generation < before)
    versions = list(
        (await db.scalars(query.order_by(DocumentVersion.generation.desc()).limit(limit + 1))).all()
    )
    oversized_versions = set((await db.scalars(
        select(DisclosureUnit.version_id).distinct().where(
            DisclosureUnit.version_id.in_([version.id for version in versions[:limit]]),
            DisclosureUnit.policy_revision == document.policy_revision,
            func.char_length(DisclosureUnit.text) > MAX_EVIDENCE_CHARS,
        )
    )).all())
    items = [
        {
            "id": str(version.id), "document_id": str(document_id),
            "generation": version.generation, "status": version.status,
            "web_source": version.web_source(),
            "error_code": version.error_code, "parser_attempts": version.parser_attempts,
            "text_attempts": version.text_attempts,
            "text_retry_at": version.text_retry_at.isoformat() if version.text_retry_at else None,
            "index_attempts": version.index_attempts,
            "index_retry_at": (
                version.index_retry_at.isoformat() if version.index_retry_at else None
            ),
            "index_error": (
                version.index_manifest.get("code")
                if version.index_manifest
                and version.index_manifest.get("state") in ("error", "failed")
                else None
            ),
            "needs_reingest": bool(
                version.pipeline_version == "docling-adapter-v1"
                and version.status in ("processing", "ready_for_review")
                and version.disclosure_format not in ("docling-context-v2", "curator-edited-v1")
            ),
            "oversized_units": version.id in oversized_versions,
            "parser_retry_at": (
                version.parser_retry_at.isoformat() if version.parser_retry_at else None
            ),
            "review_available": bool(
                version.status == "pending" and version.canonical_key and version.canonical_sha256
            ),
            "created_at": version.created_at.isoformat(),
        }
        for version in versions[:limit]
    ]
    result = {
        "items": items,
        "next_before": versions[limit - 1].generation if len(versions) > limit else None,
    }
    await db.commit()
    return result


@router.post("/versions/{version_id}/reindex", status_code=202)
async def reindex(version_id: UUID, body: RevisionBody, request: Request, db: DB) -> dict[str, Any]:
    from partner_agent.documents.service import reindex_version

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    document = await reindex_version(db, actor, identifier, version_id, body.expected_revision)
    result = {"version_id": str(version_id), "status": "processing", "revision": document.revision}
    await db.commit()
    return result


@router.post("/versions/{version_id}/retry-index", status_code=202)
async def retry_index(
    version_id: UUID, body: RevisionBody, request: Request, db: DB
) -> dict[str, Any]:
    from partner_agent.documents.service import retry_index_version

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    document = await retry_index_version(db, actor, identifier, version_id, body.expected_revision)
    result = {"version_id": str(version_id), "status": "processing", "revision": document.revision}
    await db.commit()
    return result


class UnitDenyBody(RevisionBody):
    denied: bool


@router.put("/versions/{version_id}/units/{unit_id}/group-denies/{group_id}")
async def set_group_unit_deny(
    version_id: UUID, unit_id: UUID, group_id: UUID,
    body: UnitDenyBody, request: Request, db: DB,
) -> dict[str, Any]:
    from partner_agent.auth.audience_lock import audience_write_lock
    from partner_agent.infra.models import AuditEvent, Group, Membership
    from partner_agent.infra.models_documents import DisclosureGroupDeny, DisclosureUnit

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    await audience_write_lock(db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    document = await lock_document(db, identifier, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    if document.revision != body.expected_revision:
        raise APIError(409, "revision_conflict", "Document changed")
    group = await db.get(Group, group_id)
    unit = await db.get(DisclosureUnit, unit_id)
    if (document.workspace_id is None or group is None
            or group.workspace_id != document.workspace_id
            or unit is None or unit.version_id != version_id):
        raise APIError(404, "not_found", "Group or unit not found")
    if await db.get(Membership, (document.workspace_id, actor.id)) is None:
        raise APIError(403, "workspace_membership_required", "Workspace membership required")
    existing = await db.get(DisclosureGroupDeny, (unit_id, group_id))
    changed = False
    if body.denied and existing is None:
        db.add(
            DisclosureGroupDeny(
                unit_id=unit_id, group_id=group_id, version_id=version_id,
                document_id=identifier, workspace_id=document.workspace_id,
            )
        )
        changed = True
    elif not body.denied and existing is not None:
        await db.delete(existing)
        changed = True
    if changed:
        document.revision += 1
        document.acl_revision += 1
        db.add(AuditEvent(actor_id=actor.id,
                          action="document.group_unit_deny.add" if body.denied
                          else "document.group_unit_deny.remove",
                          resource_id=f"{unit_id}:{group_id}", result="success"))
    result = {"version_id": str(version_id), "unit_id": str(unit_id), "group_id": str(group_id),
              "denied": body.denied, "revision": document.revision,
              "acl_revision": document.acl_revision}
    await db.commit()
    return result


@router.get("/versions/{version_id}/group-denies/{group_id}/units")
async def list_group_unit_denies(
    version_id: UUID, group_id: UUID, request: Request, response: Response, db: DB,
    after: UUID | None = None, limit: int = Query(default=30, ge=1, le=100),
    expected_revision: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    from partner_agent.auth.audience_lock import audience_read_lock
    from partner_agent.infra.models import Group, Membership
    from partner_agent.infra.models_documents import DisclosureGroupDeny, DisclosureUnit

    _, actor = await authenticated(request, db)
    await audience_read_lock(db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    document = await lock_document(db, identifier, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    group = await db.get(Group, group_id)
    if (document.workspace_id is None or group is None
            or group.workspace_id != document.workspace_id):
        raise APIError(404, "not_found", "Group not found")
    if await db.get(Membership, (document.workspace_id, actor.id)) is None:
        raise APIError(403, "workspace_membership_required", "Workspace membership required")
    if expected_revision is not None and expected_revision != document.revision:
        raise APIError(409, "revision_conflict", "Document changed")
    query = (select(DisclosureUnit, DisclosureGroupDeny.group_id)
             .outerjoin(DisclosureGroupDeny, and_(DisclosureGroupDeny.unit_id == DisclosureUnit.id,
                                                   DisclosureGroupDeny.group_id == group_id))
             .where(DisclosureUnit.version_id == version_id).order_by(DisclosureUnit.id)
             .limit(limit + 1))
    if after is not None:
        query = query.where(DisclosureUnit.id > after)
    rows = (await db.execute(query)).all()
    response.headers["Cache-Control"] = "private, no-store"
    result = {"version_id": str(version_id), "group_id": str(group_id),
              "revision": document.revision,
              "items": [{"id": str(unit.id), "excerpt": unit.text[:1600],
                         "truncated": len(unit.text) > 1600, "denied": denied is not None}
                        for unit, denied in rows[:limit]],
              "next_after": str(rows[limit - 1][0].id) if len(rows) > limit else None}
    await db.commit()
    return result


@router.put("/versions/{version_id}/units/{unit_id}/denies/{user_id}")
async def set_unit_answer_deny(
    version_id: UUID, unit_id: UUID, user_id: UUID,
    body: UnitDenyBody, request: Request, db: DB,
) -> dict[str, Any]:
    """Restrict answer/excerpt for one user without changing original access."""
    from partner_agent.documents.access import document_access_allowed
    from partner_agent.infra.models import AuditEvent, User
    from partner_agent.infra.models_documents import DisclosureAnswerDeny, DisclosureUnit

    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    document = await lock_document(db, identifier, actor)
    if document.scope != "knowledge" or not document_access_allowed(actor, document, None, "acl"):
        raise APIError(403, "curator_required", "Curator permission required")
    if document.revision != body.expected_revision:
        raise APIError(409, "revision_conflict", "Document changed")
    unit = await db.scalar(
        select(DisclosureUnit).where(
            DisclosureUnit.id == unit_id, DisclosureUnit.version_id == version_id
        )
    )
    if unit is None:
        raise APIError(404, "not_found", "Disclosure unit not found")
    if body.denied:
        target = await db.get(User, user_id)
        if target is None or target.disabled:
            raise APIError(422, "invalid_grantee", "Active user required")
    existing = await db.get(DisclosureAnswerDeny, (unit_id, user_id))
    changed = False
    if body.denied and existing is None:
        db.add(DisclosureAnswerDeny(unit_id=unit_id, user_id=user_id))
        changed = True
    elif not body.denied and existing is not None:
        await db.delete(existing)
        changed = True
    if changed:
        document.acl_revision += 1
        document.revision += 1
        db.add(AuditEvent(
            actor_id=actor.id,
            action="document.unit_deny.add" if body.denied else "document.unit_deny.remove",
            resource_id=f"{unit_id}:{user_id}", result="success",
        ))
    result = {
        "version_id": str(version_id), "unit_id": str(unit_id), "user_id": str(user_id),
        "denied": body.denied, "revision": document.revision, "acl_revision": document.acl_revision,
    }
    await db.commit()
    return result


@router.get("/versions/{version_id}/units")
async def list_disclosure_units(
    version_id: UUID, request: Request, response: Response, db: DB,
    after: UUID | None = None, limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    from partner_agent.documents.access import document_access_allowed
    from partner_agent.infra.models_documents import DisclosureUnit

    _, actor = await authenticated(request, db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    document = await lock_document(db, identifier, actor)
    if document.scope != "knowledge" or not document_access_allowed(actor, document, None, "acl"):
        raise APIError(403, "curator_required", "Curator permission required")
    query = select(DisclosureUnit).where(DisclosureUnit.version_id == version_id)
    if after is not None:
        query = query.where(DisclosureUnit.id > after)
    units = list((await db.scalars(query.order_by(DisclosureUnit.id).limit(limit + 1))).all())
    response.headers["Cache-Control"] = "private, no-store"
    result = {
        "version_id": str(version_id), "acl_revision": document.acl_revision,
        "items": [
            {"id": str(unit.id), "block_id": str(unit.block_id), "excerpt": unit.text[:1600],
             "truncated": len(unit.text) > 1600, "locator": unit.locator}
            for unit in units[:limit]
        ],
        "next_after": str(units[limit - 1].id) if len(units) > limit else None,
    }
    await db.commit()
    return result


@router.get("/versions/{version_id}/units/{unit_id}/denies")
async def list_unit_answer_denies(
    version_id: UUID, unit_id: UUID, request: Request, response: Response, db: DB,
    after: UUID | None = None, limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    from partner_agent.documents.access import document_access_allowed
    from partner_agent.infra.models_documents import DisclosureAnswerDeny, DisclosureUnit

    _, actor = await authenticated(request, db)
    identifier = await db.scalar(
        select(DocumentVersion.document_id).where(DocumentVersion.id == version_id)
    )
    if identifier is None:
        raise APIError(404, "not_found", "Version not found")
    document = await lock_document(db, identifier, actor)
    if document.scope != "knowledge" or not document_access_allowed(actor, document, None, "acl"):
        raise APIError(403, "curator_required", "Curator permission required")
    unit = await db.scalar(
        select(DisclosureUnit.id).where(
            DisclosureUnit.id == unit_id, DisclosureUnit.version_id == version_id
        )
    )
    if unit is None:
        raise APIError(404, "not_found", "Disclosure unit not found")
    query = select(DisclosureAnswerDeny).where(DisclosureAnswerDeny.unit_id == unit_id)
    if after is not None:
        query = query.where(DisclosureAnswerDeny.user_id > after)
    denies = list(
        (await db.scalars(query.order_by(DisclosureAnswerDeny.user_id).limit(limit + 1))).all()
    )
    response.headers["Cache-Control"] = "private, no-store"
    result = {
        "unit_id": str(unit_id), "acl_revision": document.acl_revision,
        "user_ids": [str(deny.user_id) for deny in denies[:limit]],
        "next_after": str(denies[limit - 1].user_id) if len(denies) > limit else None,
    }
    await db.commit()
    return result


@router.get("/documents/{document_id}/publication-acl-review")
async def publication_acl_review(
    document_id: UUID, target_version_id: UUID, request: Request, response: Response, db: DB,
) -> dict[str, Any]:
    """Show bounded prior/new unit denies for an explicit publication decision."""
    from partner_agent.auth.audience_lock import audience_read_lock
    from partner_agent.documents.access import document_access_allowed
    from partner_agent.infra.models import Group, Membership, User
    from partner_agent.infra.models_documents import (
        DisclosureAnswerDeny,
        DisclosureGroupDeny,
        DisclosureUnit,
    )

    _, actor = await authenticated(request, db)
    await audience_read_lock(db)
    document = await lock_document(db, document_id, actor)
    if document.scope != "knowledge" or not document_access_allowed(actor, document, None, "acl"):
        raise APIError(403, "curator_required", "Curator permission required")
    if document.workspace_id is not None and await db.get(
        Membership, (document.workspace_id, actor.id)
    ) is None:
        raise APIError(403, "workspace_membership_required", "Workspace membership required")
    target = await db.get(DocumentVersion, target_version_id)
    if target is None or target.document_id != document_id:
        raise APIError(404, "not_found", "Version not found")
    previous_id = document.last_published_version_id or document.active_version_id
    if previous_id is None or previous_id == target_version_id:
        raise APIError(409, "version_state_conflict", "No prior version to review")

    async def entries(version_id: UUID) -> tuple[list[dict[str, Any]], bool]:
        rows = (
            await db.execute(
                select(DisclosureAnswerDeny, DisclosureUnit, User)
                .join(DisclosureUnit, DisclosureUnit.id == DisclosureAnswerDeny.unit_id)
                .join(User, User.id == DisclosureAnswerDeny.user_id)
                .where(DisclosureUnit.version_id == version_id)
                .order_by(DisclosureUnit.id, User.id)
                .limit(101)
            )
        ).all()
        result = [
            {"unit_id": str(unit.id), "user_id": str(user.id), "login": user.normalized_login,
             "subject_type": "user",
             "excerpt": unit.text[:1600], "truncated": len(unit.text) > 1600}
            for _, unit, user in rows[:100]
        ]
        group_rows = (await db.execute(
            select(DisclosureUnit, Group)
            .join(DisclosureGroupDeny, DisclosureGroupDeny.unit_id == DisclosureUnit.id)
            .join(Group, Group.id == DisclosureGroupDeny.group_id)
            .where(DisclosureUnit.version_id == version_id)
            .order_by(DisclosureUnit.id, Group.id).limit(101)
        )).all()
        result.extend({"unit_id": str(unit.id), "user_id": str(group.id), "login": group.name,
                       "subject_type": "group", "excerpt": unit.text[:1600],
                       "truncated": len(unit.text) > 1600}
                      for unit, group in group_rows[:100])
        incomplete = len(rows) + len(group_rows) > 100 or any(item["truncated"] for item in result)
        return result[:100], incomplete

    previous, previous_incomplete = await entries(previous_id)
    target_entries, target_incomplete = await entries(target_version_id)
    response.headers["Cache-Control"] = "private, no-store"
    result = {
        "document_id": str(document_id), "previous_version_id": str(previous_id),
        "target_version_id": str(target_version_id), "revision": document.revision,
        "acl_revision": document.acl_revision, "previous": previous, "target": target_entries,
        "incomplete": previous_incomplete or target_incomplete,
    }
    await db.commit()
    return result
