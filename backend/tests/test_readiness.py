import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
from pydantic import SecretStr

import partner_agent.main as application


async def test_blocked_database_readiness_times_out_while_liveness_responds(monkeypatch) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    @asynccontextmanager
    async def blocked_connection():
        started.set()
        try:
            await asyncio.Event().wait()
            yield
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(
        application, "get_engine", lambda: SimpleNamespace(connect=blocked_connection)
    )
    monkeypatch.setattr(application, "DB_READINESS_TIMEOUT_SECONDS", 0.05)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application.app, raise_app_exceptions=True),
        base_url="http://localhost:8080",
    ) as client:
        readiness = asyncio.create_task(client.get("/health/ready"))
        await asyncio.wait_for(started.wait(), timeout=1)
        live = await asyncio.wait_for(client.get("/health/live"), timeout=1)
        assert live.status_code == 200
        response = await asyncio.wait_for(readiness, timeout=1)
    assert response.status_code == 503
    assert response.json()["code"] == "dependency_timeout"
    assert cancelled.is_set()


async def test_blocked_storage_probe_times_out_without_blocking_liveness(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    async def schema(_query):
        return "0029_render_timings"

    @asynccontextmanager
    async def connected():
        yield SimpleNamespace(scalar=schema)

    def blocked_storage():
        started.set()
        release.wait(timeout=2)

    monkeypatch.setattr(application, "get_engine", lambda: SimpleNamespace(connect=connected))
    monkeypatch.setattr(
        application,
        "get_settings",
        lambda: SimpleNamespace(
            s3_access_key_id="synthetic-key", s3_secret_access_key=SecretStr("synthetic-secret")
        ),
    )
    monkeypatch.setattr(application, "check_storage", blocked_storage)
    monkeypatch.setattr(application, "STORAGE_READINESS_TIMEOUT_SECONDS", 0.05)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application.app, raise_app_exceptions=True),
            base_url="http://localhost:8080",
        ) as client:
            readiness = asyncio.create_task(client.get("/health/ready"))
            async with asyncio.timeout(1):
                while not started.is_set():
                    await asyncio.sleep(0.001)
            live = await asyncio.wait_for(client.get("/health/live"), timeout=1)
            assert live.status_code == 200
            response = await asyncio.wait_for(readiness, timeout=1)
            assert not release.is_set()
            assert response.status_code == 503
            assert response.json()["code"] == "storage_unavailable"
    finally:
        release.set()
