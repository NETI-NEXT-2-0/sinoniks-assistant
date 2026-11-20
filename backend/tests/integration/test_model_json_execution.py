"""Real PostgreSQL worker/accounting with a synthetic in-process HTTP transport."""

from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_response_execution import configure_execution

from partner_agent.infra.db import session_factory
from partner_agent.infra.models_accounting import ModelCall
from partner_agent.infra.settings import get_settings
from partner_agent.runtime.provider import chat_completion
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("wire", ["chat_completions", "responses"])
@pytest.mark.parametrize(
    "payload",
    [
        '{"answer":"hidden","answer":"later","citations":[],"mode":"conversation"}',
        '{"answer":"\\u0000","citations":[],"mode":"conversation"}',
        '{"answer":"hidden","nested":' + "[" * 2000 + "0" + "]" * 2000 + "}",
    ],
)
async def test_invalid_model_json_never_publishes_but_keeps_call_cost(
    plane, monkeypatch, wire, payload
):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    monkeypatch.setenv("PARTNER_MODEL_WIRE_API", wire)
    monkeypatch.setenv("PARTNER_MODEL_INPUT_USD_PER_MILLION", "0.1")
    monkeypatch.setenv("PARTNER_MODEL_OUTPUT_USD_PER_MILLION", "0.4")
    monkeypatch.setenv("PARTNER_MODEL_DAILY_CAP_USD", "0")
    monkeypatch.setenv("PARTNER_MODEL_REQUEST_CAP_USD", "0")
    get_settings.cache_clear()
    requests = []

    def receive(request):
        requests.append(request.url.path)
        if wire == "responses":
            return httpx.Response(
                200,
                json={
                    "id": "synthetic-response",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": payload}],
                        }
                    ],
                    "usage": {"input_tokens": 13, "output_tokens": 7, "total_tokens": 20},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "synthetic-response",
                "choices": [{"message": {"content": payload}}],
                "usage": {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20},
            },
        )

    async def completion(settings, messages):
        return await chat_completion(
            settings,
            messages,
            client_factory=lambda **kwargs: httpx.AsyncClient(
                transport=httpx.MockTransport(receive), **kwargs
            ),
        )

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", completion)
    chat = await new_chat(client, headers)
    admitted = await submit(client, headers, chat, text="Ordinary synthetic conversation")
    assert admitted.status_code == 202
    run_id = UUID(admitted.json()["run_id"])
    result = await execute_run(run_id, dispatch_generation=0)
    assert result["status"] == "failed"
    assert len(requests) == 1
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    assert not any(message["role"] == "assistant" for message in snapshot["messages"])
    outcome = snapshot["runs"][0]["outcome"]
    assert outcome["code"] == "provider_invalid_rag_json"
    assert outcome["usage"] == {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}
    async with session_factory()() as db:
        calls = list((await db.scalars(select(ModelCall).where(ModelCall.run_id == run_id))).all())
        assert len(calls) == 1
        assert calls[0].status == "succeeded"  # HTTP succeeded, answer validation did not.
    ledger = (await client.get(f"/api/v1/chats/{chat['id']}/accounting")).json()
    assert ledger["summary"]["estimated_cost_usd"] == "0.0000041"
    assert ledger["accounting_complete"]
