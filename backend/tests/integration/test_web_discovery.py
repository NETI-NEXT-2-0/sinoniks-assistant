from datetime import UTC, datetime

import pytest
from test_chat_data_plane import plane as plane

from partner_agent.infra.settings import get_settings

pytestmark = pytest.mark.integration
WEB = "/api/v1/web"


async def test_public_web_discovery_admission(plane, monkeypatch):
    manager, headers, _ = await plane(role="curator")
    reader, reader_headers, _ = await plane()
    now = datetime.now(UTC)
    monkeypatch.setattr("partner_agent.auth.routes.utcnow", lambda: now)
    calls = []

    async def transport(endpoint, domain, parameters):
        calls.append((endpoint, domain, parameters))
        return [{"url": "https://example.org/python", "title": "Python", "excerpt": "Intro"}], False

    monkeypatch.setattr("partner_agent.web.routes.search_official", transport)
    monkeypatch.setenv("PARTNER_SEARXNG_URL", "")
    get_settings.cache_clear()
    body = {"domain": "example.org", "query": "Публичный курс", "confirmed_public": True}
    assert (await manager.get(f"{WEB}/official-domains")).json() == {"domains": []}
    disabled = await manager.post(f"{WEB}/search", json=body, headers=headers)
    assert disabled.status_code == 503 and disabled.json()["code"] == "web_search_disabled"
    monkeypatch.setenv("PARTNER_SEARXNG_URL", "http://searxng:8080")
    monkeypatch.setenv("PARTNER_WEB_OFFICIAL_DOMAINS", "example.org,docs.example.net")
    get_settings.cache_clear()
    assert (await manager.get(f"{WEB}/official-domains")).json() == {
        "domains": ["example.org", "docs.example.net"],
    }
    assert (await reader.get(f"{WEB}/official-domains")).status_code == 403
    reader_search = await reader.post(f"{WEB}/search", json=body, headers=reader_headers)
    assert reader_search.status_code == 403
    assert (await manager.post(f"{WEB}/search", json=body)).status_code == 403
    for patch in ({"confirmed_public": False}, {"history": []}, {"domain": "evil.org"}):
        rejected = await manager.post(f"{WEB}/search", json={**body, **patch}, headers=headers)
        assert rejected.status_code == 422
    assert not calls
    for _ in range(12):
        accepted = await manager.post(f"{WEB}/search", json=body, headers=headers)
        assert accepted.status_code == 200
        assert accepted.headers["cache-control"] == "private, no-store"
        assert accepted.json()["items"][0]["url"] == "https://example.org/python"
    for _, domain, parameters in calls:
        assert domain == "example.org" and parameters["q"] == "site:example.org Публичный курс"
    limited = await manager.post(f"{WEB}/search", json=body, headers=headers)
    assert limited.status_code == 429 and limited.json()["code"] == "web_search_rate_limited"
    assert len(calls) == 12
