"""Offline text ingestion poller; durable pending versions survive process restarts."""

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, or_, select

from partner_agent.auth.policy import MANAGER_ROLES
from partner_agent.documents.canonical import CanonicalError
from partner_agent.documents.ingest import process_stored_text
from partner_agent.infra.db import get_engine, session_factory
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import User
from partner_agent.infra.models_documents import Document, DocumentVersion

logger = logging.getLogger("partner_agent.ingest")


async def _run_serialized(
    identifier: UUID, namespace: bytes, operation: Callable[[UUID], Awaitable[bool]]
) -> bool:
    """Hold a session lock without returning it, or an idle transaction, to the pool."""
    digest = hashlib.sha256(namespace + identifier.bytes).digest()
    lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    async with get_engine().connect() as connection:
        try:
            locked = bool(await connection.scalar(select(func.pg_try_advisory_lock(lock_key))))
        except BaseException:
            # Acquisition may have reached PostgreSQL even if its response was lost.
            # Never return a potentially locked session to the pool.
            await connection.invalidate()
            raise
        if not locked:
            await connection.commit()
            return False
        try:
            await connection.commit()
            return await operation(identifier)
        finally:
            try:
                unlocked = await connection.scalar(select(func.pg_advisory_unlock(lock_key)))
                if not unlocked:
                    await connection.invalidate()
                else:
                    await connection.commit()
            except BaseException:
                await connection.invalidate()
                raise


async def _ingest_one(identifier: UUID) -> bool:
    async with session_factory()() as db:
        version = await db.scalar(
            select(DocumentVersion).where(
                DocumentVersion.id == identifier, DocumentVersion.status == "pending",
                DocumentVersion.canonical_key.is_(None), DocumentVersion.text_attempts < 5,
                or_(DocumentVersion.text_retry_at.is_(None),
                    DocumentVersion.text_retry_at <= func.now()),
                DocumentVersion.pipeline_version == "canonical-v1",
            )
        )
        if version is None:
            return False
        attempt_count, retry_at = version.text_attempts, version.text_retry_at
        document = await db.get(Document, version.document_id)
        if document is None or document.deleted_at is not None or document.scope != "knowledge":
            return False
        actor = await db.get(User, document.owner_id)
        if actor is None or actor.disabled or actor.role not in MANAGER_ROLES:
            return False
        try:
            await process_stored_text(db, actor, document.id, identifier)
            await db.commit()
            return True
        except Exception as error:
            await db.rollback()
            if isinstance(error, APIError) and error.code in {
                "version_state_conflict", "access_revoked", "not_found", "curator_required"
            }:
                return False
            if isinstance(error, CanonicalError):
                code = "invalid_text_source"
            elif isinstance(error, APIError) and error.code == "source_checksum_mismatch":
                code = error.code
            else:
                code = "text_dependency_unavailable"
            recorded = False
            try:
                current = await db.scalar(
                    select(DocumentVersion).where(DocumentVersion.id == identifier)
                    .with_for_update().execution_options(populate_existing=True)
                )
                if (
                    current is not None and current.status == "pending"
                    and current.canonical_key is None
                    and current.pipeline_version == "canonical-v1"
                    and current.text_attempts == attempt_count and current.text_retry_at == retry_at
                ):
                    current.text_attempts = min(current.text_attempts + 1, 5)
                    current.error_code = code
                    if current.text_attempts >= 5:
                        current.status = "failed"
                        current.text_retry_at = None
                    else:
                        now = await db.scalar(select(func.now()))
                        assert now is not None
                        current.text_retry_at = now + timedelta(
                            seconds=30 * 2 ** (current.text_attempts - 1)
                        )
                    await db.commit()
                    recorded = True
            except Exception:
                await db.rollback()
                logger.warning("text_failure_state_unavailable version_id=%s", identifier)
            if recorded:
                logger.warning("text_retry_required version_id=%s code=%s", identifier, code)
            else:
                logger.info("text_attempt_not_recorded version_id=%s", identifier)
            return False


async def ingest_once() -> int:
    async with session_factory()() as db:
        candidates = list(
            (
                await db.scalars(
                    select(DocumentVersion.id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .join(User, User.id == Document.owner_id)
                    .where(
                        DocumentVersion.status == "pending",
                        DocumentVersion.canonical_key.is_(None),
                        DocumentVersion.text_attempts < 5,
                        or_(DocumentVersion.text_retry_at.is_(None),
                            DocumentVersion.text_retry_at <= func.now()),
                        DocumentVersion.pipeline_version == "canonical-v1",
                        DocumentVersion.media_type.in_(["text/plain", "text/markdown"]),
                        Document.deleted_at.is_(None), Document.scope == "knowledge",
                        User.disabled.is_(False), User.role.in_(MANAGER_ROLES),
                    )
                    .order_by(DocumentVersion.created_at)
                    .limit(100)
                )
            ).all()
        )
    processed = 0
    for identifier in candidates:
        processed += int(await _run_serialized(identifier, b"partner-text:", _ingest_one))
    return processed


async def _index_one(identifier: UUID) -> bool:
    from partner_agent.documents.indexing import stage_version
    from partner_agent.retrieval.index import IndexConfigurationError

    async with session_factory()() as db:
        version = await db.scalar(
            select(DocumentVersion).where(
                DocumentVersion.id == identifier, DocumentVersion.status == "processing",
                DocumentVersion.index_attempts < 5,
                or_(DocumentVersion.index_retry_at.is_(None),
                    DocumentVersion.index_retry_at <= func.now()),
            )
        )
        if version is None:
            return False
        attempt_count, retry_at = version.index_attempts, version.index_retry_at
        document = await db.get(Document, version.document_id)
        if document is None or document.deleted_at is not None:
            return False
        actor = await db.get(User, document.owner_id)
        if actor is None or actor.disabled or actor.role not in MANAGER_ROLES:
            return False
        try:
            await stage_version(db, actor, document.id, identifier)
            await db.commit()
            return True
        except IndexConfigurationError:
            await db.rollback()
            logger.warning("index_configuration_unavailable")
            raise
        except Exception as error:
            await db.rollback()
            safe_codes = {
                "canonical_digest_mismatch", "canonical_artifact_invalid",
                "canonical_provenance_mismatch", "disclosure_integrity_mismatch",
                "index_state_changed", "disclosure_context_too_large",
            }
            code = (
                error.code if isinstance(error, APIError) and error.code in safe_codes
                else "index_dependency_unavailable"
            )
            recorded = False
            try:
                current = await db.scalar(
                    select(DocumentVersion)
                    .where(DocumentVersion.id == identifier)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    current is not None and current.status == "processing"
                    and current.index_attempts == attempt_count
                    and current.index_retry_at == retry_at
                ):
                    current.index_attempts = min(current.index_attempts + 1, 5)
                    if current.index_attempts >= 5:
                        current.index_retry_at = None
                        state = "failed"
                    else:
                        now = await db.scalar(select(func.now()))
                        assert now is not None
                        current.index_retry_at = now + timedelta(
                            seconds=30 * 2 ** (current.index_attempts - 1)
                        )
                        state = "error"
                    current.index_manifest = {"state": state, "code": code}
                    await db.commit()
                    recorded = True
            except Exception:
                await db.rollback()
                logger.warning("index_failure_state_unavailable version_id=%s", identifier)
            if recorded:
                logger.warning("index_retry_required version_id=%s code=%s", identifier, code)
            else:
                logger.info("index_attempt_not_recorded version_id=%s", identifier)
            return False


async def index_once() -> int:
    async with session_factory()() as db:
        identifiers = list(
            (
                await db.scalars(
                    select(DocumentVersion.id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .join(User, User.id == Document.owner_id)
                    .where(
                        Document.deleted_at.is_(None), Document.scope == "knowledge",
                        User.disabled.is_(False), User.role.in_(MANAGER_ROLES),
                        DocumentVersion.status == "processing",
                        DocumentVersion.index_attempts < 5,
                        or_(DocumentVersion.index_retry_at.is_(None),
                            DocumentVersion.index_retry_at <= func.now()),
                        DocumentVersion.canonical_key.is_not(None),
                        DocumentVersion.pipeline_version.in_(
                            ["canonical-v1", "docling-adapter-v1"]
                        ),
                    )
                    .order_by(DocumentVersion.created_at)
                    .limit(100)
                )
            ).all()
        )
    if identifiers:
        from partner_agent.infra.settings import get_settings
        from partner_agent.retrieval.index import FixtureIndex

        await FixtureIndex(get_settings().qdrant_url).ensure_compatible()
    processed = 0
    for identifier in identifiers:
        processed += int(await _run_serialized(identifier, b"partner-index:", _index_one))
    return processed


async def main() -> None:
    while True:
        try:
            from partner_agent.documents.chat_files import pending_files, process_file

            for identifier in await pending_files(binary=False):
                await _run_serialized(identifier, b"chat-file:", process_file)
            await ingest_once()
            await index_once()
        except Exception:
            logger.warning("ingest_poll_unavailable")
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
