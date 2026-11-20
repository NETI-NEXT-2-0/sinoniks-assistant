"""Owner/chat-scoped extraction and evidence; never publishes into shared knowledge."""

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from partner_agent.documents.canonical import CanonicalDocument, extract_utf8
from partner_agent.documents.docling_adapter import ParsedDocument, assert_reviewable
from partner_agent.documents.ingest import TEXT_MEDIA_TYPES, storage
from partner_agent.infra.db import session_factory
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import User, utcnow
from partner_agent.infra.models_chat_files import ChatFile
from partner_agent.infra.models_stage2 import Chat, ContextManifest, Message, Run
from partner_agent.retrieval.lexical import query_terms
from partner_agent.runs.service import lock_chat

BinaryParser = Callable[[bytes, ChatFile], ParsedDocument]


def file_dto(item: ChatFile) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "name": item.name,
        "status": item.status,
        "size_bytes": item.size_bytes,
        "error_code": item.error_code,
        "warnings": item.warnings,
        "created_at": item.created_at.isoformat(),
    }


def chunk_digest(chunks: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(chunks, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def extract_chunks(canonical: CanonicalDocument | ParsedDocument) -> list[dict[str, Any]]:
    if isinstance(canonical, ParsedDocument):
        assert_reviewable(canonical)
    chunks = []
    total = 0
    for block in canonical.blocks:
        locator = (
            block.positions[0].model_dump(mode="json")
            if isinstance(canonical, ParsedDocument) and hasattr(block, "positions")
            else block.model_dump(mode="json").get("locator", {})
        )
        for offset in range(0, len(block.text), 1400):
            text = block.text[offset : offset + 1400]
            total += len(text)
            chunks.append(
                {"text": text, "locator": locator, "block_id": str(block.id), "offset": offset}
            )
            if len(chunks) > 1000 or total > 1_000_000:
                raise APIError(422, "attachment_text_limit", "Extracted document is too large")
    if not chunks:
        raise APIError(422, "attachment_empty_text", "No readable document text")
    return chunks


async def owned_file(
    db: AsyncSession, actor: User, chat_id: UUID, file_id: UUID, *, lock: bool = False
) -> ChatFile:
    await lock_chat(db, chat_id, actor)
    active = await db.scalar(select(User.disabled).where(User.id == actor.id))
    if active is not False:
        raise APIError(403, "access_revoked", "Access revoked")
    query = select(ChatFile).where(
        ChatFile.id == file_id,
        ChatFile.chat_id == chat_id,
        ChatFile.owner_id == actor.id,
        ChatFile.deleted_at.is_(None),
    )
    if lock:
        query = query.with_for_update()
    item = await db.scalar(query.execution_options(populate_existing=True))
    if item is None:
        raise APIError(404, "not_found", "Chat file not found")
    return item


async def validate_selection(
    db: AsyncSession, actor: User, chat_id: UUID, identifiers: list[UUID]
) -> None:
    if len(identifiers) > 5 or len(set(identifiers)) != len(identifiers):
        raise APIError(422, "invalid_attachments", "Select up to five distinct files")
    for identifier in sorted(identifiers):
        item = await owned_file(db, actor, chat_id, identifier, lock=True)
        if item.status != "ready":
            raise APIError(409, "attachment_not_ready", "Wait until file processing finishes")


async def process_file(file_id: UUID, binary_parser: BinaryParser | None = None) -> bool:
    async with session_factory()() as db:
        item = await db.get(ChatFile, file_id)
        if item is None or item.status != "pending" or item.deleted_at is not None:
            return False
        actor = await db.get(User, item.owner_id)
        if actor is None or actor.disabled:
            return False
        item = await owned_file(db, actor, item.chat_id, item.id, lock=True)
        if item.attempts >= 5 or item.retry_at and item.retry_at > utcnow():
            return False
        if item.media_type not in TEXT_MEDIA_TYPES and binary_parser is None:
            return False
        key, digest, size, attempts = (
            item.original_key,
            item.source_sha256,
            item.size_bytes,
            item.attempts,
        )
        chat_id = item.chat_id
        retry_at = item.retry_at
        await db.commit()
        try:

            def convert() -> tuple[list[dict[str, Any]], list[str]]:
                adapter = storage()
                try:
                    source = adapter.get(adapter.originals_bucket, key).data
                finally:
                    adapter.client.close()
                if len(source) != size or hashlib.sha256(source).hexdigest() != digest:
                    raise APIError(409, "source_checksum_mismatch", "Source changed")
                canonical = (
                    extract_utf8(source, chat_id, file_id, item.media_type)
                    if item.media_type in TEXT_MEDIA_TYPES
                    else binary_parser(source, item)
                    if binary_parser
                    else None
                )
                if canonical is None:
                    raise RuntimeError("Parser unavailable")
                return extract_chunks(canonical), list(canonical.warnings)

            chunks, warnings = await run_in_threadpool(convert)
            current = await owned_file(db, actor, chat_id, file_id, lock=True)
            if (
                current.status != "pending"
                or current.source_sha256 != digest
                or current.original_key != key
                or current.attempts != attempts
                or current.retry_at != retry_at
            ):
                return False
            current.chunks = chunks
            current.canonical_sha256 = chunk_digest(chunks)
            current.warnings = warnings
            current.status, current.error_code, current.retry_at = "ready", None, None
            await db.commit()
            return True
        except Exception as error:
            await db.rollback()
            try:
                current = await owned_file(db, actor, chat_id, file_id, lock=True)
                if (
                    current.status != "pending"
                    or current.attempts != attempts
                    or current.retry_at != retry_at
                ):
                    return False
                current.attempts += 1
                # Keep raw parser/storage exceptions and document contents out of outcomes.
                current.error_code = (
                    error.code if isinstance(error, APIError) else "attachment_processing_failed"
                )
                current.status = "failed" if current.attempts >= 5 else "pending"
                current.retry_at = utcnow() + timedelta(seconds=30 * 2 ** (current.attempts - 1))
                await db.commit()
            except APIError:
                await db.rollback()
            return False


async def pending_files(binary: bool) -> list[UUID]:
    async with session_factory()() as db:
        query = (
            select(ChatFile.id)
            .join(Chat, Chat.id == ChatFile.chat_id)
            .join(User, User.id == ChatFile.owner_id)
            .where(
                ChatFile.status == "pending",
                ChatFile.deleted_at.is_(None),
                Chat.deleted_at.is_(None),
                User.disabled.is_(False),
                ChatFile.attempts < 5,
                or_(ChatFile.retry_at.is_(None), ChatFile.retry_at <= func.now()),
            )
        )
        condition = ChatFile.media_type.in_(TEXT_MEDIA_TYPES)
        query = query.where(~condition if binary else condition)
        return list((await db.scalars(query.order_by(ChatFile.created_at).limit(50))).all())


async def file_evidence(
    db: AsyncSession, actor: User, chat_id: UUID, file_id: UUID
) -> list[dict[str, Any]]:
    item = await owned_file(db, actor, chat_id, file_id, lock=True)
    if (
        item.status != "ready"
        or not item.canonical_sha256
        or chunk_digest(item.chunks) != item.canonical_sha256
    ):
        raise APIError(409, "source_context_changed", "Chat file changed")
    return [
        {
            "evidence_id": f"attachment:{item.id}:{index}",
            "attachment_id": str(item.id),
            "source_kind": "attachment",
            "chat_id": str(chat_id),
            "title": item.name,
            "excerpt": chunk["text"],
            "locator": chunk["locator"],
            "source_sha256": item.source_sha256,
            "canonical_sha256": item.canonical_sha256,
        }
        for index, chunk in enumerate(item.chunks)
    ]


async def restore_file_evidence(
    db: AsyncSession, actor: User, chat_id: UUID, evidence_id: str
) -> dict[str, Any]:
    try:
        prefix, identifier, index = evidence_id.split(":")
        if prefix != "attachment" or int(index) < 0:
            raise ValueError()
        items = await file_evidence(db, actor, chat_id, UUID(identifier))
        item = items[int(index)]
        if item["evidence_id"] != evidence_id:
            raise ValueError()
        return item
    except (ValueError, IndexError):
        raise APIError(409, "source_context_changed", "Chat file changed") from None


async def validate_file_evidence(
    db: AsyncSession, actor: User, chat_id: UUID, evidence: list[dict[str, Any]]
) -> None:
    for item in sorted(evidence, key=lambda entry: entry["evidence_id"]):
        try:
            current = await restore_file_evidence(db, actor, chat_id, item["evidence_id"])
        except APIError as error:
            if error.status == 404:
                raise APIError(409, "source_context_changed", "Chat file changed") from None
            raise
        if any(
            current.get(key) != item.get(key)
            for key in ("excerpt", "source_sha256", "canonical_sha256", "chat_id")
        ):
            raise APIError(409, "source_context_changed", "Chat file changed")


async def attachment_context(
    db: AsyncSession, actor: User, run: Run, question: str
) -> list[dict[str, Any]]:
    manifest = await db.scalar(select(ContextManifest).where(ContextManifest.run_id == run.id))
    previous_ids = (
        select(Message.run_id).where(
            Message.id.in_([UUID(value) for value in manifest.message_ids])
        )
        if manifest
        else select(Run.id).where(Run.id == run.id)
    )
    previous = (
        await db.scalars(
            select(Run)
            .where(
                Run.id.in_(previous_ids),
                Run.chat_id == run.chat_id,
                Run.user_id == actor.id,
                Run.status == "succeeded",
            )
            .order_by(Run.queue_seq.desc())
            .limit(10)
        )
    ).all()
    identifiers = list(
        dict.fromkeys(
            [
                *(run.attachment_ids or []),
                *(value for old in previous for value in (old.attachment_ids or [])),
            ]
        )
    )[:5]
    terms, _ = query_terms(question)
    groups = []
    for identifier in identifiers:
        items = await file_evidence(db, actor, run.chat_id, UUID(identifier))
        groups.append(sorted(items, key=lambda item: -len(terms & query_terms(item["excerpt"])[0])))
    result = []
    # Every attached file receives a slot before adding more excerpts from any one file.
    for index in range(8):
        for items in groups:
            if index < len(items):
                result.append(items[index])
                if len(result) == 8:
                    return result
    return result
