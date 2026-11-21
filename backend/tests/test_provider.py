import json

import httpx
import pytest

from partner_agent.infra.settings import Settings
from partner_agent.runtime.provider import (
    ProviderError,
    capability_report,
    completion_result,
    enforce_request_cap,
    estimate_cost_usd,
    normalize_usage,
    responses_result,
    stream_completion,
)


@pytest.mark.parametrize("wire", ["responses", "chat_completions"])
async def test_completion_transport_sends_output_limit(wire):
    from partner_agent.runtime.provider import chat_completion

    def receive(request):
        body = json.loads(request.content)
        field = "max_output_tokens" if wire == "responses" else "max_tokens"
        assert body[field] == 512
        return httpx.Response(200, json={"choices": []})

    settings = Settings(
        database_url="postgresql+psycopg://u:p@localhost/db",
        runtime_profile="openai_compatible", model_endpoint="https://test.invalid/v1",
        model_name="synthetic", model_api_key="synthetic", model_wire_api=wire,
        model_max_output_tokens=512,
    )
    await chat_completion(settings, [{"role": "user", "content": "test"}],
                          client_factory=lambda **kwargs: httpx.AsyncClient(
                              transport=httpx.MockTransport(receive), **kwargs))


def test_provider_rejects_truncated_json_even_when_parseable():
    with pytest.raises(ProviderError, match="provider_incomplete_response"):
        completion_result({"choices": [{"finish_reason": "length",
                                         "message": {"content": '{"design":{}}'}}]})
    with pytest.raises(ProviderError, match="provider_incomplete_response"):
        responses_result({"status": "incomplete", "output": [
            {"content": [{"type": "output_text", "text": '{"design":{}}'}]},
        ]})


async def test_provider_transport_failure_preserves_safe_code():
    import httpx

    from partner_agent.runtime.provider import chat_completion

    sentinel = "private_provider_transport_detail"

    def fail(request):
        raise httpx.ConnectTimeout(sentinel, request=request)

    settings = Settings(
        database_url="postgresql+psycopg://u:p@localhost/db",
        runtime_profile="openai_compatible",
        model_endpoint="https://provider.invalid/v1",
        model_name="test-model",
        model_api_key="test-key",
    )
    with pytest.raises(ProviderError) as caught:
        await chat_completion(
            settings, [{"role": "user", "content": "test"}],
            client_factory=lambda **kwargs: httpx.AsyncClient(
                transport=httpx.MockTransport(fail), **kwargs
            ),
        )
    assert caught.value.code == "provider_request_failed"
    assert sentinel not in str(caught.value)


def test_capability_report_is_explicit_for_offline_profile():
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    assert capability_report(settings) == {
        "profile": "offline_fixture", "enabled": False, "reason": "offline_profile"
    }


def test_usage_cost_and_request_cap_are_bounded():
    settings = Settings(
        database_url="postgresql+psycopg://u:p@localhost/db",
        model_input_usd_per_million=2.0, model_output_usd_per_million=4.0,
        model_request_cap_usd=0.00001,
    )
    cost = estimate_cost_usd(settings, {"prompt_tokens": 1000, "completion_tokens": 1000})
    assert cost == 0.006
    with pytest.raises(ProviderError, match="provider_request_cap_exceeded"):
        enforce_request_cap(settings, cost)


def test_completion_result_extracts_text_and_usage():
    text, usage = completion_result({
        "choices": [{"message": {"content": " Answer "}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    })
    assert text == "Answer"
    assert usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": "x" * 12001}}]},
        {"choices": [{"message": {"content": 1}}]},
    ],
)
def test_completion_result_rejects_untrusted_shapes(body):
    with pytest.raises(ProviderError, match="provider_invalid_response"):
        completion_result(body)

@pytest.mark.parametrize(
    ("line", "expected"),
    [("data: {\"choices\":[{\"delta\":{\"content\":\"Hi\"}}]}", "Hi"),
     ("data: [DONE]", None), (": keepalive", None)],
)
def test_parse_stream_event(line, expected):
    from partner_agent.runtime.provider import parse_stream_event
    assert parse_stream_event(line) == expected


def test_daily_cap_and_outcome_metadata():
    from partner_agent.runtime.provider import enforce_daily_cap, outcome_metadata
    settings = Settings(
        database_url="postgresql+psycopg://u:p@localhost/db", model_daily_cap_usd=0.01
    )
    with pytest.raises(ProviderError, match="provider_daily_cap_exceeded"):
        enforce_daily_cap(settings, 0.009, 0.002)
    metadata = outcome_metadata(settings, {"prompt_tokens": 0, "completion_tokens": 0})
    assert metadata["profile"] == "offline_fixture" and metadata["estimated_cost_usd"] == 0.0

def test_structured_result_bounds_citations():
    from partner_agent.runtime.provider import structured_result
    body = {"choices": [{"message": {"content": "ok"}}],
            "citations": [{"source": "doc", "quote": "q"}]}
    text, citations, usage = structured_result(body)
    assert text == "ok" and citations == [{"source": "doc", "quote": "q"}] and usage == {}

def test_parse_responses_stream_events():
    from partner_agent.runtime.provider import parse_responses_stream_event
    line = 'data: {"type":"response.output_text.delta","delta":"Hi"}'
    assert parse_responses_stream_event(line) == "Hi"
    assert parse_responses_stream_event('data: {"type":"response.completed"}') is None


def test_responses_usage_preserves_billable_input_and_output():
    _, usage = responses_result({
        "output": [{"content": [{"type": "output_text", "text": "ok"}]}],
        "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
    })
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db",
                        model_input_usd_per_million=2, model_output_usd_per_million=4)
    assert usage == {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}
    assert estimate_cost_usd(settings, usage) == 0.004


@pytest.mark.parametrize("raw", [[], {"input_tokens": True}, {"completion_tokens": -1},
    {"prompt_tokens": 1.5}, {"input_tokens": 1, "prompt_tokens": 2}])
def test_invalid_usage_is_rejected(raw):
    with pytest.raises(ProviderError, match="provider_invalid_usage"):
        normalize_usage(raw)


@pytest.mark.parametrize("usage", [{}, {"total_tokens": 7}, {"prompt_tokens": 4}])
def test_incomplete_usage_is_unknown_rather_than_free(usage):
    from partner_agent.runtime.provider import outcome_metadata

    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    assert estimate_cost_usd(settings, usage) is None
    metadata = outcome_metadata(settings, usage)
    assert metadata["estimated_cost_usd"] is None and not metadata["usage_complete"]


def test_safe_error_code_is_available_to_execution():
    error = ProviderError("provider_invalid_usage")
    assert error.code == "provider_invalid_usage"


@pytest.mark.parametrize("wire", ["chat_completions", "responses"])
@pytest.mark.parametrize("length,accepted", [(12000, True), (12001, False)])
async def test_http_stream_enforces_total_limit_and_closes_transport(wire, length, accepted):
    closed = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for size in (6000, length - 6000):
                event = ({"type": "response.output_text.delta", "delta": "x" * size}
                         if wire == "responses"
                         else {"choices": [{"delta": {"content": "x" * size}}]})
                yield ("data: " + json.dumps(event) + "\n\n").encode()

        async def aclose(self):
            closed.append(True)

    def handler(request):
        assert request.url.path.endswith(
            "/responses" if wire == "responses" else "/chat/completions"
        )
        return httpx.Response(200, stream=Body())

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db",
        runtime_profile="openai_compatible", model_endpoint="https://test.invalid/v1",
        model_name="synthetic", model_api_key="synthetic", model_wire_api=wire)
    chunks = []
    if accepted:
        chunks = [part async for part in stream_completion(settings, [], client_factory=factory)]
        assert len("".join(chunks)) == length
    else:
        with pytest.raises(ProviderError, match="provider_invalid_stream"):
            async for part in stream_completion(settings, [], client_factory=factory):
                chunks.append(part)
        assert len("".join(chunks)) == 6000
    assert closed == [True]


@pytest.mark.parametrize("route", ["openai/flex", ""])
async def test_polza_pins_declared_provider_without_fallback(route):
    from partner_agent.runtime.provider import chat_completion

    calls = []

    def receive(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["model"] == "openai/gpt-6-luna"
        assert body["provider"] == {"only": ["openai/flex"], "allow_fallbacks": False}
        assert "max_price" not in body["provider"]  # Polza pricing is in RUB.
        return httpx.Response(200, json={"choices": []})

    settings = Settings(
        _env_file=None, database_url="postgresql+psycopg://u:p@localhost/db",
        runtime_profile="openai_compatible", model_endpoint="https://polza.ai/api/v1",
        model_name="openai/gpt-6-luna", model_api_key="synthetic",
        model_provider_variant=route,
    )
    if not route:
        with pytest.raises(ProviderError, match="provider_route_required"):
            await chat_completion(settings, [], client_factory=lambda **kwargs: httpx.AsyncClient(
                transport=httpx.MockTransport(receive), **kwargs))
        assert not calls
    else:
        await chat_completion(settings, [], client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(receive), **kwargs))
        assert len(calls) == 1
