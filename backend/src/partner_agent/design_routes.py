"""Saved conceptual Sinoniks projects with immutable revisions and exact exports."""

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from partner_agent.auth.routes import DB, api_rate_limit, authenticated, unsafe_session
from partner_agent.chats.routes import selected_branch
from partner_agent.design_service import create_revision, latest_revision
from partner_agent.infra.errors import APIError
from partner_agent.infra.models_designs import DesignRevision
from partner_agent.runs.service import lock_chat
from partner_agent.sinoniks_contract import DesignInput

router = APIRouter(
    prefix="/api/v1/chats/{chat_id}/designs", tags=["designs"],
    dependencies=[Depends(api_rate_limit)],
)


class SaveDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    branch_id: UUID
    parent_id: UUID | None = None
    input: DesignInput


def revision_dto(row: DesignRevision) -> dict[str, Any]:
    return {
        "id": str(row.id), "branch_id": str(row.branch_id), "revision": row.revision,
        "parent_id": str(row.parent_id) if row.parent_id else None,
        "input": row.input, "questions": row.questions, "result": row.result,
        "status": "needs_clarification" if row.questions else "conceptual",
        "created_at": row.created_at.isoformat(),
        "artifact_sha256": {
            extension: hashlib.sha256(content.encode()).hexdigest()
            for extension, content in row.artifacts.items()
        },
    }


@router.get("/latest")
async def latest(
    chat_id: UUID, branch_id: UUID, request: Request, response: Response, db: DB,
) -> dict[str, Any]:
    _, actor = await authenticated(request, db)
    chat = await lock_chat(db, chat_id, actor)
    await selected_branch(db, chat, branch_id)
    row = await latest_revision(db, chat_id, branch_id)
    result = {"design": revision_dto(row) if row else None}
    response.headers["Cache-Control"] = "private, no-store"
    await db.commit()
    return result


@router.post("", status_code=201)
async def save(
    chat_id: UUID, body: SaveDesign, request: Request, response: Response, db: DB,
    idempotency_key: UUID = Header(),
) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    chat = await lock_chat(db, chat_id, actor)
    await selected_branch(db, chat, body.branch_id)
    serialized = json.dumps(body.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    existing = await db.scalar(select(DesignRevision).where(
        DesignRevision.chat_id == chat_id, DesignRevision.idempotency_key == idempotency_key,
    ))
    if existing is not None:
        if existing.request_sha256 != digest:
            raise APIError(409, "idempotency_conflict", "The key belongs to another design request")
        response.headers["Cache-Control"] = "private, no-store"
        result = revision_dto(existing)
        await db.commit()
        return result
    row = await create_revision(
        db, chat_id=chat_id, branch_id=body.branch_id, parent_id=body.parent_id,
        conditions=body.input, key=idempotency_key, digest=digest,
    )
    result = revision_dto(row)
    response.headers["Cache-Control"] = "private, no-store"
    await db.commit()
    return result


@router.get("/{design_id}")
async def get_design(
    chat_id: UUID, design_id: UUID, request: Request, response: Response, db: DB,
) -> dict[str, Any]:
    _, actor = await authenticated(request, db)
    await lock_chat(db, chat_id, actor)
    row = await db.get(DesignRevision, design_id)
    if row is None or row.chat_id != chat_id:
        raise APIError(404, "not_found", "Design not found")
    result = revision_dto(row)
    response.headers["Cache-Control"] = "private, no-store"
    await db.commit()
    return result


@router.post("/{design_id}/download/{extension}")
async def download(
    chat_id: UUID, design_id: UUID, extension: Literal["json", "drawio", "txt"],
    request: Request, db: DB,
) -> Response:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    await lock_chat(db, chat_id, actor)
    row = await db.get(DesignRevision, design_id)
    if row is None or row.chat_id != chat_id:
        raise APIError(404, "not_found", "Design not found")
    content = row.artifacts.get(extension)
    if content is None:
        raise APIError(409, "design_incomplete", "Complete the project before downloading")
    data = content.encode()
    await db.commit()
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="sinoniks-{design_id}.{extension}"',
        "Cache-Control": "private, no-store",
        "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
    })
