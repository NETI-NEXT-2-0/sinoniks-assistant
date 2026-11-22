import logging

import httpx

from partner_agent.infra.db import database_session
from partner_agent.main import app


async def test_unexpected_exception_is_sanitized_without_rethrow(caplog) -> None:
    sentinel = "PRIVATE_INPUT_EXCEPTION_SENTINEL_74b312"

    async def broken_database() -> None:
        raise RuntimeError(sentinel)

    app.dependency_overrides[database_session] = broken_database
    try:
        with caplog.at_level(logging.ERROR):
            # True is essential: the default ServerErrorMiddleware would re-raise
            # even after rendering a sanitized global exception-handler response.
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
                base_url="http://localhost:8080",
            ) as client:
                response = await client.get("/api/v1/auth/csrf")
    finally:
        app.dependency_overrides.pop(database_session, None)

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["details"] == {}
    assert sentinel not in response.text
    assert sentinel not in caplog.text
    errors = [record for record in caplog.records if record.name == "partner_agent"]
    assert len(errors) == 1
    assert "RuntimeError" in errors[0].getMessage()
    assert body["request_id"] in errors[0].getMessage()
    assert errors[0].exc_info is None
