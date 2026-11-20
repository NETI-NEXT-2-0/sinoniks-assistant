import asyncio
import gzip
import json
import zlib

import httpx
import pytest
from pydantic import ValidationError

from partner_agent.infra.settings import Settings
from partner_agent.runtime.provider import ProviderError, chat_completion


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.emitted = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.emitted += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def settings(**changes):
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/db",
        runtime_profile="openai_compatible",
        model_endpoint="https://fixture.invalid/v1",
        model_name="synthetic",
        model_api_key="synthetic",
        **changes,
    )


def factory(stream, *, status=200, headers=None, request_check=None):
    def receive(request):
        if request_check:
            request_check(request)
        return httpx.Response(status, headers=headers, stream=stream)

    return lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(receive), **kwargs)


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
@pytest.mark.parametrize("wire", ["chat_completions", "responses"])
async def test_valid_bounded_wire_formats_and_common_compression(encoding, wire):
    payload = {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}}
    raw = json.dumps(payload).encode()
    encoded = (
        gzip.compress(raw)
        if encoding == "gzip"
        else zlib.compress(raw)
        if encoding == "deflate"
        else raw
    )
    stream = Chunks([encoded[i : i + 7] for i in range(0, len(encoded), 7)])

    def check(request):
        sent = json.loads(request.content)
        assert sent["stream"] is False
        assert request.headers["accept-encoding"] == "identity, gzip, deflate"
        assert request.url.path == (
            "/v1/responses" if wire == "responses" else "/v1/chat/completions"
        )

    result = await chat_completion(
        settings(model_wire_api=wire),
        [],
        client_factory=factory(
            stream,
            headers={"Content-Encoding": encoding},
            request_check=check,
        ),
    )
    assert result == payload and stream.closed


async def test_exact_body_limit_succeeds_and_one_more_byte_fails(monkeypatch):
    from partner_agent.runtime import provider

    raw = b'{"choices":[]}'
    monkeypatch.setattr(provider, "MAX_RESPONSE_BYTES", len(raw))
    stream = Chunks([raw])
    assert await chat_completion(settings(), [], client_factory=factory(stream)) == {"choices": []}
    stream = Chunks([raw + b" "])
    with pytest.raises(ProviderError, match="provider_response_too_large"):
        await chat_completion(settings(), [], client_factory=factory(stream))
    assert stream.closed


async def test_declared_oversize_is_rejected_without_reading_body(monkeypatch):
    from partner_agent.runtime import provider

    monkeypatch.setattr(provider, "MAX_RESPONSE_BYTES", 128)
    stream = Chunks([b"private response text"] * 100)
    with pytest.raises(ProviderError) as caught:
        await chat_completion(
            settings(),
            [],
            client_factory=factory(
                stream,
                headers={"Content-Length": "129"},
            ),
        )
    assert caught.value.code == "provider_response_too_large"
    assert "private" not in str(caught.value)
    assert stream.emitted == 0 and stream.closed


async def test_unknown_length_stops_early_and_closes_stream(monkeypatch):
    from partner_agent.runtime import provider

    monkeypatch.setattr(provider, "MAX_RESPONSE_BYTES", 128)
    monkeypatch.setattr(provider, "RESPONSE_CHUNK_BYTES", 16)
    stream = Chunks([b"x" * 16] * 100)
    with pytest.raises(ProviderError, match="provider_response_too_large"):
        await chat_completion(settings(), [], client_factory=factory(stream))
    assert stream.emitted < 100 and stream.closed


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_compressed_expansion_is_bounded_before_full_allocation(monkeypatch, encoding):
    from partner_agent.runtime import provider

    monkeypatch.setattr(provider, "MAX_RESPONSE_BYTES", 1024)
    raw = b"x" * 200_000
    encoded = gzip.compress(raw) if encoding == "gzip" else zlib.compress(raw)
    assert len(encoded) < 1024
    stream = Chunks([encoded])
    with pytest.raises(ProviderError, match="provider_response_too_large"):
        await chat_completion(
            settings(),
            [],
            client_factory=factory(
                stream,
                headers={"Content-Encoding": encoding},
            ),
        )
    assert stream.closed


@pytest.mark.parametrize("encoded", [b"bad gzip", gzip.compress(b'{"choices":[]}')[:-4]])
async def test_corrupt_or_truncated_compression_fails_safely(encoded):
    stream = Chunks([encoded])
    with pytest.raises(ProviderError, match="provider_request_failed"):
        await chat_completion(
            settings(),
            [],
            client_factory=factory(
                stream,
                headers={"Content-Encoding": "gzip"},
            ),
        )
    assert stream.closed


async def test_unadvertised_encoding_fails_without_reading():
    stream = Chunks([b"not a supported encoding"])
    with pytest.raises(ProviderError, match="provider_response_encoding"):
        await chat_completion(
            settings(),
            [],
            client_factory=factory(
                stream,
                headers={"Content-Encoding": "br"},
            ),
        )
    assert stream.closed and stream.emitted == 0


async def test_total_deadline_stops_a_trickling_response_and_closes():
    stream = Chunks([b" "] * 100, delay=0.02)
    with pytest.raises(ProviderError, match="provider_request_timeout"):
        await chat_completion(
            settings(model_timeout_seconds=0.07), [], client_factory=factory(stream)
        )
    assert stream.closed and stream.emitted < 100


async def test_external_cancellation_is_not_relabelled_as_timeout():
    stream = Chunks([b" "] * 100, delay=0.01)
    task = asyncio.create_task(chat_completion(settings(), [], client_factory=factory(stream)))
    while stream.emitted == 0:
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


async def test_error_http_body_usage_survives_bounded_transport():
    usage = {"prompt_tokens": 10, "completion_tokens": 2}
    stream = Chunks([json.dumps({"usage": usage, "error": "private detail"}).encode()])
    with pytest.raises(ProviderError) as caught:
        await chat_completion(settings(), [], client_factory=factory(stream, status=429))
    assert caught.value.code == "provider_request_failed" and caught.value.usage == usage
    assert "private" not in str(caught.value) and stream.closed


async def test_deep_invalid_envelope_does_not_leak_a_raw_recursion_error():
    stream = Chunks([b'{"x":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"])
    with pytest.raises(ProviderError) as caught:
        await chat_completion(settings(), [], client_factory=factory(stream))
    # Python's recursion limit may differ; either parser failure or envelope validation is safe.
    assert caught.value.code in {"provider_request_failed", "provider_invalid_response"}
    assert stream.closed


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1, 0])
def test_deadline_setting_must_be_finite_positive(timeout):
    with pytest.raises(ValidationError):
        settings(model_timeout_seconds=timeout)
