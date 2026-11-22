"""Real PostgreSQL, late synthetic provider reports, no paid API access."""

from copy import deepcopy
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_chat_data_plane import complete, new_chat, submit
from test_chat_data_plane import plane as plane
from test_session_accounting import settings

from partner_agent.infra.db import session_factory
from partner_agent.infra.models import utcnow
from partner_agent.infra.models_accounting import ModelCall
from partner_agent.infra.models_stage2 import Run
from partner_agent.runtime.accounting import accounting_scope
from partner_agent.runtime.accounting_math import usage_record
from partner_agent.runtime.provider import ProviderError, chat_completion
from partner_agent.runtime.usage_reconciliation import (
    UsageImport,
    UsageReconciliationError,
    reconcile_usage,
)

pytestmark = pytest.mark.integration
DIGEST = "a" * 64


async def prepared(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    run = UUID((await submit(client, headers, chat)).json()["run_id"])
    await complete(run)
    async with session_factory()() as db:
        call = ModelCall(
            id=uuid4(), run_id=run, model="synthetic/model", provider="synthetic/route",
            response_id="resp-synthetic", status="failed", ended_at=utcnow(),
            error_code="provider_request_failed", usage=usage_record({"input_tokens": 10000}),
            tariff={"input_usd_per_million": "0.1", "output_usd_per_million": "0.4"},
        )
        db.add(call)
        await db.commit()
        row = {"call_id": str(call.id), "response_id": call.response_id,
               "model_id": call.model, "provider": call.provider,
               "usage": {"input_tokens": 10000, "output_tokens": 2000, "cost": .002}}
    return client, chat, call.id, row


def batch(*rows):
    return UsageImport.model_validate({"schema_version": 1, "source": "provider_export",
                                       "calls": rows})


async def test_late_usage_dry_run_atomic_import_export_and_repeat(plane):
    client, chat, call_id, row = await prepared(plane)
    outsider, _, _ = await plane("admin")
    path = f"/api/v1/chats/{chat['id']}/accounting"
    before = (await client.get(path)).json()
    assert before["summary"]["estimated_cost_usd"] is None
    async with session_factory()() as db, db.begin():
        preview = await reconcile_usage(db, batch(row), DIGEST)
        assert preview == {"calls_checked": 1, "calls_changed": 1, "applied": False}
    assert (await client.get(path)).json() == before

    async with session_factory()() as db, db.begin():
        assert (await reconcile_usage(db, batch(row), DIGEST, apply=True))["calls_changed"] == 1
    exported = (await client.get(path + "/export")).json()
    assert exported["summary"]["calls_count"] == 1
    assert exported["summary"]["estimated_cost_usd"] == "0.0018"
    assert exported["summary"]["provider_cost_usd"] == "0.002"
    call = exported["calls"][0]
    assert call["call_id"] == str(call_id) and call["status"] == "failed"
    assert call["error_code"] == "provider_request_failed"
    assert call["usage_source"] == "operator_provider_export"
    assert call["usage"]["reconciliation"]["import_sha256"] == DIGEST
    async with session_factory()() as db, db.begin():
        assert (await reconcile_usage(db, batch(row), "b" * 64, apply=True))["calls_changed"] == 0
        assert (await db.scalar(select(func.count()).select_from(ModelCall).where(
            ModelCall.run_id == (await db.get(ModelCall, call_id)).run_id,
        ))) == 1
    assert (await client.get(path + "/export")).json() == exported
    assert (await outsider.get(path + "/export")).status_code == 404

async def test_retry_late_usage_and_local_turn_recompute_one_session(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    run_id = UUID((await submit(client, headers, chat)).json()["run_id"])
    requests = []

    def receive(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"id": "late-report", "usage": {"input_tokens": 10000}})
        return httpx.Response(200, json={"id": "retry-report", "choices": [],
                                        "usage": {"input_tokens": 10000, "output_tokens": 2000,
                                                  "cost": .002}})

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(receive), **kwargs)

    with accounting_scope(run_id):
        with pytest.raises(ProviderError):
            await chat_completion(settings(), [], client_factory=factory)
        await chat_completion(settings(), [], client_factory=factory)
    await complete(run_id)
    local_id = UUID((await submit(client, headers, chat)).json()["run_id"])
    await complete(local_id)
    async with session_factory()() as db:
        local = await db.get(Run, local_id)
        local.outcome = {"execution": "local_project_answer", "provider_calls": 0}
        await db.commit()
        late = await db.scalar(select(ModelCall).where(ModelCall.response_id == "late-report",
                                                       ModelCall.run_id == run_id))
        imported = {"call_id": str(late.id), "response_id": late.response_id,
                    "model_id": late.model, "provider": late.provider,
                    "usage": {"prompt_tokens": 10000, "output_tokens": 2000, "cost": .002}}
    path = f"/api/v1/chats/{chat['id']}/accounting"
    unknown = (await client.get(path)).json()
    assert unknown["summary"]["calls_count"] == 2
    assert unknown["summary"]["estimated_cost_usd"] is None
    assert unknown["summary"]["known_recorded_cost_usd"] == "0.0018"
    async with session_factory()() as db, db.begin():
        await reconcile_usage(db, batch(imported), DIGEST, apply=True)
    current = (await client.get(path)).json()
    assert current["summary"]["estimated_cost_usd"] == "0.0036"
    assert current["summary"]["provider_cost_usd"] == "0.004"
    assert current["summary"]["calls_count"] == 2
    assert current["summary"]["input_tokens"] == 20000
    assert current["summary"]["output_tokens"] == 4000
    assert current["accounting_complete"] is True and len(requests) == 2
    assert len(current["turns"]) == 2 and len(current["by_model"]) == 1
    elapsed = []
    for turn in current["turns"]:
        duration = turn["server_elapsed_ms"] + 100
        elapsed.append(duration)
        measured = await client.post(path + f"/turns/{turn['turn_id']}/rendered", headers=headers,
                                     json={"elapsed_ms": duration, "scope": "full_result"})
        assert measured.status_code == 200
    exported = (await client.get(path + "/export")).json()
    assert exported["full_timing_complete"] is True
    assert exported["mean_full_elapsed_ms"] == sum(elapsed) / 2



async def test_partial_reports_preserve_previous_import_receipts(plane):
    _, _, call_id, row = await prepared(plane)
    first = deepcopy(row)
    first["usage"] = {"output_tokens": 2000}
    async with session_factory()() as db, db.begin():
        await reconcile_usage(db, batch(first), DIGEST, apply=True)
    async with session_factory()() as db, db.begin():
        await reconcile_usage(db, batch(row), "b" * 64, apply=True)
        call = await db.get(ModelCall, call_id)
        receipt = call.usage["reconciliation"]
        assert receipt["import_sha256"] == "b" * 64
        assert receipt["history"][0]["import_sha256"] == DIGEST
        assert len(receipt["history"]) == 1
        assert call.usage["input_tokens"] == 10000 and call.usage["output_tokens"] == 2000


@pytest.mark.parametrize("change", [
    "response", "model", "provider", "unknown", "active", "conflict",
])
async def test_bad_report_or_active_attempt_cannot_change_ledger(plane, change):
    client, chat, call_id, row = await prepared(plane)
    path = f"/api/v1/chats/{chat['id']}/accounting/export"
    if change == "active":
        async with session_factory()() as db:
            stored = await db.get(ModelCall, call_id)
            stored.status, stored.ended_at = "started", None
            await db.commit()
    before = (await client.get(path)).json()
    altered = deepcopy(row)
    if change == "response":
        altered["response_id"] = "different"
    elif change == "model":
        altered["model_id"] = "different"
    elif change == "provider":
        altered["provider"] = "different"
    elif change == "unknown":
        altered["call_id"] = str(uuid4())
    elif change == "conflict":
        altered["usage"]["input_tokens"] = 999
    # Put a valid change first: a later error must still apply no part of a batch.
    valid = deepcopy(row)
    if altered["call_id"] == valid["call_id"]:
        valid["call_id"] = str(uuid4())
        async with session_factory()() as db:
            source = await db.get(ModelCall, call_id)
            db.add(ModelCall(id=UUID(valid["call_id"]), run_id=source.run_id, model=source.model,
                             provider=source.provider, response_id=source.response_id,
                             status="failed", ended_at=utcnow(), usage=deepcopy(source.usage)))
            await db.commit()
        before = (await client.get(path)).json()
    async with session_factory()() as db:
        with pytest.raises(UsageReconciliationError):
            await reconcile_usage(db, batch(valid, altered), DIGEST, apply=True)
        await db.commit()  # Even a caller that catches the error cannot flush partial updates.
    assert (await client.get(path)).json() == before
