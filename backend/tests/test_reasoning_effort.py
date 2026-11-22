import json

import httpx
import pytest
from pydantic import ValidationError

from partner_agent.infra.settings import Settings
from partner_agent.runtime.accounting import tariff_snapshot
from partner_agent.runtime.provider import ProviderError, chat_completion


@pytest.mark.parametrize("wire", ["chat_completions", "responses"])
@pytest.mark.parametrize("effort", [None, "none", "low"])
@pytest.mark.parametrize("endpoint", ["https://test.invalid/v1", "https://polza.ai/api/v1",
                                      "https://api.polza.ai/v1", "https://polza.ai.invalid/v1"])
async def test_explicit_reasoning_effort_preserves_wire_format_and_ledger(wire, effort, endpoint):
    calls = []

    def receive(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if effort is None:
            assert "reasoning" not in payload and "reasoning_effort" not in payload
        elif wire == "chat_completions" and endpoint in {
            "https://test.invalid/v1", "https://polza.ai.invalid/v1",
        }:
            assert payload["reasoning_effort"] == effort and "reasoning" not in payload
        else:
            assert payload["reasoning"] == {"effort": effort}
            assert "reasoning_effort" not in payload
        cap = "max_tokens" if wire == "chat_completions" else "max_output_tokens"
        assert payload[cap] == 6000
        return httpx.Response(200, json={"choices": []})

    settings = Settings(
        _env_file=None, database_url="postgresql+psycopg://localhost/test",
        runtime_profile="openai_compatible", model_endpoint=endpoint,
        model_name="synthetic", model_api_key="synthetic", model_wire_api=wire,
        model_max_output_tokens=6000, model_reasoning_effort=effort,
        model_provider_variant="openai",
    )
    if endpoint == "https://polza.ai/api/v1" and wire == "responses":
        with pytest.raises(ProviderError, match="provider_route_required"):
            await chat_completion(settings, [])
        assert not calls
        return
    await chat_completion(settings, [], client_factory=lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(receive), **kwargs,
    ))
    assert len(calls) == 1  # Tuning never adds a hidden retry.
    assert tariff_snapshot(settings)["reasoning_effort"] == effort
    assert tariff_snapshot(settings)["max_output_tokens"] == 6000


def test_blank_compose_reasoning_value_preserves_provider_default():
    settings = Settings(
        _env_file=None, database_url="postgresql+psycopg://localhost/test",
        model_reasoning_effort="",
    )
    assert settings.model_reasoning_effort is None
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql+psycopg://localhost/test",
                 model_reasoning_effort="fast")


@pytest.mark.parametrize('wire', ['chat_completions', 'responses'])
@pytest.mark.parametrize('enabled', [False, True])
async def test_json_output_is_opt_in_and_preserves_wire_contract(wire, enabled):
    calls = []

    def receive(request):
        body = json.loads(request.content)
        calls.append(body)
        if enabled and wire == 'chat_completions':
            assert body['response_format'] == {'type': 'json_object'} and 'text' not in body
        elif enabled:
            assert body['text'] == {'format': {'type': 'json_object'}}
            assert 'response_format' not in body
        else:
            assert 'response_format' not in body and 'text' not in body
        return httpx.Response(200, json={'choices': []})

    settings = Settings(_env_file=None, database_url='postgresql+psycopg://localhost/test',
                        runtime_profile='openai_compatible', model_endpoint='https://test.invalid/v1',
                        model_name='synthetic', model_api_key='synthetic', model_wire_api=wire,
                        model_json_output=enabled)
    await chat_completion(settings, [{'role': 'system', 'content': 'Return JSON'}],
                          client_factory=lambda **kw: httpx.AsyncClient(
                              transport=httpx.MockTransport(receive), **kw))
    assert len(calls) == 1 and tariff_snapshot(settings)['json_output'] is enabled
