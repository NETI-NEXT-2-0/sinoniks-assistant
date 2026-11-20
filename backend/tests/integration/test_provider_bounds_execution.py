"""No paid requests: synthetic streamed HTTP, real worker and PostgreSQL ledger."""

import asyncio
import gzip
import json
from uuid import UUID

import httpx
import pytest
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_response_execution import configure_execution

from partner_agent.infra.settings import get_settings
from partner_agent.runtime.provider import chat_completion
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


class Stream(httpx.AsyncByteStream):
    def __init__(self, failure):
        self.failure = failure
        self.closed = False
        self.emitted = 0

    async def __aiter__(self):
        if self.failure == "trickle":
            for _ in range(100):
                await asyncio.sleep(0.02)
                self.emitted += 1
                yield b" "
        elif self.failure == "expansion":
            self.emitted += 1
            yield gzip.compress(b"x" * 200_000)
        elif self.failure == "http":
            self.emitted += 1
            yield json.dumps(
                {
                    "error": "synthetic private detail",
                    "usage": {
                        "prompt_tokens": 13,
                        "completion_tokens": 7,
                        "total_tokens": 20,
                    },
                }
            ).encode()
        else:
            self.emitted += 1
            yield b"not read"

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("wire", ["chat_completions", "responses"])
@pytest.mark.parametrize("failure", ["length", "trickle", "expansion", "http"])
async def test_transport_failure_is_durable_and_unknown_cost_is_not_zero(
    plane, monkeypatch, wire, failure
):
    from partner_agent.runtime import provider

    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    for key, value in {
        "PARTNER_MODEL_WIRE_API": wire,
        "PARTNER_MODEL_TIMEOUT_SECONDS": ".07" if failure == "trickle" else "45",
        "PARTNER_MODEL_INPUT_USD_PER_MILLION": ".1",
        "PARTNER_MODEL_OUTPUT_USD_PER_MILLION": ".4",
        "PARTNER_MODEL_DAILY_CAP_USD": "0",
        "PARTNER_MODEL_REQUEST_CAP_USD": "0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(provider, "MAX_RESPONSE_BYTES", 1024)
    get_settings.cache_clear()
    stream = Stream(failure)
    attempts = []

    def receive(request):
        attempts.append(request.url.path)
        response_headers = (
            {"Content-Length": "2048"}
            if failure == "length"
            else {"Content-Encoding": "gzip"}
            if failure == "expansion"
            else {}
        )
        return httpx.Response(
            429 if failure == "http" else 200, headers=response_headers, stream=stream
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
    submitted = await submit(client, headers, chat, text="Synthetic ordinary conversation")
    assert submitted.status_code == 202
    run_id = UUID(submitted.json()["run_id"])
    assert (await execute_run(run_id, dispatch_generation=0))["status"] == "failed"
    assert len(attempts) == 1 and stream.closed
    if failure == "length":
        assert stream.emitted == 0
    if failure == "trickle":
        assert stream.emitted < 100
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    assert not any(message["role"] == "assistant" for message in snapshot["messages"])
    expected = {
        "length": "provider_response_too_large",
        "expansion": "provider_response_too_large",
        "trickle": "provider_request_timeout",
        "http": "provider_request_failed",
    }[failure]
    assert snapshot["runs"][0]["outcome"]["code"] == expected
    ledger_response = await client.get(f"/api/v1/chats/{chat['id']}/accounting")
    assert "synthetic private detail" not in ledger_response.text
    ledger = ledger_response.json()
    assert len(ledger["calls"]) == 1 and ledger["calls"][0]["status"] == "failed"
    assert ledger["calls"][0]["error_code"] == expected
    if failure == "http":
        assert ledger["summary"]["estimated_cost_usd"] == "0.0000041"
        assert ledger["accounting_complete"]
        assert snapshot["runs"][0]["outcome"]["usage"] == {
            "prompt_tokens": 13,
            "completion_tokens": 7,
            "total_tokens": 20,
        }
    else:
        assert ledger["summary"]["estimated_cost_usd"] is None
        assert not ledger["accounting_complete"]
