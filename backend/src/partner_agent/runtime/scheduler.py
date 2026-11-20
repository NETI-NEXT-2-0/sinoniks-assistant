"""Actual Hatchet scheduling; payloads carry identifiers only, never chat content."""

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from hatchet_sdk import (
    Context,
    Hatchet,
    TTLBasedIdempotencyConfig,
)
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel, ConfigDict, Field, model_validator

TASK_NAME = "partner-agent-run-v1"


class DispatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    run_id: UUID
    chat_id: UUID
    user_id: UUID
    dispatch_generation: int = Field(ge=0)
    dispatch_key: str

    @model_validator(mode="after")
    def stable_key(self) -> "DispatchInput":
        if self.dispatch_key != f"{self.run_id}:{self.dispatch_generation}":
            raise ValueError("Dispatch key does not match the run generation")
        return self


def client() -> Hatchet:
    logging.getLogger("hatchet").setLevel(logging.WARNING)
    try:
        return Hatchet()
    except Exception:
        raise RuntimeError("Local scheduler credentials/configuration are unavailable") from None


class Scheduler:
    def __init__(self, hatchet: Hatchet | None = None) -> None:
        self.hatchet = hatchet or client()
        self.task = self.hatchet.stubs.task(name=TASK_NAME, input_validator=DispatchInput)

    async def dispatch(self, payload: DispatchInput) -> str:
        reference = await self.task.aio_run(payload, wait_for_result=False)
        return reference.workflow_run_id

    async def status(self, scheduler_id: str) -> str:
        return (await self.hatchet.runs.aio_get_status(scheduler_id)).value

    async def cancel(self, scheduler_id: str) -> None:
        await self.hatchet.runs.aio_cancel(scheduler_id)


def worker() -> Any:
    from partner_agent.runtime.runner import execute_run

    hatchet = client()
    round_robin = ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN

    @hatchet.task(
        name=TASK_NAME,
        version="fixture-v1",
        input_validator=DispatchInput,
        retries=2,
        execution_timeout=timedelta(minutes=6),
        schedule_timeout=timedelta(hours=24),
        # User groups are round-robin before the global cap. Independent chats
        # both global slots; PG independently enforces each chat FIFO.
        concurrency=[
            ConcurrencyExpression(
                expression="input.user_id", max_runs=2, limit_strategy=round_robin
            ),
            ConcurrencyExpression(
                expression="input.chat_id", max_runs=1, limit_strategy=round_robin
            ),
            ConcurrencyExpression(expression="'answers'", max_runs=2, limit_strategy=round_robin),
        ],
        idempotency=TTLBasedIdempotencyConfig(
            key_expression="input.dispatch_key", ttl=timedelta(hours=24)
        ),
    )
    async def run(payload: DispatchInput, ctx: Context) -> dict[str, str]:
        return await execute_run(
            payload.run_id,
            dispatch_generation=payload.dispatch_generation,
            scheduler_id=ctx.workflow_run_id,
        )

    return hatchet.worker("partner-agent-worker", workflows=[run], slots=2)


def main() -> None:
    worker().start()


if __name__ == "__main__":
    main()
