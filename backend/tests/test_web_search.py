from urllib.parse import parse_qs

import httpx
import pytest

from partner_agent.infra.errors import APIError
from partner_agent.infra.settings import Settings
from partner_agent.web.search import official_domains, search_official, search_parameters


@pytest.fixture
def settings():
    return Settings(
        database_url="postgresql+psycopg://u:p@localhost/db",
        searxng_url="http://searxng:8080", web_official_domains="EXAMPLE.org, example.org",
    )


def test_search_parameters_limit_engines_to_an_explicit_public_subject(settings):
    assert official_domains(settings) == ("example.org",)
    assert search_parameters(settings, "example.org", "  Курсы   Python 3.12 ")["q"] == (
        "site:example.org Курсы Python 3.12"
    )
    with pytest.raises(APIError, match="Select a configured official domain"):
        search_parameters(settings, "example.org.evil.org", "Python")
    for text in ("!engine Python", "site:private.example.org", "a@example.org", "https://example.org"):
        with pytest.raises(APIError, match="Enter short public search terms"):
            search_parameters(settings, "example.org", text)
    assert official_domains(settings.model_copy(update={"web_official_domains": "127.0.0.1"})) == ()
    assert official_domains(settings.model_copy(update={"searxng_url": "http://another.org"})) == ()


def mock_search_client(monkeypatch, transport):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        "partner_agent.web.search.httpx.AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(transport), **kwargs),
    )


async def test_filters_untrusted_search_results_after_transport(monkeypatch, settings):
    def transport(request):
        assert request.method == "POST" and str(request.url) == "http://searxng:8080/search"
        assert parse_qs(request.content.decode())["q"] == ["site:example.org Курсы Python"]
        assert not request.headers.get("cookie")
        return httpx.Response(200, json={
            "results": [
                {"url": "https://example.org.evil.org/a"},
                {"url": "https://evil.org/?url=https://example.org"},
                {"url": "https://example.org@evil.org/"},
                {"url": "http://example.org/a"},
                {"url": "https://docs.example.org/a", "title": "Example", "content": "Intro"},
                {"url": "https://docs.example.org/a", "title": "Duplicate"},
                *({"url": f"https://example.org/p/{i}"} for i in range(8)),
            ],
            "unresponsive_engines": [["startpage", "timeout"]],
        })

    mock_search_client(monkeypatch, transport)
    domain = "example.org"
    items, partial = await search_official(
        settings.searxng_url, domain, search_parameters(settings, domain, "Курсы Python"),
    )
    assert partial and len(items) == 5
    assert items[0] == {"url": "https://docs.example.org/a", "title": "Example", "excerpt": "Intro"}
    assert [item["url"] for item in items[1:]] == [f"https://example.org/p/{i}" for i in range(4)]


@pytest.mark.parametrize("reply", [
    httpx.Response(302, headers={"Location": "http://private.invalid/"}),
    httpx.Response(403),
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, content=b"y" * (1024 * 1024 + 1)),
    httpx.Response(200, json={"results": [], "unresponsive_engines": [["duckduckgo", "403"]]}),
])
async def test_unavailable_search_is_distinct_from_empty_results(monkeypatch, settings, reply):
    calls = []

    def transport(request):
        calls.append(request.url)
        return reply

    mock_search_client(monkeypatch, transport)
    with pytest.raises(APIError, match="search service did not respond"):
        await search_official(settings.searxng_url, "example.org", {})
    assert len(calls) == 1


async def test_search_returns_normal_empty_response_when_engines_respond(monkeypatch, settings):
    mock_search_client(monkeypatch, lambda request: httpx.Response(200, json={"results": []}))
    assert await search_official(settings.searxng_url, "example.org", {}) == ([], False)
