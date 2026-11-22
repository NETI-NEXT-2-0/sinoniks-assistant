"""Private chat file admission. No shared-corpus role or publication is involved."""

import hashlib
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select

from partner_agent.auth.routes import DB, api_rate_limit, authenticated, unsafe_session
from partner_agent.documents.canonical import MAX_FILE_BYTES
from partner_agent.documents.chat_files import file_dto, owned_file
from partner_agent.documents.ingest import DOCLING_MEDIA_TYPES, TEXT_MEDIA_TYPES, store_original
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import utcnow
from partner_agent.infra.models_chat_files import ChatFile
from partner_agent.infra.storage import object_key
from partner_agent.runs.service import lock_chat

router = APIRouter(
    prefix="/api/v1/chats", tags=["chat files"], dependencies=[Depends(api_rate_limit)]
)


@router.get("/{chat_id}/attachments")
async def list_files(chat_id: UUID, request: Request, db: DB) -> dict[str, Any]:
    _, actor = await authenticated(request, db)
    await lock_chat(db, chat_id, actor)
    rows = (
        await db.scalars(
            select(ChatFile)
            .where(
                ChatFile.chat_id == chat_id,
                ChatFile.owner_id == actor.id,
                ChatFile.deleted_at.is_(None),
            )
            .order_by(ChatFile.created_at)
            .limit(20)
        )
    ).all()
    result = {"items": [file_dto(item) for item in rows]}
    await db.commit()
    return result


@router.post("/{chat_id}/attachments", status_code=202)
async def upload_file(
    chat_id: UUID, request: Request, db: DB, name: str = Query(min_length=1, max_length=200)
) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    await lock_chat(db, chat_id, actor)
    await db.commit()
    try:
        upload_key = UUID(request.headers.get("idempotency-key", ""))
    except ValueError:
        raise APIError(422, "idempotency_key_required", "UUID upload key required") from None
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if media_type not in TEXT_MEDIA_TYPES and media_type not in DOCLING_MEDIA_TYPES:
        raise APIError(415, "unsupported_media_type", "Unsupported document type")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_FILE_BYTES:
            raise APIError(413, "file_size_limit", "File exceeds 25 MiB")
        data.extend(chunk)
    if not data:
        raise APIError(413, "file_size_limit", "Empty file")
    await lock_chat(db, chat_id, actor)
    digest = hashlib.sha256(data).hexdigest()
    item = await db.scalar(
        select(ChatFile)
        .where(ChatFile.chat_id == chat_id, ChatFile.upload_key == upload_key)
        .with_for_update()
    )
    if item is not None:
        if (
            item.deleted_at
            or item.source_sha256 != digest
            or item.media_type != media_type
            or item.size_bytes != len(data)
        ):
            raise APIError(409, "idempotency_conflict", "Upload key identifies another file")
    else:
        count = await db.scalar(
            select(func.count())
            .select_from(ChatFile)
            .where(ChatFile.chat_id == chat_id, ChatFile.deleted_at.is_(None))
        )
        if count is not None and count >= 20:
            raise APIError(422, "chat_file_limit", "Up to 20 files per chat")
        identifier = uuid4()
        clean_name = "".join(c for c in name.replace("\\", "/").split("/")[-1] if ord(c) >= 32)
        item = ChatFile(
            id=identifier,
            chat_id=chat_id,
            owner_id=actor.id,
            upload_key=upload_key,
            name=clean_name or "document",
            media_type=media_type,
            size_bytes=len(data),
            source_sha256=digest,
            original_key=object_key(chat_id, identifier, "original"),
            status="pending",
            created_at=utcnow(),
        )
        db.add(item)
        await db.flush()
    result, key, pending = file_dto(item), item.original_key, item.status == "pending"
    await db.commit()
    if pending:
        # Exact-byte replay recovers a PUT interrupted after durable admission.
        await store_original(key, bytes(data), media_type)
        await owned_file(db, actor, chat_id, item.id)
        await db.commit()
    return result


@router.delete("/{chat_id}/attachments/{file_id}", status_code=204)
async def remove_file(chat_id: UUID, file_id: UUID, request: Request, db: DB) -> Response:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    item = await owned_file(db, actor, chat_id, file_id, lock=True)
    item.deleted_at = utcnow()
    await db.commit()
    return Response(status_code=204)


@router.post("/{chat_id}/attachments/{file_id}/retry")
async def retry_file(chat_id: UUID, file_id: UUID, request: Request, db: DB) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    item = await owned_file(db, actor, chat_id, file_id, lock=True)
    if item.status == "ready":
        raise APIError(409, "attachment_already_ready", "File is already ready")
    item.status, item.error_code, item.retry_at, item.attempts = "pending", None, None, 0
    result = file_dto(item)
    await db.commit()
    return result
