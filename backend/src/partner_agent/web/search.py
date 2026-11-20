"""Bounded SearXNG discovery. Result links are never opened by the API."""

import asyncio
import json
import re
from urllib.parse import urlsplit

import httpx

from partner_agent.documents.web_source import normalize_source_url
from partner_agent.infra.errors import APIError
from partner_agent.infra.settings import Settings

MAX_RESULTS = 5
MAX_REPLY_BYTES = 1024 * 1024
MAX_QUERY_CHARS = 200
SEARCH_TIMEOUT = 12
_PUBLIC_TERMS = re.compile(r"[\w .,'\"«»()?-]+")


def official_domains(settings: Settings) -> tuple[str, ...]:
    if not settings.searxng_url or not settings.web_official_domains.strip():
        return ()
    try:
        endpoint = urlsplit(settings.searxng_url)
        if (
            endpoint.scheme not in ("http", "https") or not endpoint.hostname
            or endpoint.username is not None or endpoint.password is not None
            or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment
            or any(char.isspace() or char == "\\" for char in settings.searxng_url)
        ):
            return ()
        # Plain HTTP is intended for the private Compose service and loopback smoke tests.
        if endpoint.scheme == "http" and endpoint.hostname not in (
            "searxng", "localhost", "127.0.0.1",
        ):
            return ()
        domains = tuple(dict.fromkeys(
            item.strip().lower() for item in settings.web_official_domains.split(",")
            if item.strip()
        ))
        if len(domains) > 20 or any(
            normalize_source_url(f"https://{domain}") != f"https://{domain}/" for domain in domains
        ):
            return ()
    except ValueError:
        return ()
    return domains


def search_parameters(settings: Settings, domain: str, query: str) -> dict[str, str]:
    domains = official_domains(settings)
    if not domains:
        raise APIError(503, "web_search_disabled", "Official web search is not configured")
    if domain not in domains:
        raise APIError(422, "web_domain_not_allowed", "Select a configured official domain")
    query = " ".join(query.split())
    if (
        not query or len(query) > MAX_QUERY_CHARS or not _PUBLIC_TERMS.fullmatch(query)
        or not any(char.isalnum() for char in query)
    ):
        raise APIError(422, "web_public_terms_required", "Enter short public search terms")
    return {
        "q": f"site:{domain} {query}", "format": "json", "categories": "general",
        "language": "ru", "safesearch": "1",
    }


def _unavailable() -> APIError:
    return APIError(503, "web_search_unavailable", "The search service did not respond", True)


def _display_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(char if char.isprintable() else " " for char in value)
    return " ".join(text.split())[:limit]


async def search_official(
    endpoint: str, domain: str, parameters: dict[str, str],
) -> tuple[list[dict[str, str]], bool]:
    try:
        async with asyncio.timeout(SEARCH_TIMEOUT), httpx.AsyncClient(
            timeout=SEARCH_TIMEOUT, follow_redirects=False, trust_env=False,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        ) as client, client.stream(
            "POST", endpoint.rstrip("/") + "/search", data=parameters,
        ) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if len(body) + len(chunk) > MAX_REPLY_BYTES:
                    raise _unavailable()
                body.extend(chunk)
        payload = json.loads(body)
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError, RecursionError):
        raise _unavailable() from None
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise _unavailable()
    raw_results = payload["results"]
    partial = bool(payload.get("unresponsive_engines"))
    if not raw_results and partial:
        raise _unavailable()
    matches = []
    seen = set()
    for result in raw_results:
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            continue
        try:
            url = normalize_source_url(result["url"])
        except ValueError:
            continue
        host = urlsplit(url).hostname
        if not host or (host != domain and not host.endswith("." + domain)) or url in seen:
            continue
        matches.append({
            "url": url,
            "title": _display_text(result.get("title"), 240) or host,
            "excerpt": _display_text(result.get("content"), 500),
        })
        seen.add(url)
        if len(matches) >= MAX_RESULTS:
            break
    return matches, partial
