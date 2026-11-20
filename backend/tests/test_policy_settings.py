from uuid import uuid4

import pytest
from pydantic import ValidationError

from partner_agent.auth.policy import Principal, Resource, authorize
from partner_agent.auth.security import normalize_login
from partner_agent.infra.settings import Settings


def test_admin_has_no_implicit_content_access() -> None:
    admin = Principal(uuid4(), "admin")
    other = Resource(owner_id=uuid4())
    assert authorize(admin, "admin.users", other)
    assert not authorize(admin, "chat.read", other)
    assert not authorize(admin, "original.download", other)
    assert not authorize(admin, "knowledge.answer", other)
    assert authorize(
        admin, "knowledge.answer", Resource(allowed_actions=frozenset({"knowledge.answer"}))
    )
    assert not authorize(
        admin, "original.download", Resource(allowed_actions=frozenset({"knowledge.answer"}))
    )
    assert not authorize(
        admin,
        "knowledge.answer",
        Resource(
            allowed_actions=frozenset({"knowledge.answer"}),
            denied_actions=frozenset({"knowledge.answer"}),
        ),
    )


def test_owner_and_disabled_principal() -> None:
    user_id = uuid4()
    assert authorize(Principal(user_id, "user"), "chat.write", Resource(owner_id=user_id))
    assert not authorize(Principal(user_id, "admin", True), "admin.users", Resource())


@pytest.mark.parametrize("role", ["admin", "curator"])
def test_unified_manager_can_administer_and_manage_own_knowledge(role) -> None:
    from partner_agent.documents.access import document_access_allowed
    from partner_agent.infra.models import User
    from partner_agent.infra.models_documents import Document

    actor = User(id=uuid4(), role=role, disabled=False)
    own = Document(id=uuid4(), owner_id=actor.id, scope="knowledge", title="Own")
    other = Document(id=uuid4(), owner_id=uuid4(), scope="knowledge", title="Other")
    assert authorize(Principal(actor.id, role), "admin.groups", Resource())
    assert document_access_allowed(actor, own, None, "manage")
    assert document_access_allowed(actor, own, None, "answer")
    assert not document_access_allowed(actor, other, None, "manage")
    assert not document_access_allowed(actor, other, None, "original")
    actor.disabled = True
    assert not document_access_allowed(actor, own, None, "manage")


def test_normalization() -> None:
    assert normalize_login("  Ａdmin  ") == "admin"
    with pytest.raises(ValueError):
        normalize_login("a b")


def test_production_cookie_requires_https() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+psycopg://localhost/app", environment="production")
    settings = Settings(
        database_url="postgresql+psycopg://localhost/app",
        environment="production",
        public_origin="https://partner.example",
    )
    assert settings.cookie_name == "__Host-session"
    with pytest.raises(ValidationError):
        Settings(database_url="sqlite:///local.db")
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+psycopg://localhost/app", public_origin="http://0.0.0.0")


def test_production_cookie_flags(monkeypatch) -> None:
    from starlette.responses import Response

    from partner_agent.auth.routes import cookie
    from partner_agent.infra.settings import get_settings

    monkeypatch.setenv("PARTNER_DATABASE_URL", "postgresql+psycopg://localhost/app")
    monkeypatch.setenv("PARTNER_ENVIRONMENT", "production")
    monkeypatch.setenv("PARTNER_PUBLIC_ORIGIN", "https://partner.example")
    get_settings.cache_clear()
    try:
        response = Response()
        cookie(response, "opaque-session")
        header = response.headers["set-cookie"]
        assert header.startswith("__Host-session=")
        assert "Secure" in header and "HttpOnly" in header
        assert "SameSite=lax" in header and "Path=/" in header
        assert "Domain=" not in header
    finally:
        get_settings.cache_clear()


def test_invalid_lifetimes() -> None:
    with pytest.raises(ValidationError, match="idle TTL"):
        Settings(
            database_url="postgresql+psycopg://localhost/app",
            idle_ttl_seconds=101,
            absolute_ttl_seconds=100,
        )


def test_invalid_settings_hide_credential_inputs() -> None:
    secret = "startup-private-sentinel"
    with pytest.raises(ValidationError) as rejected:
        Settings(database_url=f"mysql://operator:{secret}@localhost/app")
    assert secret not in str(rejected.value)


def test_direct_grant_actions_are_distinct_and_principal_bound() -> None:
    from partner_agent.documents.access import document_access_allowed
    from partner_agent.infra.models import User
    from partner_agent.infra.models_documents import Document, DocumentGrant

    owner_id, reader_id, other_id, doc_id = uuid4(), uuid4(), uuid4(), uuid4()
    document = Document(id=doc_id, owner_id=owner_id, scope="knowledge", title="Course")
    reader = User(id=reader_id, role="user", disabled=False)
    grant = DocumentGrant(
        document_id=doc_id, user_id=reader_id, allow_answer=True, allow_original=False
    )
    assert document_access_allowed(reader, document, grant, "answer")
    assert not document_access_allowed(reader, document, grant, "original")
    other = User(id=other_id, role="admin", disabled=False)
    assert not document_access_allowed(other, document, grant, "answer")
    reader.disabled = True
    assert not document_access_allowed(reader, document, grant, "answer")
