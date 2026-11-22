"""Real PostgreSQL, synthetic HTTP provider: no paid API requests."""

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from test_chat_data_plane import complete, new_chat, submit
from test_chat_data_plane import plane as plane

from partner_agent.infra.db import session_factory
from partner_agent.infra.models import utcnow
from partner_agent.infra.models_accounting import ModelCall
from partner_agent.infra.models_stage2 import Run
from partner_agent.infra.settings import Settings
from partner_agent.runtime.accounting import accounting_scope
from partner_agent.runtime.provider import ProviderError, chat_completion

pytestmark = pytest.mark.integration


def settings():
    return Settings(_env_file=None, database_url="postgresql+psycopg://u:p@localhost/db",
                    runtime_profile="openai_compatible", model_endpoint="https://test.invalid/v1",
                    model_name="synthetic", model_api_key="never-publish-me",
                    model_input_usd_per_million=0.1, model_output_usd_per_million=0.4,
                    model_catalog_id="synthetic/catalog", model_provider_variant="synthetic/route",
                    model_price_snapshot="synthetic-2026-11-20")


async def test_call_ledger_export_acl_and_first_render_measurement(plane):
    client, headers, _ = await plane()
    admin, _, _ = await plane("admin")
    chat = await new_chat(client, headers)
    run_id = uuid.UUID((await submit(client, headers, chat)).json()["run_id"])
    path = f"/api/v1/chats/{chat['id']}/accounting"

    def receive(request):
        return httpx.Response(200, json={"id": "resp-synthetic", "choices": [],
                                        "usage": {"prompt_tokens": 10000,
                                                  "completion_tokens": 2000,
                                                  "cost": 0.002,
                                                  "private_text": "not retained"}})

    with accounting_scope(run_id):
        await chat_completion(settings(), [], client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(receive), **kwargs))
    await complete(run_id)
    response = await client.get(path)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["summary"]["estimated_cost_usd"] == "0.0018"
    assert data["summary"]["provider_cost_usd"] == "0.002"
    assert data["accounting_complete"] is True
    assert data["tariff_declaration_complete"] is True
    assert data["timing_complete"] is False
    assert len(data["calls"]) == 1
    assert "private_text" not in response.text and "never-publish-me" not in response.text
    assert (await client.get(path)).json()["calls"] == data["calls"]
    assert (await admin.get(path)).status_code == 404
    assert (await admin.get(path + "/export")).status_code == 404
    exported = await client.get(path + "/export")
    assert exported.json() == data
    assert "attachment" in exported.headers["content-disposition"]
    timing_path = path + f"/turns/{run_id}/rendered"
    assert (await client.post(timing_path, json={"elapsed_ms": 1000})).status_code == 403
    elapsed = data["turns"][0]["server_elapsed_ms"] + 1234
    first = await client.post(timing_path, headers=headers, json={"elapsed_ms": elapsed})
    assert first.status_code == 200, first.text
    repeated = await client.post(timing_path, headers=headers, json={"elapsed_ms": 1})
    assert repeated.json()["elapsed_ms"] == elapsed
    assert (await client.get(path)).json()["mean_elapsed_ms"] == elapsed


@pytest.mark.parametrize("pricing_declared", [True, False])
async def test_expert_json_adds_session_accounting_without_changing_saved_revision(
    plane, pricing_declared,
):
    client, headers, _ = await plane()
    outsider, outsider_headers, _ = await plane("admin")
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}"
    conditions = json.loads(
        (Path(__file__).parents[1] / "fixtures/network-f07.json").read_bytes()
    )
    saved = await client.post(
        base + "/designs", headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"branch_id": chat["default_branch_id"], "input": conditions},
    )
    assert saved.status_code == 201
    revision = saved.json()
    path = base + f"/designs/{revision['id']}/download/json"
    expert_path = path + "?include_accounting=true"
    snapshot = await client.post(path, headers=headers)
    assert snapshot.status_code == 200
    initial = await client.post(expert_path, headers=headers)
    assert initial.json()["usage"]["status"] == "not_measured"
    assert (await outsider.post(expert_path, headers=outsider_headers)).status_code == 404
    assert (await client.post(expert_path)).status_code == 403

    run_id = uuid.UUID((await submit(client, headers, chat)).json()["run_id"])
    pending = (await client.post(expert_path, headers=headers)).json()
    assert pending["usage"]["status"] == "partial"
    assert pending["usage"]["input_tokens"] is None
    assert pending["usage"]["evaluation_cost_usd"] is None

    def receive(request):
        return httpx.Response(200, json={"id": "expert-metering", "choices": [],
                                        "usage": {"prompt_tokens": 10000,
                                                  "completion_tokens": 2000,
                                                  "cost": 0.002}})

    provider_settings = settings()
    if not pricing_declared:
        provider_settings = provider_settings.model_copy(update={
            "model_input_usd_per_million": 0.0, "model_output_usd_per_million": 0.0,
        })
    with accounting_scope(run_id):
        await chat_completion(
            provider_settings, [], client_factory=lambda **kwargs: httpx.AsyncClient(
                transport=httpx.MockTransport(receive), **kwargs,
            ),
        )
    await complete(run_id)
    ledger = (await client.get(base + "/accounting")).json()
    elapsed = ledger["turns"][0]["server_elapsed_ms"] + 1000
    timing_path = base + f"/accounting/turns/{run_id}/rendered"
    assert (await client.post(timing_path, headers=headers,
                              json={"elapsed_ms": elapsed})).status_code == 200
    card_only = (await client.post(expert_path, headers=headers)).json()
    assert card_only["timing"]["mean_turn_seconds"] is None
    full = await client.post(timing_path, headers=headers,
                             json={"elapsed_ms": elapsed, "scope": "full_result"})
    assert full.status_code == 200
    expert = (await client.post(expert_path, headers=headers)).json()
    assert expert["usage"]["status"] == ("measured" if pricing_declared else "partial")
    assert expert["usage"]["input_tokens"] == 10000
    assert expert["usage"]["output_tokens"] == 2000
    assert expert["usage"]["evaluation_cost_usd"] == ("0.0018" if pricing_declared else None)
    assert expert["usage"]["actual_cost_usd"] == "0.002"
    assert expert["usage"]["calls"][0]["turn_id"] == str(run_id)
    assert "raw" not in expert["usage"]["calls"][0]["usage"]
    assert expert["timing"]["mean_turn_seconds"] == elapsed / 1000
    assert expert["revision_id"] == revision["id"]
    assert expert["configuration_sha256"] == revision["artifact_sha256"]["json"]
    assert expert["rules"] == snapshot.json()["rules"]
    assert (await client.post(path, headers=headers)).content == snapshot.content


@pytest.mark.parametrize("failure", ["http", "cancel"])
async def test_failed_and_cancelled_attempts_stay_in_ledger(plane, failure):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    run_id = uuid.UUID((await submit(client, headers, chat)).json()["run_id"])

    async def receive(request):
        if failure == "cancel":
            raise asyncio.CancelledError()
        return httpx.Response(429, json={"usage": {"input_tokens": 10, "output_tokens": 2}})

    with accounting_scope(run_id), pytest.raises(
        asyncio.CancelledError if failure == "cancel" else ProviderError,
    ):
        await chat_completion(settings(), [], client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(receive), **kwargs))
    async with session_factory()() as db:
        rows = (await db.scalars(select(ModelCall).where(ModelCall.run_id == run_id))).all()
        assert len(rows) == 1
        assert rows[0].status == ("cancelled" if failure == "cancel" else "failed")
        assert rows[0].ended_at is not None
    result = (await client.get(f"/api/v1/chats/{chat['id']}/accounting")).json()
    if failure == "http":
        assert result["summary"]["estimated_cost_usd"] == "0.0000018"
    else:
        assert result["summary"]["estimated_cost_usd"] is None
        assert result["summary"]["unknown_cost_calls"] == 1


async def test_historical_cancel_without_profile_is_not_reported_as_free(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    run_id = uuid.UUID((await submit(client, headers, chat)).json()["run_id"])
    await complete(run_id)
    async with session_factory()() as db:
        run = await db.get(Run, run_id)
        run.started_at = utcnow()
        run.outcome = {"code": "cancelled"}
        run.status = "cancelled"
        await db.commit()
    result = (await client.get(f"/api/v1/chats/{chat['id']}/accounting")).json()
    assert result["legacy_untracked_turns"] == 1
    assert result["summary"]["estimated_cost_usd"] is None
    assert result["accounting_complete"] is False
