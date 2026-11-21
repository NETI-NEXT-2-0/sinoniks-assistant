"""Offline operator reconciliation; never calls a provider or adds a billable attempt."""

import argparse
import asyncio
import hashlib
import json
import re
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from partner_agent.infra.db import get_engine, session_factory
from partner_agent.infra.models import utcnow
from partner_agent.infra.models_accounting import ModelCall
from partner_agent.runtime.accounting_math import usage_record

MAX_IMPORT_BYTES = 1_048_576
MAX_MERGED_USAGE_BYTES = 65_536
MAX_RECONCILIATIONS = 20
USAGE_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
    "provider_cost_usd",
)


class UsageReconciliationError(ValueError):
    """Safe failure code without the supplied report or private response content."""


class UsageRecordImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: UUID
    response_id: str = Field(pattern=r"^[\w.:-]{1,200}$")
    model_id: str = Field(min_length=1, max_length=200)
    provider: str | None = Field(default=None, max_length=200)
    usage: dict[str, Any]


class UsageImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    source: Literal["provider_export"]
    calls: list[UsageRecordImport] = Field(min_length=1, max_length=1000)


def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UsageReconciliationError("duplicate_json_key")
        result[key] = value
    return result


def load_import(path: Path) -> tuple[UsageImport, str]:
    with path.open("rb") as stream:
        content = stream.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise UsageReconciliationError("import_too_large")
    try:
        data = json.loads(content, object_pairs_hook=reject_duplicates)
        batch = UsageImport.model_validate(data)
    except (ValueError, RecursionError) as exc:
        raise UsageReconciliationError("invalid_usage_import") from exc
    if len({record.call_id for record in batch.calls}) != len(batch.calls):
        raise UsageReconciliationError("duplicate_call_id")
    return batch, hashlib.sha256(content).hexdigest()


def checked_usage(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = usage_record(raw)
    for names, field in [
        (("prompt_tokens", "input_tokens"), "input_tokens"),
        (("completion_tokens", "output_tokens"), "output_tokens"),
        (("total_tokens",), "total_tokens"),
        (("cost",), "provider_cost_usd"),
    ]:
        if any(name in raw for name in names) and normalized[field] is None:
            raise UsageReconciliationError("invalid_usage_values")
    if not any(normalized[field] is not None for field in USAGE_FIELDS):
        raise UsageReconciliationError("usage_missing")
    return normalized


def merge_raw(previous: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(previous)
    for key, value in incoming.items():
        old = merged.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            merged[key] = merge_raw(old, value)
        elif key in merged and old != value:
            raise UsageReconciliationError("usage_conflict")
        else:
            merged[key] = value
    return merged


def reconciled_usage(previous: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    incoming = checked_usage(raw)
    for field in USAGE_FIELDS:
        old, new = previous.get(field), incoming[field]
        if old is not None and new is not None:
            equal = Decimal(str(old)) == Decimal(str(new))
            if not equal:
                raise UsageReconciliationError("usage_conflict")
    merged = merge_raw(previous.get("raw") or {}, incoming["raw"] or {})
    if len(json.dumps(merged).encode()) > MAX_MERGED_USAGE_BYTES:
        raise UsageReconciliationError("merged_usage_too_large")
    normalized = checked_usage(merged)
    # Preserve existing normalized facts even in historical rows with no raw projection.
    for field in USAGE_FIELDS:
        if previous.get(field) is not None:
            normalized[field] = previous[field]
    return {**previous, **normalized}


def reconciliation_receipt(previous: dict[str, Any], digest: str) -> dict[str, Any]:
    prior = previous.get("reconciliation")
    history = deepcopy(prior.get("history", [])) if prior else []
    if prior:
        history.append({"import_sha256": prior["import_sha256"],
                        "recorded_at": prior["recorded_at"]})
    if len(history) >= MAX_RECONCILIATIONS:
        raise UsageReconciliationError("reconciliation_history_limit")
    return {"source": "operator_provider_export", "import_sha256": digest,
            "recorded_at": utcnow().isoformat(), "history": history}


async def reconcile_usage(
    db: AsyncSession, batch: UsageImport, digest: str, *, apply: bool = False,
) -> dict[str, int | bool]:
    """Caller owns the transaction. Validate the whole batch before changing any row."""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UsageReconciliationError("invalid_import_digest")
    if len({record.call_id for record in batch.calls}) != len(batch.calls):
        raise UsageReconciliationError("duplicate_call_id")
    ids = [record.call_id for record in batch.calls]
    rows = {row.id: row for row in (await db.scalars(
        select(ModelCall).where(ModelCall.id.in_(ids)).order_by(ModelCall.id).with_for_update()
    )).all()}
    updates: list[tuple[ModelCall, dict[str, Any]]] = []
    for record in batch.calls:
        row = rows.get(record.call_id)
        if row is None:
            raise UsageReconciliationError("call_not_found")
        if row.status == "started" or row.ended_at is None:
            raise UsageReconciliationError("call_not_finished")
        if (row.response_id is None or row.response_id != record.response_id
            or row.model != record.model_id or row.provider != record.provider):
            raise UsageReconciliationError("provider_identity_mismatch")
        updated = reconciled_usage(row.usage, record.usage)
        if updated != row.usage:
            updates.append((row, {**updated,
                                  "reconciliation": reconciliation_receipt(row.usage, digest)}))
    if apply:
        for row, updated in updates:
            row.usage = updated
    return {"calls_checked": len(batch.calls), "calls_changed": len(updates), "applied": apply}


async def run_import(path: Path, apply: bool) -> dict[str, int | bool]:
    batch, digest = load_import(path)
    try:
        async with session_factory()() as db, db.begin():
            return await reconcile_usage(db, batch, digest, apply=apply)
    finally:
        await get_engine().dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Private provider-export JSON")
    parser.add_argument("--apply", action="store_true", help="Apply; default only validates")
    options = parser.parse_args()
    try:
        result = asyncio.run(run_import(options.input, options.apply))
    except (OSError, UsageReconciliationError, ValidationError):
        parser.exit(2, "Usage import rejected; no batch applied. "
                       "Check schema, identity and values.\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
