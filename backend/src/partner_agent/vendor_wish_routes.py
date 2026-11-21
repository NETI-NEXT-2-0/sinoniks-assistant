from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select

from partner_agent.auth.routes import DB, api_rate_limit, authenticated
from partner_agent.infra.errors import APIError
from partner_agent.infra.models_stage2 import Admission, Run
from partner_agent.runs.service import lock_chat

router = APIRouter(prefix="/api/v1/chats", tags=["vendor-wishes"],
                   dependencies=[Depends(api_rate_limit)])


@router.get("/{chat_id}/vendor-wishes/export")
async def export_vendor_wishes(chat_id: UUID, request: Request, db: DB) -> JSONResponse:
    _, actor = await authenticated(request, db)
    await lock_chat(db, chat_id, actor)
    wishes = Run.outcome["vendor_wishes"]
    rows = (await db.execute(
        select(Run.id, Run.input_message_id, Run.branch_id, Run.created_at, Run.queue_seq,
               wishes.label("wishes"))
        .where(Run.chat_id == chat_id, Run.status == "succeeded", wishes.is_not(None))
        .order_by(Run.queue_seq)
        .limit(1001)
    )).mappings().all()
    wish = Admission.response["vendor_wish"]
    remarks = Admission.response["project_answer_remarks"]
    submitted = (await db.execute(
        select(Run.id, Run.input_message_id, Run.branch_id, Run.created_at, Run.queue_seq,
               wish.label("wish"), remarks.label("remarks"))
        .join(Admission, Admission.run_id == Run.id)
        .where(Run.chat_id == chat_id, Admission.chat_id == chat_id, Admission.operation == "run",
               or_(wish.is_not(None), remarks.is_not(None)))
        .order_by(Run.queue_seq).limit(1001)
    )).mappings().all()
    if len(rows) > 1000 or len(submitted) > 1000:
        raise APIError(422, "vendor_wishes_export_limit", "Session exceeds the wish export limit")
    unique: dict[tuple[str | None, ...], dict[str, str | None]] = {}
    combined = [
        (row, [row["wish"]] if row["wish"] else row["remarks"], "user_submission")
        for row in submitted
    ]
    combined.extend((row, row["wishes"], "model_quote") for row in rows)
    combined.sort(key=lambda item: item[0]["queue_seq"])
    for row, entries, origin in combined:
        for entry in entries:
            if entry.get("purpose") == "project_clarification":
                continue
            key = (
                entry["kind"], entry["text"], entry.get("base_revision_id"), entry.get("path"),
            )
            unique.setdefault(key, {
                **entry, "turn_id": str(row["id"]), "message_id": str(row["input_message_id"]),
                "branch_id": str(row["branch_id"]), "created_at": row["created_at"].isoformat(),
                "origin": origin,
            })
    if len(unique) > 1000:
        raise APIError(422, "vendor_wishes_export_limit", "Session exceeds the wish export limit")
    return JSONResponse({
        "schema_version": 1, "session_id": str(chat_id), "wishes": list(unique.values()),
    }, headers={
        "Content-Disposition": f'attachment; filename="vendor-wishes-{chat_id}.json"',
        "Cache-Control": "private, no-store",
    })
