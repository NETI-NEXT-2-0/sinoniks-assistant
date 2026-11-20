"""Real PostgreSQL, synthetic HTTP provider: no paid API requests."""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select
from test_chat_data_plane import complete, new_chat, submit
from test_chat_data_plane import plane as plane

from partner_agent.infra.db import session_factory
from partner_agent.infra.models_accounting import ModelCall
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
