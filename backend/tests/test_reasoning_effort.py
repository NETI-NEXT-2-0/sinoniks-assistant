import json

import httpx
import pytest
from pydantic import ValidationError

from partner_agent.infra.settings import Settings
from partner_agent.runtime.accounting import tariff_snapshot
from partner_agent.runtime.provider import chat_completion


@pytest.mark.parametrize("wire", ["chat_completions", "responses"])
@pytest.mark.parametrize("effort", [None, "none", "low"])
async def test_explicit_reasoning_effort_preserves_wire_format_and_ledger(wire, effort):
    calls = []

    def receive(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if effort is None:
            assert "reasoning" not in payload and "reasoning_effort" not in payload
        elif wire == "chat_completions":
            assert payload["reasoning_effort"] == effort and "reasoning" not in payload
        else:
            assert payload["reasoning"] == {"effort": effort}
            assert "reasoning_effort" not in payload
        cap = "max_tokens" if wire == "chat_completions" else "max_output_tokens"
        assert payload[cap] == 6000
        return httpx.Response(200, json={"choices": []})

    settings = Settings(
        _env_file=None, database_url="postgresql+psycopg://localhost/test",
        runtime_profile="openai_compatible", model_endpoint="https://test.invalid/v1",
        model_name="synthetic", model_api_key="synthetic", model_wire_api=wire,
        model_max_output_tokens=6000, model_reasoning_effort=effort,
    )
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
