import json

import pytest

from partner_agent.infra.settings import Settings
from partner_agent.runtime.provider import ProviderError
from partner_agent.runtime.rag_provider import answer_with_provider, decode_model_json

PAYLOAD = '{"answer":"ok","citations":[],"mode":"conversation"}'
USAGE = {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}


@pytest.mark.parametrize(
    "payload",
    [
        '{"answer":"first","answer":"second","citations":[],"mode":"conversation"}',
        '{"answer":"ok","citations":[],"mode":"design","mode":"conversation"}',
        '{"answer":"ok","metadata":{"port":1,"port":2}}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"value":1e999}',
        '{"answer":"\\ud800"}',
        '{"answer":"\\udfff"}',
        '{"\\ud800":"value"}',
        '{"answer":"\\u0000"}',
        "null",
        "[]",
        '"answer"',
        '{"value":' + "[" * 70 + "0" + "]" * 70 + "}",
        '{"value":' + "[" * 2000 + "0" + "]" * 2000 + "}",
        '{"value":' + "9" * 5000 + "}",
    ],
)
def test_untrusted_response_fails_with_safe_error(payload):
    with pytest.raises(ProviderError) as failure:
        decode_model_json(payload)
    assert str(failure.value) == "provider_invalid_rag_json"


def test_valid_unicode_newlines_and_repeated_values_are_preserved():
    text = "Русский ответ\nСтрока\tТаблица 😀"
    raw = json.dumps({"answer": text, "citations": [], "mode": "conversation", "same": [1, 1]})
    decoded = decode_model_json(raw)
    assert decoded["answer"] == text and decoded["same"] == [1, 1]
    assert decode_model_json('{"answer":"\\ud83d\\ude00"}')["answer"] == "😀"


def test_complexity_and_size_are_bounded_even_with_shallow_json():
    with pytest.raises(ProviderError):
        decode_model_json('{"x":[' + ",".join("0" for _ in range(50_001)) + "]}")
    with pytest.raises(ProviderError):
        decode_model_json('{"answer":"' + "a" * 128000 + '"}')


@pytest.mark.parametrize("wire", ["chat_completions", "responses"])
@pytest.mark.parametrize(
    "payload",
    [
        '{"answer":"first","answer":"second","citations":[],"mode":"conversation"}',
        '{"answer":"\\u0000","citations":[],"mode":"conversation"}',
        '{"answer":"ok","nested":' + "[" * 2000 + "0" + "]" * 2000 + "}",
    ],
)
async def test_malformed_chat_and_responses_keep_usage_without_a_real_provider(
    monkeypatch, wire, payload
):
    async def model(settings, messages):
        if wire == "responses":
            return {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": payload}],
                    }
                ],
                "usage": {"input_tokens": 13, "output_tokens": 7, "total_tokens": 20},
            }
        return {"choices": [{"message": {"content": payload}}], "usage": USAGE}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db", model_wire_api=wire)
    with pytest.raises(ProviderError) as failure:
        await answer_with_provider(settings, "Synthetic ordinary conversation", [])
    assert failure.value.code == "provider_invalid_rag_json"
    assert failure.value.usage == USAGE


@pytest.mark.parametrize("wire", ["chat_completions", "responses"])
async def test_normal_answers_still_pass_both_wire_formats(monkeypatch, wire):
    async def model(settings, messages):
        if wire == "responses":
            return {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": PAYLOAD}],
                    }
                ],
                "usage": {"input_tokens": 13, "output_tokens": 7, "total_tokens": 20},
            }
        return {"choices": [{"message": {"content": PAYLOAD}}], "usage": USAGE}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    result = await answer_with_provider(
        Settings(database_url="postgresql+psycopg://u:p@localhost/db", model_wire_api=wire),
        "Synthetic",
        [],
    )
    assert result["answer"] == "ok" and result["answer_mode"] == "conversation"
