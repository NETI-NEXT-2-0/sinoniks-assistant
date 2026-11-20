"""Owner-only session accounting and render-completion measurements."""

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from partner_agent.auth.routes import DB, api_rate_limit, authenticated, unsafe_session
from partner_agent.infra.errors import APIError
from partner_agent.infra.models import utcnow
from partner_agent.infra.models_accounting import ModelCall, TurnTiming
from partner_agent.infra.models_stage2 import Run
from partner_agent.runs.service import TERMINAL, lock_chat
from partner_agent.runtime.accounting import call_dto

router = APIRouter(prefix="/api/v1/chats", tags=["accounting"],
                   dependencies=[Depends(api_rate_limit)])


def summarize(calls: list[dict[str, Any]]) -> dict[str, Any]:
    def money(key: str) -> str | None:
        values = [item[key] for item in calls]
        return (str(sum((Decimal(value) for value in values), Decimal(0)))
                if all(value is not None for value in values) else None)

    def tokens(key: str) -> int | None:
        values = [item["usage"].get(key) for item in calls]
        return sum(values) if all(value is not None for value in values) else None

    return {
        "calls_count": len(calls),
        "models": sorted({item["model_id"] for item in calls}),
        "input_tokens": tokens("input_tokens"), "output_tokens": tokens("output_tokens"),
        "input_cost_usd": money("input_cost_usd"),
        "output_cost_usd": money("output_cost_usd"),
        "estimated_cost_usd": money("estimated_cost_usd"),
        "provider_cost_usd": (str(sum((Decimal(item["usage"]["provider_cost_usd"])
                                      for item in calls), Decimal(0)))
                              if all(item["usage"].get("provider_cost_usd") is not None
                                     for item in calls) else None),
        "unknown_cost_calls": sum(item["estimated_cost_usd"] is None for item in calls),
        "unfinished_calls": sum(item["status"] == "started" for item in calls),
    }


@router.get("/{chat_id}/accounting")
async def session_accounting(chat_id: UUID, request: Request, db: DB) -> dict[str, Any]:
    _, actor = await authenticated(request, db)
    await lock_chat(db, chat_id, actor)
    runs = list((await db.scalars(select(Run).where(Run.chat_id == chat_id)
                                 .order_by(Run.created_at))).all())
    rows = list((await db.scalars(select(ModelCall).join(Run, Run.id == ModelCall.run_id)
                                 .where(Run.chat_id == chat_id)
                                 .order_by(ModelCall.started_at).limit(10001))).all())
    if len(rows) > 10000:
        raise APIError(422, "accounting_export_limit", "Session ledger exceeds export limit")
    calls = [call_dto(row) for row in rows]
    known = {row.run_id for row in rows}
    legacy = sum(run.id not in known and (run.outcome or {}).get("profile") == "openai_compatible"
                 for run in runs)
    timings = {row.run_id: row.elapsed_ms for row in (await db.scalars(
        select(TurnTiming).join(Run, Run.id == TurnTiming.run_id).where(Run.chat_id == chat_id)
    )).all()}
    turns = [{"turn_id": str(run.id), "status": run.status,
              "elapsed_ms": timings.get(run.id),
              "server_elapsed_ms": (int((run.finished_at - run.created_at).total_seconds() * 1000)
                                    if run.finished_at else None)} for run in runs]
    total_ms = sum(timings.values())
    tariff_complete = all(
        row.tariff.get("eligible_input_price") is True and row.tariff.get("catalog_id")
        and row.tariff.get("provider_variant") and row.tariff.get("snapshot") for row in rows
    )
    summary = summarize(calls)
    summary["known_recorded_cost_usd"] = str(sum(
        (Decimal(call["estimated_cost_usd"]) for call in calls
         if call["estimated_cost_usd"] is not None), Decimal(0),
    ))
    if legacy:
        summary["estimated_cost_usd"] = None
    return {"schema_version": "1", "session_id": str(chat_id), "calls": calls,
            "summary": summary, "legacy_untracked_turns": legacy,
            "by_model": [{"model_id": model, **summarize([
                call for call in calls if call["model_id"] == model
            ])} for model in summary["models"]],
            "accounting_complete": (legacy == 0 and summary["unknown_cost_calls"] == 0
                                    and summary["unfinished_calls"] == 0),
            "tariff_declaration_complete": tariff_complete,
            "turns": turns, "timing_source": "client_answer_and_result_card",
            "timing_complete": len(timings) == len(runs),
            "mean_elapsed_ms": total_ms / len(runs) if runs and len(timings) == len(runs) else None}


@router.get("/{chat_id}/accounting/export")
async def export_accounting(chat_id: UUID, request: Request, db: DB) -> Response:
    payload = await session_accounting(chat_id, request, db)
    return Response(json.dumps(payload, ensure_ascii=False, indent=2),
                    media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="usage-{chat_id}.json"',
                             "Cache-Control": "no-store"})


class TimingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    elapsed_ms: int = Field(ge=0, le=86_400_000, strict=True)


@router.post("/{chat_id}/accounting/turns/{run_id}/rendered")
async def rendered(
    chat_id: UUID, run_id: UUID, body: TimingBody, request: Request, db: DB,
) -> dict[str, Any]:
    await unsafe_session(request, db)
    _, actor = await authenticated(request, db)
    await lock_chat(db, chat_id, actor)
    run = await db.get(Run, run_id)
    if run is None or run.chat_id != chat_id:
        raise APIError(404, "not_found", "Turn not found")
    if run.status not in TERMINAL and run.status != "waiting_input":
        raise APIError(409, "turn_not_complete", "Turn is still running")
    existing = await db.get(TurnTiming, run_id)
    if existing is not None:
        return {"turn_id": str(run_id), "elapsed_ms": existing.elapsed_ms}
    if run.finished_at is not None:
        floor = int((run.finished_at - run.created_at).total_seconds() * 1000)
        if body.elapsed_ms + 10 < floor:
            raise APIError(422, "timing_before_server_completion", "Invalid elapsed time")
    # The first observation is immutable: refresh/retry cannot shorten the measured step.
    await db.execute(insert(TurnTiming).values(run_id=run_id, elapsed_ms=body.elapsed_ms,
                                              recorded_at=utcnow())
                     .on_conflict_do_nothing(index_elements=["run_id"]))
    await db.commit()
    timing = await db.get(TurnTiming, run_id)
    assert timing is not None
    return {"turn_id": str(run_id), "elapsed_ms": timing.elapsed_ms}
