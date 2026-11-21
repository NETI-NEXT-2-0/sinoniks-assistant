"""Saved conceptual Sinoniks projects with immutable revisions and exact exports."""

import hashlib
import json
from datetime import UTC
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from partner_agent.accounting_routes import read_session_accounting
from partner_agent.auth.routes import DB, api_rate_limit, authenticated, unsafe_session
from partner_agent.chats.routes import selected_branch
from partner_agent.design_service import (
    create_revision,
    latest_revision,
    revision_status,
    validate_project_sources,
)
from partner_agent.infra.errors import APIError
from partner_agent.infra.models_designs import DesignRevision
from partner_agent.project_answers import question_choices
from partner_agent.requirements_result import expert_accounting
from partner_agent.runs.service import lock_chat
from partner_agent.sinoniks_contract import DesignInput
from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_contract19_io import assess, upgrade_input

router = APIRouter(
    prefix="/api/v1/chats/{chat_id}/designs", tags=["designs"],
    dependencies=[Depends(api_rate_limit)],
)


class SaveDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    branch_id: UUID
    parent_id: UUID | None = None
    input: DesignInput | Requirements19


class ValidateRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    branch_id: UUID
    input: Requirements19


@router.post("/requirements/validate")
async def validate_requirements(
    chat_id: UUID, body: ValidateRequirements, request: Request, response: Response, db: DB,
) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    chat = await lock_chat(db, chat_id, actor)
    await selected_branch(db, chat, body.branch_id)
    result = {"input": body.input.model_dump(mode="json"),
              "assessment": assess(body.input).model_dump(mode="json"),
              "saved": False}
    response.headers["Cache-Control"] = "private, no-store"
    await db.commit()
    return result


def revision_dto(row: DesignRevision) -> dict[str, Any]:
    return {
        "id": str(row.id), "branch_id": str(row.branch_id), "revision": row.revision,
        "parent_id": str(row.parent_id) if row.parent_id else None,
        "input": row.input, "questions": row.questions, "result": row.result,
        "status": revision_status(row),
        "created_at": row.created_at.astimezone(UTC).isoformat(),
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
    if row is not None:
        await validate_project_sources(db, actor, chat_id, row)
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
        await validate_project_sources(db, actor, chat_id, existing)
        response.headers["Cache-Control"] = "private, no-store"
        result = revision_dto(existing)
        await db.commit()
        return result
    previous = await latest_revision(db, chat_id, body.branch_id)
    if previous is not None:
        await validate_project_sources(db, actor, chat_id, previous)
    row = await create_revision(
        db, chat_id=chat_id, branch_id=body.branch_id, parent_id=body.parent_id,
        conditions=body.input, key=idempotency_key, digest=digest,
    )
    result = revision_dto(row)
    response.headers["Cache-Control"] = "private, no-store"
    await db.commit()
    return result


class UpgradeDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    branch_id: UUID


@router.post("/{design_id}/upgrade", status_code=201)
async def upgrade_design(
    chat_id: UUID, design_id: UUID, body: UpgradeDesign, request: Request,
    response: Response, db: DB, idempotency_key: UUID = Header(),
) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    chat = await lock_chat(db, chat_id, actor)
    await selected_branch(db, chat, body.branch_id)
    serialized = json.dumps({"operation": "upgrade19", "source": str(design_id),
                             **body.model_dump(mode="json")}, sort_keys=True)
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    existing = await db.scalar(select(DesignRevision).where(
        DesignRevision.chat_id == chat_id, DesignRevision.idempotency_key == idempotency_key,
    ))
    if existing is not None:
        if existing.request_sha256 != digest:
            raise APIError(409, "idempotency_conflict", "Key belongs to another command")
        row = existing
        await validate_project_sources(db, actor, chat_id, row)
    else:
        source = await latest_revision(db, chat_id, body.branch_id)
        if source is None or source.id != design_id:
            raise APIError(409, "design_revision_conflict", "Reload before upgrading")
        await validate_project_sources(db, actor, chat_id, source)
        if source.input.get("schema_version") == 3:
            raise APIError(409, "design_already_upgraded", "Requirements already use 1.9")
        try:
            conditions = upgrade_input(DesignInput.model_validate(source.input),
                                       project_id="project-" + str(source.id))
        except ValueError:
            raise APIError(409, "design_kind_unknown",
                           "Clarify data/file kinds before upgrading") from None
        row = await create_revision(
            db, chat_id=chat_id, branch_id=body.branch_id, parent_id=source.id,
            conditions=conditions, key=idempotency_key, digest=digest, allow_upgrade=True,
        )
    response.headers["Cache-Control"] = "private, no-store"
    result = revision_dto(row)
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
    await validate_project_sources(db, actor, chat_id, row)
    result = revision_dto(row)
    response.headers["Cache-Control"] = "private, no-store"
    await db.commit()
    return result


@router.get("/{design_id}/questions")
async def get_questions(
    chat_id: UUID, design_id: UUID, branch_id: UUID, request: Request, response: Response, db: DB,
    offset: int = Query(default=0, ge=0, le=10000),
) -> dict[str, Any]:
    _, actor = await authenticated(request, db)
    chat = await lock_chat(db, chat_id, actor)
    await selected_branch(db, chat, branch_id)
    row = await db.get(DesignRevision, design_id)
    if row is None or row.chat_id != chat_id:
        raise APIError(404, "not_found", "Project not found")
    await validate_project_sources(db, actor, chat_id, row)
    if row.input.get("schema_version") != 3:
        raise APIError(409, "project_answer_version_invalid", "Use requirements 1.9")
    latest = await latest_revision(db, chat_id, branch_id)
    questions = question_choices(Requirements19.model_validate(row.input))
    result = {"revision_id": str(row.id), "current": latest is not None and latest.id == row.id,
              "questions": questions[offset:offset + 6], "total": len(questions),
              "next_offset": offset + 6 if offset + 6 < len(questions) else None}
    response.headers["Cache-Control"] = "private, no-store"
    await db.commit()
    return result


@router.get("/{design_id}/diagram")
async def diagram(
    chat_id: UUID, design_id: UUID, request: Request, response: Response, db: DB,
) -> dict[str, Any]:
    _, actor = await authenticated(request, db)
    await lock_chat(db, chat_id, actor)
    row = await db.get(DesignRevision, design_id)
    if row is None or row.chat_id != chat_id:
        raise APIError(404, "not_found", "Design not found")
    await validate_project_sources(db, actor, chat_id, row)
    content = row.artifacts.get("diagram")
    if content is None:
        raise APIError(409, "diagram_not_available", "This revision has no saved browser diagram")
    scene: dict[str, Any] = json.loads(content)
    response.headers["Cache-Control"] = "private, no-store"
    await db.commit()
    return {"revision_id": str(row.id), "revision": row.revision, "scene": scene,
            "sha256": hashlib.sha256(content.encode()).hexdigest()}


@router.post("/{design_id}/download/{extension}")
async def download(
    chat_id: UUID, design_id: UUID, extension: Literal["json", "drawio", "txt", "requirements"],
    request: Request, db: DB,
    include_accounting: bool = False,
) -> Response:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    await lock_chat(db, chat_id, actor)
    row = await db.get(DesignRevision, design_id)
    if row is None or row.chat_id != chat_id:
        raise APIError(404, "not_found", "Design not found")
    await validate_project_sources(db, actor, chat_id, row)
    content = row.artifacts.get(extension)
    if content is None:
        raise APIError(409, "design_incomplete", "Complete the project before downloading")
    if include_accounting:
        if extension != "json" or row.input.get("schema_version") != 3:
            raise APIError(422, "expert_export_requires_v3_json", "Select requirements 1.9 JSON")
        result = json.loads(content)
        ledger = await read_session_accounting(chat_id, db)
        result.update(expert_accounting(ledger))
        result["revision_id"] = str(row.id)
        result["configuration_sha256"] = hashlib.sha256(content.encode()).hexdigest()
        content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    data = content.encode()
    await db.commit()
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="sinoniks-{design_id}.{extension}"',
        "Cache-Control": "private, no-store",
        "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
    })
