"""Record actual HTTP attempts before dispatch, including rejected paid outputs."""

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from partner_agent.infra.db import session_factory
from partner_agent.infra.models import utcnow
from partner_agent.infra.models_accounting import ModelCall
from partner_agent.infra.settings import Settings
from partner_agent.runtime.accounting_math import priced_usage, usage_record

_run: ContextVar[UUID | None] = ContextVar("model_accounting_run", default=None)


@contextmanager
def accounting_scope(run_id: UUID) -> Iterator[None]:
    token = _run.set(run_id)
    try:
        yield
    finally:
        _run.reset(token)


@dataclass
class Capture:
    body: Any = None


def tariff_snapshot(settings: Settings) -> dict[str, Any]:
    declared = settings.model_pricing_declared or bool(
        settings.model_input_usd_per_million or settings.model_output_usd_per_million
    )
    return {
        "catalog_id": settings.model_catalog_id or None,
        "provider_variant": settings.model_provider_variant or None,
        "snapshot": settings.model_price_snapshot or None,
        "contest_mode": settings.model_contest_mode,
        "input_usd_per_million": str(settings.model_input_usd_per_million) if declared else None,
        "output_usd_per_million": str(settings.model_output_usd_per_million) if declared else None,
        "declared": declared,
        "eligible_input_price": (settings.model_input_usd_per_million <= 0.10
                                 if declared else None),
    }


@asynccontextmanager
async def track_model_call(settings: Settings) -> AsyncIterator[Capture]:
    capture = Capture()
    run_id = _run.get()
    if run_id is None:
        yield capture
        return
    async with session_factory()() as db:
        row = ModelCall(run_id=run_id, model=settings.model_name,
                        provider=settings.model_provider_variant or None,
                        tariff=tariff_snapshot(settings))
        db.add(row)
        await db.commit()  # Fail closed: no unrecorded outbound call on DB failure.
        call_id = row.id
    status = "succeeded"
    error_code = None
    try:
        yield capture
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        code = getattr(exc, "code", "provider_request_failed")
        error_code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else None
        raise
    finally:
        async def persist() -> None:
            async with session_factory()() as db:
                saved = await db.get(ModelCall, call_id, with_for_update=True)
                assert saved is not None
                body = capture.body if isinstance(capture.body, dict) else {}
                saved.usage = usage_record(body.get("usage"))
                reported_model = body.get("model")
                if (isinstance(reported_model, str)
                    and re.fullmatch(r"[\w./:@-]{1,200}", reported_model)):
                    saved.usage = {**saved.usage, "reported_model": reported_model}
                response_id = body.get("id")
                saved.response_id = (response_id if isinstance(response_id, str)
                                     and re.fullmatch(r"[\w.:-]{1,200}", response_id) else None)
                saved.status = status
                saved.error_code = error_code
                saved.ended_at = utcnow()
                await db.commit()

        # Cancellation of a run must not cancel the independent accounting commit.
        task = asyncio.create_task(persist())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise


def call_dto(row: ModelCall) -> dict[str, Any]:
    return {
        "call_id": str(row.id), "turn_id": str(row.run_id), "model_id": row.model,
        "provider": row.provider, "purpose": row.purpose, "status": row.status,
        "started_at": row.started_at.isoformat(),
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "response_id": row.response_id, "error_code": row.error_code,
        "raw_usage_format": "numeric_projection_no_response_content",
        "usage_source": "provider" if row.usage.get("raw") else "unknown",
        "usage": row.usage, "tariff": row.tariff,
        **priced_usage(row.usage, row.tariff.get("input_usd_per_million"),
                       row.tariff.get("output_usd_per_million")),
    }
