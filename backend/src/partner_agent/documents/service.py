"""Document mutations share one owner-checked transaction; callers commit."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from partner_agent.auth.audience_lock import audience_read_lock
from partner_agent.auth.policy import MANAGER_ROLES
from partner_agent.documents.limits import MAX_EVIDENCE_CHARS
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import AuditEvent, User
from partner_agent.infra.models_documents import (
    DisclosureAnswerDeny,
    DisclosureGroupDeny,
    DisclosureUnit,
    Document,
    DocumentVersion,
)


async def lock_document(db: AsyncSession, identifier: UUID, actor: User) -> Document:
    # Refresh authorization after a processor releases its transaction for external I/O.
    current = await db.scalar(
        select(User).where(User.id == actor.id).execution_options(populate_existing=True)
    )
    if current is None or current.disabled:
        raise APIError(403, "access_revoked", "Access revoked")
    document = await db.scalar(select(Document).where(Document.id == identifier).with_for_update())
    # The document lock may have waited while an administrator changed the user.
    current = await db.scalar(
        select(User).where(User.id == actor.id).execution_options(populate_existing=True)
    )
    if current is None or current.disabled:
        raise APIError(403, "access_revoked", "Access revoked")
    # Administrator is an operational role, not an implicit content permission.
    if document is None or document.deleted_at is not None or document.owner_id != actor.id:
        raise APIError(404, "not_found", "Document not found")
    if actor.disabled:
        raise APIError(403, "access_revoked", "Access revoked")
    return document


async def create_document(db: AsyncSession, actor: User, title: str, scope: str) -> Document:
    if actor.disabled:
        raise APIError(403, "access_revoked", "Access revoked")
    if scope not in ("private", "knowledge") or not title.strip() or len(title) > 200:
        raise APIError(422, "validation_error", "Invalid document")
    if scope == "knowledge" and actor.role not in MANAGER_ROLES:
        raise APIError(403, "curator_required", "Curator permission required")
    document = Document(owner_id=actor.id, title=title.strip(), scope=scope)
    db.add(document)
    await db.flush()
    return document


async def publish_version(
    db: AsyncSession, actor: User, document_id: UUID, version_id: UUID, expected_revision: int,
    confirm_deny_review: bool = False,
) -> Document:
    await audience_read_lock(db)
    document = await lock_document(db, document_id, actor)
    if document.scope != "knowledge" or actor.role not in MANAGER_ROLES:
        raise APIError(403, "curator_required", "Curator permission required")
    if document.revision != expected_revision:
        raise APIError(409, "revision_conflict", "Document changed")
    version = await db.scalar(
        select(DocumentVersion)
        .where(DocumentVersion.id == version_id, DocumentVersion.document_id == document.id)
        .with_for_update()
    )
    if version is None:
        raise APIError(404, "not_found", "Version not found")
    latest = await db.scalar(
        select(func.max(DocumentVersion.generation)).where(
            DocumentVersion.document_id == document.id
        )
    )
    manifest = version.index_manifest or {}
    count = await db.scalar(
        select(func.count())
        .select_from(DisclosureUnit)
        .where(
            DisclosureUnit.version_id == version.id,
            DisclosureUnit.policy_revision == document.policy_revision,
        )
    )
    oversized = await db.scalar(
        select(func.count()).select_from(DisclosureUnit).where(
            DisclosureUnit.version_id == version.id,
            DisclosureUnit.policy_revision == document.policy_revision,
            func.char_length(DisclosureUnit.text) > MAX_EVIDENCE_CHARS,
        )
    )
    if oversized:
        raise APIError(
            409, "disclosure_unit_too_large",
            "A selected fragment exceeds the current evidence limit",
        )
    if (
        version.status != "ready_for_review"
        or not version.canonical_key
        or not version.canonical_sha256
        or version.approved_canonical_sha256 != version.canonical_sha256
        or (version.pipeline_version == "docling-adapter-v1"
            and version.disclosure_format not in ("docling-context-v2", "curator-edited-v1"))
        or version.generation != latest
        or not count
        or manifest.get("state") != "verified"
        or manifest.get("version_id") != str(version.id)
        or manifest.get("policy_revision") != document.policy_revision
        or manifest.get("unit_count") != count
    ):
        raise APIError(409, "publication_not_ready", "Version is not ready for publication")
    previous_id = document.last_published_version_id or document.active_version_id
    deny_review_required = False
    if previous_id is not None and previous_id != version_id:
        denied_count = await db.scalar(
            select(func.count())
            .select_from(DisclosureAnswerDeny)
            .join(DisclosureUnit, DisclosureUnit.id == DisclosureAnswerDeny.unit_id)
            .where(DisclosureUnit.version_id == previous_id)
        )
        group_denied_count = await db.scalar(
            select(func.count()).select_from(DisclosureGroupDeny)
            .join(DisclosureUnit, DisclosureUnit.id == DisclosureGroupDeny.unit_id)
            .where(DisclosureUnit.version_id == previous_id)
        )
        if group_denied_count:
            from partner_agent.infra.models import Membership

            if document.workspace_id is None or await db.get(
                Membership, (document.workspace_id, actor.id)
            ) is None:
                raise APIError(
                    403, "workspace_membership_required", "Workspace membership required"
                )
        deny_review_required = bool(denied_count or group_denied_count)
        if deny_review_required and not confirm_deny_review:
            raise APIError(
                409, "deny_review_required",
                "Review prior unit restrictions before switching versions",
            )
    document.active_version_id = version.id
    document.last_published_version_id = version.id
    document.revision += 1
    db.add(
        AuditEvent(
            actor_id=actor.id,
            action=(
                "document.publish_after_deny_review" if deny_review_required else "document.publish"
            ),
            resource_id=str(document.id),
            result="success",
        )
    )
    await db.flush()
    return document


async def unpublish(
    db: AsyncSession, actor: User, document_id: UUID, expected_revision: int
) -> Document:
    document = await lock_document(db, document_id, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    if document.revision != expected_revision:
        raise APIError(409, "revision_conflict", "Document changed")
    document.active_version_id = None
    document.revision += 1
    db.add(
        AuditEvent(
            actor_id=actor.id,
            action="document.unpublish",
            resource_id=str(document.id),
            result="success",
        )
    )
    await db.flush()
    return document


async def reindex_version(
    db: AsyncSession, actor: User, document_id: UUID, version_id: UUID, expected_revision: int
) -> Document:
    """Invalidate readiness atomically; worker will rebuild from approved units."""
    document = await lock_document(db, document_id, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    if document.revision != expected_revision:
        raise APIError(409, "revision_conflict", "Document changed")
    version = await db.scalar(
        select(DocumentVersion)
        .where(DocumentVersion.id == version_id, DocumentVersion.document_id == document_id)
        .with_for_update()
    )
    if version is None:
        raise APIError(404, "not_found", "Version not found")
    count = await db.scalar(
        select(func.count()).select_from(DisclosureUnit).where(
            DisclosureUnit.version_id == version.id,
            DisclosureUnit.policy_revision == document.policy_revision,
        )
    )
    if (
        version.status != "ready_for_review" or not version.canonical_key
        or not version.canonical_sha256
        or version.approved_canonical_sha256 != version.canonical_sha256 or not count
        or (version.pipeline_version == "docling-adapter-v1"
            and version.disclosure_format not in ("docling-context-v2", "curator-edited-v1"))
    ):
        raise APIError(409, "version_state_conflict", "Version cannot be reindexed")
    version.status = "processing"
    version.index_manifest = None
    version.index_attempts = 0
    version.index_retry_at = None
    document.revision += 1
    db.add(AuditEvent(
        actor_id=actor.id, action="document.reindex", resource_id=str(version.id), result="success"
    ))
    await db.flush()
    return document


async def retry_index_version(
    db: AsyncSession, actor: User, document_id: UUID, version_id: UUID, expected_revision: int
) -> Document:
    document = await lock_document(db, document_id, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    if document.revision != expected_revision:
        raise APIError(409, "revision_conflict", "Document changed")
    version = await db.scalar(
        select(DocumentVersion)
        .where(DocumentVersion.id == version_id, DocumentVersion.document_id == document_id)
        .with_for_update()
    )
    if version is None:
        raise APIError(404, "not_found", "Version not found")
    manifest = version.index_manifest or {}
    if (
        version.status != "processing" or version.index_attempts == 0
        or manifest.get("state") not in ("error", "failed")
    ):
        raise APIError(409, "version_state_conflict", "Index cannot be retried")
    version.index_attempts = 0
    version.index_retry_at = None
    version.index_manifest = None
    document.revision += 1
    db.add(AuditEvent(
        actor_id=actor.id, action="document.retry_index", resource_id=str(version.id),
        result="success",
    ))
    await db.flush()
    return document
