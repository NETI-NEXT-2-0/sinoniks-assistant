"""Version admission and deterministic text ingestion; external I/O stays outside row locks."""

import hashlib
from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from partner_agent.auth.policy import MANAGER_ROLES
from partner_agent.documents.canonical import MAX_FILE_BYTES, extract_utf8
from partner_agent.documents.service import lock_document
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import User
from partner_agent.infra.models_documents import DocumentVersion
from partner_agent.infra.settings import get_settings
from partner_agent.infra.storage import Storage, create_s3_client, object_key

DOCLING_MEDIA_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}
TEXT_MEDIA_TYPES = frozenset(("text/plain", "text/markdown"))


def storage() -> Storage:
    settings = get_settings()
    return Storage(
        create_s3_client(
            settings.s3_endpoint_url,
            settings.s3_region,
            settings.s3_access_key_id,
            settings.s3_secret_access_key.get_secret_value(),
        ),
        settings.s3_originals_bucket,
        settings.s3_derived_bucket,
    )


async def admit_version(
    db: AsyncSession,
    actor: User,
    document_id: UUID,
    expected_revision: int,
    data: bytes,
    media_type: str,
    idempotency_key: UUID | None = None,
    *,
    source_url: str | None = None,
    source_captured_on: date | None = None,
) -> tuple[DocumentVersion, bool]:
    if not 0 < len(data) <= MAX_FILE_BYTES:
        raise APIError(413, "file_size_limit", "Invalid file size")
    if media_type not in TEXT_MEDIA_TYPES and media_type not in DOCLING_MEDIA_TYPES:
        raise APIError(415, "unsupported_media_type", "Unsupported document type")
    document = await lock_document(db, document_id, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    source_hash = hashlib.sha256(data).hexdigest()
    if idempotency_key is not None:
        existing = await db.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.upload_idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if (existing.source_sha256 != source_hash or existing.size_bytes != len(data)
                    or existing.media_type != media_type or existing.source_url != source_url
                    or existing.source_captured_on != source_captured_on):
                raise APIError(409, "idempotency_conflict", "Upload key belongs to another source")
            return existing, False
    if expected_revision != document.revision:
        raise APIError(409, "revision_conflict", "Document changed")
    generation = await db.scalar(
        select(func.max(DocumentVersion.generation)).where(
            DocumentVersion.document_id == document_id
        )
    )
    identifier = uuid4()
    # Binary validation occurs inside the isolated parser after durable admission.
    if media_type in TEXT_MEDIA_TYPES:
        extract_utf8(data, document_id, identifier, media_type)
    version = DocumentVersion(
        id=identifier,
        document_id=document_id,
        generation=(generation or 0) + 1,
        upload_idempotency_key=idempotency_key,
        source_sha256=source_hash,
        source_url=source_url,
        source_captured_on=source_captured_on,
        media_type=media_type,
        size_bytes=len(data),
        original_key=object_key(document_id, identifier, "original"),
        pipeline_version=(
            "canonical-v1" if media_type in TEXT_MEDIA_TYPES else "docling-adapter-v1"
        ),
        status="pending",
    )
    db.add(version)
    document.revision += 1
    await db.flush()
    return version, True


async def store_original(key: str, data: bytes, media_type: str) -> None:
    def put() -> None:
        adapter = storage()
        try:
            adapter.put(adapter.originals_bucket, key, data, media_type)
        finally:
            adapter.client.close()

    await run_in_threadpool(put)


async def process_stored_text(
    db: AsyncSession, actor: User, document_id: UUID, version_id: UUID
) -> DocumentVersion:
    # Canonical source blocks are never disclosure units until explicit approval.
    document = await lock_document(db, document_id, actor)
    if actor.role not in MANAGER_ROLES or document.scope != "knowledge":
        raise APIError(403, "curator_required", "Curator permission required")
    version = await db.get(DocumentVersion, version_id)
    if version is None or version.document_id != document_id:
        raise APIError(404, "not_found", "Version not found")
    if (
        version.status != "pending" or version.canonical_key is not None
        or version.pipeline_version != "canonical-v1" or version.media_type not in TEXT_MEDIA_TYPES
    ):
        raise APIError(409, "version_state_conflict", "Version already processed")
    key, media_type, expected_hash = version.original_key, version.media_type, version.source_sha256
    size = version.size_bytes
    await db.commit()

    def convert() -> tuple[str, str]:
        adapter = storage()
        try:
            source = adapter.get(adapter.originals_bucket, key).data
            if len(source) != size or hashlib.sha256(source).hexdigest() != expected_hash:
                raise APIError(409, "source_checksum_mismatch", "Source checksum mismatch")
            canonical = extract_utf8(source, document_id, version_id, media_type)
            data = canonical.model_dump_json().encode()
            digest = hashlib.sha256(data).hexdigest()
            canonical_key = object_key(document_id, version_id, f"canonical-{digest}.json")
            adapter.put(adapter.derived_bucket, canonical_key, data, "application/json")
            return canonical_key, digest
        finally:
            adapter.client.close()

    canonical_key, digest = await run_in_threadpool(convert)
    document = await lock_document(db, document_id, actor)
    version = await db.scalar(
        select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if version is None or (
        version.document_id != document_id or version.status != "pending"
        or version.canonical_key is not None or version.pipeline_version != "canonical-v1"
        or version.original_key != key or version.source_sha256 != expected_hash
        or version.media_type != media_type or version.size_bytes != size
        or actor.role not in MANAGER_ROLES or document.scope != "knowledge"
    ):
        raise APIError(409, "version_state_conflict", "Version changed during processing")
    version.canonical_key = canonical_key
    version.canonical_sha256 = digest
    version.error_code = None
    version.text_retry_at = None
    await db.flush()
    return version
