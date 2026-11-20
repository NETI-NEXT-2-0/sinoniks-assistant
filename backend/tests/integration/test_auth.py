"""Runs against a migrated, disposable PostgreSQL database; never creates an SQLite substitute."""

import os
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from partner_agent.auth.security import digest
from partner_agent.cli import first_admin, reset_password
from partner_agent.infra.db import get_engine, session_factory
from partner_agent.infra.models import AuditEvent, RateLimit, Session, User, utcnow
from partner_agent.infra.settings import get_settings
from partner_agent.main import app

pytestmark = pytest.mark.integration


async def test_admin_accounts_revision_and_session_revocation(client):
    response = await login(client, await prepare(client))
    assert response.status_code == 200
    headers = {"Origin": "http://localhost:8080", "X-CSRF-Token": response.json()["csrf_token"]}
    payload = {"login": "Member", "password": "synthetic-member-password", "role": "user"}
    assert (await client.post("/api/v1/admin/users", json=payload)).status_code == 403
    created = await client.post("/api/v1/admin/users", json=payload, headers=headers)
    assert created.status_code == 201
    member = created.json()
    assert member["login"] == "member" and member["revision"] == 1
    assert "password" not in created.text
    assert (
        await client.post("/api/v1/admin/users", json=payload, headers=headers)
    ).status_code == 409
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080"
    ) as second:
        member_login = await second.post(
            "/api/v1/auth/login", headers=await prepare(second), json=payload
        )
        assert member_login.status_code == 200
        assert (await second.get("/api/v1/admin/users")).status_code == 403
        updated = await client.patch(
            f"/api/v1/admin/users/{member['id']}",
            headers=headers,
            json={"expected_revision": 1, "disabled": True},
        )
        assert updated.status_code == 200 and updated.json()["revision"] == 2
        assert (await second.get("/api/v1/auth/me")).status_code == 401
    conflict = await client.patch(
        f"/api/v1/admin/users/{member['id']}",
        headers=headers,
        json={"expected_revision": 1, "disabled": False},
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "revision_conflict"
    admin_id = response.json()["user"]["id"]
    last = await client.patch(
        f"/api/v1/admin/users/{admin_id}",
        headers=headers,
        json={"expected_revision": 1, "disabled": True},
    )
    assert last.status_code == 409 and last.json()["code"] == "last_administrator"


@pytest.mark.parametrize("role", ["admin", "curator", "user"])
async def test_manager_role_combines_directory_and_document_workflows(client, role):
    from partner_agent.infra.models_documents import Document

    async with session_factory()() as db:
        actor = await db.scalar(select(User).where(User.normalized_login == "admin"))
        actor.role = role
        await db.commit()
    response = await login(client, await prepare(client))
    assert response.status_code == 200
    headers = {"Origin": "http://localhost:8080", "X-CSRF-Token": response.json()["csrf_token"]}
    expected = 200 if role in ("admin", "curator") else 403
    assert (await client.get("/api/v1/admin/users")).status_code == expected
    assert (await client.get("/api/v1/admin/workspaces")).status_code == expected
    created = await client.post(
        "/api/v1/knowledge/documents", json={"title": "Manager test"}, headers=headers
    )
    assert created.status_code == (201 if expected == 200 else 403)
    if created.status_code == 201:
        from uuid import UUID

        async with session_factory()() as db:
            await db.execute(delete(Document).where(Document.id == UUID(created.json()["id"])))
            await db.commit()


@pytest_asyncio.fixture(autouse=True)
async def isolated_db():
    url = os.environ.get("PARTNER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set PARTNER_TEST_DATABASE_URL to a migrated disposable PostgreSQL database")
    os.environ["PARTNER_DATABASE_URL"] = url
    os.environ["PARTNER_PUBLIC_ORIGIN"] = "http://localhost:8080"
    os.environ["PARTNER_ENVIRONMENT"] = "test"
    get_settings.cache_clear()
    get_engine.cache_clear()
    async with session_factory()() as db:
        # Refuse to erase non-test databases accidentally.
        from sqlalchemy import text

        name = await db.scalar(text("SELECT current_database()"))
        assert name.endswith("_test"), "Integration database name must end in _test"
        for model in (AuditEvent, RateLimit, Session, User):
            await db.execute(delete(model))
        await db.commit()
        await first_admin(db, "Admin", "safe-test-password-123")
    yield
    await get_engine().dispose()
    get_engine.cache_clear()
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080"
    ) as client:
        yield client


async def prepare(client):
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return {"Origin": "http://localhost:8080", "X-CSRF-Token": response.json()["csrf_token"]}


async def login(client, headers):
    return await client.post(
        "/api/v1/auth/login",
        headers=headers,
        json={"login": "ADMIN", "password": "safe-test-password-123"},
    )


async def test_login_rotates_and_logout_revokes(client):
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    headers = await prepare(client)
    anonymous = client.cookies.get("partner-dev-session")
    response = await login(client, headers)
    assert response.status_code == 200
    authenticated = client.cookies.get("partner-dev-session")
    assert authenticated != anonymous
    assert response.json()["csrf_token"] != headers["X-CSRF-Token"]
    assert "httponly" in response.headers["set-cookie"].lower()
    assert (await client.get("/api/v1/auth/me")).json()["role"] == "admin"
    async with session_factory()() as db:
        old = await db.scalar(select(Session).where(Session.token_hash == digest(anonymous)))
        current = await db.scalar(
            select(Session).where(Session.token_hash == digest(authenticated))
        )
        assert old.revoked_at is not None
        assert current.token_hash != authenticated
        assert current.csrf_hash != response.json()["csrf_token"]
    # Pre-login CSRF cannot authorize a post-login operation.
    assert (await client.post("/api/v1/auth/logout", headers=headers)).status_code == 403
    headers["X-CSRF-Token"] = response.json()["csrf_token"]
    assert (await client.post("/api/v1/auth/logout", headers=headers)).status_code == 204
    client.cookies.set("partner-dev-session", authenticated)
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_origin_csrf_and_uniform_credentials(client):
    rejected_password = "synthetic-rejected-password-9381"
    headers = await prepare(client)
    assert (await login(client, {})).status_code == 403
    assert (await login(client, {**headers, "Origin": "https://evil.example"})).status_code == 403
    assert (await login(client, {**headers, "X-CSRF-Token": "wrong"})).status_code == 403
    first = await client.post(
        "/api/v1/auth/login", headers=headers,
        json={"login": "missing", "password": rejected_password},
    )
    second = await client.post(
        "/api/v1/auth/login", headers=headers,
        json={"login": "admin", "password": rejected_password},
    )
    assert first.status_code == second.status_code == 401
    assert first.json()["code"] == second.json()["code"] == "invalid_credentials"
    assert all(rejected_password not in response.text for response in (first, second))


async def test_throttle_shared_across_sessions(client, monkeypatch):
    from partner_agent.auth import routes

    now = utcnow()
    monkeypatch.setattr(routes, "utcnow", lambda: now)
    for _ in range(5):
        headers = await prepare(client)
        response = await client.post(
            "/api/v1/auth/login", headers=headers, json={"login": "admin", "password": "bad"}
        )
        assert response.status_code == 401
    headers = await prepare(client)
    response = await login(client, headers)
    assert response.status_code == 429
    assert response.json()["code"] == "login_rate_limited"
    now += timedelta(minutes=1)
    response = await login(client, await prepare(client))
    assert response.status_code == 200


async def test_password_reset_and_expiry_revoke(client):
    response = await login(client, await prepare(client))
    assert response.status_code == 200
    async with session_factory()() as db:
        await reset_password(db, "admin", "changed-test-password-123")
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    response = await client.post(
        "/api/v1/auth/login",
        headers=await prepare(client),
        json={"login": "admin", "password": "changed-test-password-123"},
    )
    assert response.status_code == 200
    token = client.cookies.get("partner-dev-session")
    async with session_factory()() as db:
        await db.execute(
            update(Session)
            .where(Session.token_hash == digest(token))
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await db.commit()
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_bootstrap_no_default_or_second_admin(client):
    async with session_factory()() as db:
        with pytest.raises(ValueError, match="already exists"):
            await first_admin(db, "other", "safe-test-password-456")
        await db.rollback()
        with pytest.raises(ValueError, match="12"):
            await first_admin(db, "other", "short")
        admins = (await db.scalars(select(User).where(User.role == "admin"))).all()
        assert len(admins) == 1
        assert admins[0].password_hash.startswith("$argon2id$")


async def test_disabled_user_and_absolute_expiry(client):
    response = await login(client, await prepare(client))
    assert response.status_code == 200
    async with session_factory()() as db:
        await db.execute(update(User).where(User.normalized_login == "admin").values(disabled=True))
        await db.commit()
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    headers = await prepare(client)
    assert (await login(client, headers)).json()["code"] == "invalid_credentials"
    async with session_factory()() as db:
        await db.execute(update(User).values(disabled=False))
        await db.commit()
    response = await login(client, await prepare(client))
    assert response.status_code == 200
    token = client.cookies.get("partner-dev-session")
    expiry = utcnow() - timedelta(seconds=1)
    async with session_factory()() as db:
        await db.execute(
            update(Session)
            .where(Session.token_hash == digest(token))
            .values(expires_at=expiry, absolute_expires_at=expiry)
        )
        await db.commit()
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_anonymous_expiry(client):
    headers = await prepare(client)
    token = client.cookies.get("partner-dev-session")
    async with session_factory()() as db:
        session = await db.scalar(select(Session).where(Session.token_hash == digest(token)))
        assert session.user_id is None
        assert (session.absolute_expires_at - session.created_at).total_seconds() <= 600
        expiry = utcnow() - timedelta(seconds=1)
        session.expires_at = expiry
        session.absolute_expires_at = expiry
        await db.commit()
    assert (await login(client, headers)).status_code == 401


async def test_input_errors_use_safe_envelope(client):
    headers = await prepare(client)
    response = await client.post(
        "/api/v1/auth/login", headers=headers, json={"login": "admin", "password": "SECRET" * 1000}
    )
    assert response.status_code == 422
    assert set(response.json()) == {"code", "message", "request_id", "retryable", "details"}
    assert "SECRET" not in response.text
    assert response.json()["details"] == {}


async def test_general_throttle_is_distinct(client, monkeypatch):
    monkeypatch.setenv("PARTNER_API_REQUESTS_PER_MINUTE", "2")
    get_settings.cache_clear()
    assert (await client.get("/api/v1/auth/csrf")).status_code == 200
    assert (await client.get("/api/v1/auth/csrf")).status_code == 200
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 429
    assert response.json()["code"] == "api_rate_limited"


async def test_login_upgrades_argon2_and_idle_refresh_respects_absolute_limit(client):
    from argon2 import PasswordHasher

    from partner_agent.auth.security import password_hasher

    password = "safe-test-password-123"
    weak_hash = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(password)
    async with session_factory()() as db:
        await db.execute(
            update(User).where(User.normalized_login == "admin").values(password_hash=weak_hash)
        )
        await db.commit()
    response = await login(client, await prepare(client))
    assert response.status_code == 200
    token = client.cookies.get("partner-dev-session")
    absolute = utcnow() + timedelta(seconds=90)
    before_idle = utcnow() + timedelta(seconds=30)
    old_seen = utcnow() - timedelta(minutes=1)
    async with session_factory()() as db:
        user = await db.scalar(select(User).where(User.normalized_login == "admin"))
        assert user.password_hash != weak_hash
        assert password_hasher.verify(password, user.password_hash)
        await db.execute(
            update(Session)
            .where(Session.token_hash == digest(token))
            .values(expires_at=before_idle, absolute_expires_at=absolute, last_seen_at=old_seen)
        )
        await db.commit()
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    async with session_factory()() as db:
        session = await db.scalar(select(Session).where(Session.token_hash == digest(token)))
        assert session.last_seen_at > old_seen
        assert session.expires_at > before_idle
        assert session.expires_at == session.absolute_expires_at == absolute
