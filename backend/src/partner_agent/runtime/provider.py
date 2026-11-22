"""Bounded provider boundary for real model execution."""

import asyncio
import json
import zlib
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from decimal import ROUND_CEILING, Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from partner_agent.infra.settings import ROUTER_ENDPOINT, Settings
from partner_agent.runtime.accounting import track_model_call
from partner_agent.runtime.output_schema import current_output_schema

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RESPONSE_CHUNK_BYTES = 64 * 1024


async def bounded_response_body(response: httpx.Response) -> bytes:
    """Bound both raw wire bytes and decompressed JSON, without eager HTTPX decoding."""
    length = response.headers.get("content-length")
    if length and length.isascii() and length.isdecimal() and int(length) > MAX_RESPONSE_BYTES:
        raise ProviderError("provider_response_too_large")
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity", "gzip", "deflate"}:
        raise ProviderError("provider_response_encoding")
    if response.is_stream_consumed:
        # Mock/custom transports may provide an already buffered, decoded response.
        # The production network path below reads raw bytes before allocating a full body.
        try:
            buffered = response.content
        except httpx.ResponseNotRead as exc:
            raise ProviderError("provider_request_failed") from exc
        if len(buffered) > MAX_RESPONSE_BYTES or response.num_bytes_downloaded > MAX_RESPONSE_BYTES:
            raise ProviderError("provider_response_too_large")
        return buffered
    decoder = (
        zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
        if encoding in {"gzip", "deflate"} else None
    )
    body = bytearray()
    received = 0
    async for chunk in response.aiter_raw(chunk_size=RESPONSE_CHUNK_BYTES):
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise ProviderError("provider_response_too_large")
        # max_length prevents a small compressed chunk from allocating an unbounded result.
        decoded = (
            decoder.decompress(chunk, MAX_RESPONSE_BYTES - len(body) + 1) if decoder else chunk
        )
        if len(body) + len(decoded) > MAX_RESPONSE_BYTES:
            raise ProviderError("provider_response_too_large")
        body.extend(decoded)
    if decoder is not None and (not decoder.eof or decoder.unused_data):
        raise ProviderError("provider_request_failed")
    return bytes(body)


class ProviderError(RuntimeError):
    def __init__(self, code: str, *, usage: dict[str, int] | None = None,
                 diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.usage = usage
        self.diagnostics = diagnostics


def normalize_usage(raw: Any) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProviderError("provider_invalid_usage")
    result: dict[str, int] = {}
    for target, aliases in (
        ("prompt_tokens", ("prompt_tokens", "input_tokens")),
        ("completion_tokens", ("completion_tokens", "output_tokens")),
        ("total_tokens", ("total_tokens",)),
    ):
        values = [raw[key] for key in aliases if key in raw]
        if any(type(value) is not int or value < 0 for value in values):
            raise ProviderError("provider_invalid_usage")
        if values:
            if len(set(values)) != 1:
                raise ProviderError("provider_invalid_usage")
            result[target] = values[0]
    return result


def estimate_cost_usd(settings: Settings, usage: dict[str, int]) -> float | None:
    """Calculate a non-negative estimate from provider usage and configured tariff."""
    usage = normalize_usage(usage)
    if "prompt_tokens" not in usage or "completion_tokens" not in usage:
        return None
    prompt = usage["prompt_tokens"]
    completion = usage["completion_tokens"]
    return round(
        prompt * settings.model_input_usd_per_million / 1_000_000
        + completion * settings.model_output_usd_per_million / 1_000_000,
        8,
    )


def enforce_request_cap(settings: Settings, estimated_cost_usd: float) -> None:
    if settings.model_request_cap_usd and estimated_cost_usd > settings.model_request_cap_usd:
        raise ProviderError("provider_request_cap_exceeded")


def reservation_cost_usd(settings: Settings) -> float:
    context = settings.model_context_window_tokens
    if settings.model_contest_mode and context:
        inbound = context * Decimal(str(settings.model_input_usd_per_million))
        outbound = settings.model_max_output_tokens * Decimal(
            str(settings.model_output_usd_per_million)
        )
        return float((inbound + outbound).to_integral_value(rounding=ROUND_CEILING) / 1_000_000)
    estimate = estimate_cost_usd(settings, {
        "prompt_tokens": 4000, "completion_tokens": settings.model_max_output_tokens,
    })
    assert estimate is not None
    return estimate


def enforce_daily_cap(settings: Settings, spent_usd: float, estimated_cost_usd: float) -> None:
    if settings.model_daily_cap_usd and (
        spent_usd + estimated_cost_usd > settings.model_daily_cap_usd
    ):
        raise ProviderError("provider_daily_cap_exceeded")


def outcome_metadata(settings: Settings, usage: dict[str, int]) -> dict[str, Any]:
    cost = estimate_cost_usd(settings, usage)
    if cost is not None:
        enforce_request_cap(settings, cost)
    return {
        "profile": settings.runtime_profile,
        "model": (settings.model_name if settings.runtime_profile != "offline_fixture"
                  else "offline_fixture"),
        "usage": usage,
        "estimated_cost_usd": cost,
        "usage_complete": cost is not None,
    }


def parse_stream_event(line: str) -> str | None:
    """Parse one OpenAI SSE data line, returning a bounded text delta."""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return None
    try:
        body = httpx.Response(200, content=payload).json()
    except ValueError as exc:
        raise ProviderError("provider_invalid_stream") from exc
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError("provider_invalid_stream")
    delta = choices[0].get("delta") or {}
    content = delta.get("content") if isinstance(delta, dict) else None
    if content is not None and not isinstance(content, str):
        raise ProviderError("provider_invalid_stream")
    return content


def parse_responses_stream_event(line: str) -> str | None:
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return None
    try:
        body = httpx.Response(200, content=payload).json()
    except ValueError as exc:
        raise ProviderError("provider_invalid_stream") from exc
    if not isinstance(body, dict):
        raise ProviderError("provider_invalid_stream")
    event_type = body.get("type")
    if event_type in ("response.output_text.delta", "response.text.delta"):
        delta = body.get("delta")
        if not isinstance(delta, str):
            raise ProviderError("provider_invalid_stream")
        return delta
    if event_type in ("response.completed", "response.done"):
        return None
    return None


async def stream_completion_lines(
    lines: AsyncIterator[str], *, parser: Callable[[str], str | None] = parse_stream_event
) -> AsyncIterator[str]:
    """Yield text deltas from bounded SSE input; callers own cancellation."""
    total = 0
    async for line in lines:
        if len(line) > 100000:
            raise ProviderError("provider_invalid_stream")
        delta = parser(line.rstrip("\r\n"))
        if delta:
            total += len(delta)
            if total > 12000:
                raise ProviderError("provider_invalid_stream")
            yield delta


def capability_report(settings: Settings) -> dict[str, Any]:
    """Return configuration-only capabilities without contacting a provider."""
    if settings.runtime_profile == "offline_fixture":
        return {"profile": "offline_fixture", "enabled": False, "reason": "offline_profile"}
    return {
        "profile": settings.runtime_profile,
        "enabled": True,
        "model": settings.model_name,
        "chat": True,
        "stream": False,
        "usage": True,
    }


def completion_result(
    body: dict[str, Any], *, max_chars: int = 12000,
) -> tuple[str, dict[str, int]]:
    """Extract bounded assistant text and usage without exposing provider payloads."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError("provider_invalid_response")
    if choices[0].get("finish_reason") in ("length", "content_filter"):
        raise ProviderError("provider_incomplete_response")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ProviderError("provider_invalid_response")
    content = message["content"].strip()
    if not content or len(content) > max_chars:
        raise ProviderError("provider_invalid_response")
    return content, normalize_usage(body.get("usage"))


def responses_result(
    body: dict[str, Any], *, max_chars: int = 12000,
) -> tuple[str, dict[str, int]]:
    """Normalize an OpenAI Responses API JSON result."""
    if body.get("status") in ("incomplete", "failed", "cancelled"):
        raise ProviderError("provider_incomplete_response")
    output = body.get("output")
    if not isinstance(output, list):
        raise ProviderError("provider_invalid_response")
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    text = "".join(parts).strip()
    if not text or len(text) > max_chars:
        raise ProviderError("provider_invalid_response")
    return text, normalize_usage(body.get("usage"))


async def capability_probe(
    settings: Settings,
    *, client_factory: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]] | None = None,
) -> dict[str, Any]:
    """Explicitly probe a configured provider; never called during startup."""
    if settings.runtime_profile != "openai_compatible":
        raise ProviderError("provider_disabled_in_offline_profile")
    factory = client_factory or (lambda **kwargs: httpx.AsyncClient(**kwargs))
    try:
        async with factory(timeout=settings.model_timeout_seconds) as client:
            response = await client.get(
                settings.model_endpoint.rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {settings.model_api_key.get_secret_value()}"},
            )
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError("provider_probe_failed") from exc
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise ProviderError("provider_invalid_probe")
    return {"profile": settings.runtime_profile, "model": settings.model_name, "reachable": True}


async def chat_completion(
    settings: Settings, messages: list[dict[str, str]],
    *, client_factory: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]] | None = None,
) -> dict[str, Any]:
    """Call an OpenAI-compatible endpoint with bounded, redacted transport."""
    if settings.runtime_profile != "openai_compatible":
        raise ProviderError("provider_disabled_in_offline_profile")
    path = "/responses" if settings.model_wire_api == "responses" else "/chat/completions"
    endpoint = settings.model_endpoint.rstrip("/") + path
    payload = {"model": settings.model_name, "input": messages, "stream": False}
    payload["max_output_tokens"] = settings.model_max_output_tokens
    if settings.model_wire_api == "chat_completions":
        payload = {"model": settings.model_name, "messages": messages, "stream": False,
                   "max_tokens": settings.model_max_output_tokens}
    if settings.model_reasoning_effort is not None:
        # Polza silently ignores OpenAI's top-level reasoning_effort.
        # Its unified API requires reasoning.effort on both wire formats.
        polza = urlsplit(settings.model_endpoint).hostname in {"polza.ai", "api.polza.ai"}
        if settings.model_wire_api == "chat_completions" and not polza:
            payload["reasoning_effort"] = settings.model_reasoning_effort
        else:
            payload["reasoning"] = {"effort": settings.model_reasoning_effort}
    schema = current_output_schema() if settings.model_structured_design_output else None
    if schema is not None:
        output_format = {"type": "json_schema", "name": "sinoniks_new_project", "strict": True,
                         "schema": schema}
        if settings.model_wire_api == "chat_completions":
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                key: value for key, value in output_format.items() if key != "type"
            }}
        else:
            payload["text"] = {"format": output_format}
    elif settings.model_json_output:
        if settings.model_wire_api == "chat_completions":
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["text"] = {"format": {"type": "json_object"}}
    if settings.model_contest_mode and settings.model_endpoint.rstrip("/") == ROUTER_ENDPOINT:
        payload["provider"] = {
            "order": [settings.model_provider_variant],
            "only": [settings.model_provider_variant],
            "allow_fallbacks": False,
            "max_price": {
                "prompt": settings.model_input_usd_per_million,
                "completion": settings.model_output_usd_per_million,
                "request": 0,
            },
        }
    if settings.model_endpoint.rstrip("/") == "https://polza.ai/api/v1":
        if not settings.model_provider_variant or settings.model_wire_api != "chat_completions":
            raise ProviderError("provider_route_required")
        payload.setdefault("response_format", {"type": "json_object"})
        payload["provider"] = {
            "only": [settings.model_provider_variant],
            "allow_fallbacks": False,
        }
    async with track_model_call(settings) as captured:
        body: Any = None
        try:
            factory = client_factory or (lambda **kwargs: httpx.AsyncClient(**kwargs))
            async with asyncio.timeout(settings.model_timeout_seconds):
                async with factory(timeout=settings.model_timeout_seconds) as client:
                    async with client.stream(
                        "POST", endpoint,
                        headers={"Authorization":
                                 f"Bearer {settings.model_api_key.get_secret_value()}",
                                 "Accept-Encoding": "identity, gzip, deflate"},
                        json=payload,
                    ) as response:
                        raw = await bounded_response_body(response)
                        body = json.loads(raw)
                        captured.body = body
                        response.raise_for_status()
        except TimeoutError as exc:
            raise ProviderError("provider_request_timeout") from exc
        except (httpx.HTTPError, ValueError, RecursionError, zlib.error) as exc:
            usage = normalize_usage(body.get("usage")) if isinstance(body, dict) else None
            raise ProviderError("provider_request_failed", usage=usage) from exc
        if not isinstance(body, dict):
            raise ProviderError("provider_invalid_response")
        if (
            settings.model_wire_api == "chat_completions"
            and not isinstance(body.get("choices"), list)
        ):
            raise ProviderError("provider_invalid_response",
                                usage=normalize_usage(body.get("usage")))
        return body


async def stream_completion(
    settings: Settings, messages: list[dict[str, str]],
    *, client_factory: Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]] | None = None,
) -> AsyncIterator[str]:
    """Open an explicit SSE completion stream; caller controls cancellation."""
    if settings.runtime_profile != "openai_compatible":
        raise ProviderError("provider_disabled_in_offline_profile")
    if settings.model_contest_mode:
        raise ProviderError("provider_contest_requires_metered_completion")
    path = "/responses" if settings.model_wire_api == "responses" else "/chat/completions"
    endpoint = settings.model_endpoint.rstrip("/") + path
    payload = {"model": settings.model_name, "input": messages, "stream": True}
    parser = (
        parse_responses_stream_event
        if settings.model_wire_api == "responses"
        else parse_stream_event
    )
    if settings.model_wire_api == "chat_completions":
        payload = {"model": settings.model_name, "messages": messages, "stream": True}
    factory = client_factory or (lambda **kwargs: httpx.AsyncClient(**kwargs))
    try:
        async with factory(timeout=settings.model_timeout_seconds) as client:
            async with client.stream(
                "POST", endpoint,
                headers={"Authorization": f"Bearer {settings.model_api_key.get_secret_value()}"},
                json=payload,
            ) as response:
                response.raise_for_status()
                async for delta in stream_completion_lines(response.aiter_lines(), parser=parser):
                    yield delta
    except ProviderError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError("provider_stream_failed") from exc


async def _one_line(line: str) -> AsyncIterator[str]:
    yield line

def structured_result(body: dict[str, Any]) -> tuple[str, list[dict[str, str]], dict[str, int]]:
    """Validate a provider JSON answer with bounded citation records."""
    text, usage = completion_result(body)
    raw = body.get("citations", [])
    if not isinstance(raw, list) or len(raw) > 50:
        raise ProviderError("provider_invalid_citations")
    citations: list[dict[str, str]] = []
    for item in raw:
        if (not isinstance(item, dict) or not isinstance(item.get("source"), str)
            or not isinstance(item.get("quote"), str)):
            raise ProviderError("provider_invalid_citations")
        if len(item["source"]) > 500 or len(item["quote"]) > 1600:
            raise ProviderError("provider_invalid_citations")
        citations.append({"source": item["source"], "quote": item["quote"]})
    return text, citations, usage
