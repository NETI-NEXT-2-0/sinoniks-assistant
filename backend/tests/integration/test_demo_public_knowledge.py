"""Opt-in demo sharing is reversible and never shares chat/private materials."""

import hashlib
from types import SimpleNamespace

import pytest
from test_chat_data_plane import plane as plane
from test_response_execution import corpus as corpus

from partner_agent.documents.access import document_access_allowed
from partner_agent.infra.db import session_factory
from partner_agent.infra.models import User
from partner_agent.infra.models_documents import DisclosureAnswerDeny, Document, DocumentVersion
from partner_agent.infra.settings import get_settings
from partner_agent.retrieval.service import search_knowledge

pytestmark = pytest.mark.integration


async def test_demo_shares_published_sources_and_originals_without_edit_or_chat_access(
    corpus, plane, monkeypatch,
):
    _, _, _, _, _, doc_id, version_id, unit_id = corpus
    client, headers, reader_id = await plane()
    settings = get_settings()
    download = f"/api/v1/knowledge/versions/{version_id}/original/download"
    async with session_factory()() as db:
        reader = await db.get(User, reader_id)
        assert await search_knowledge(db, reader, "Synthetic course") == []
    assert (await client.post(download, headers=headers)).status_code == 404

    monkeypatch.setattr(settings, "demo_public_knowledge", True)
    body = b"Synthetic document source"
    monkeypatch.setattr("partner_agent.documents.ingest.storage", lambda: SimpleNamespace(
        originals_bucket="originals", get=lambda bucket, key: SimpleNamespace(data=body),
        client=SimpleNamespace(close=lambda: None),
    ))
    async with session_factory()() as db:
        version = await db.get(DocumentVersion, version_id)
        version.source_sha256 = hashlib.sha256(body).hexdigest()
        version.size_bytes = len(body)
        await db.commit()
        reader = await db.get(User, reader_id)
        doc = await db.get(Document, doc_id)
        assert [row["evidence_id"] for row in
                await search_knowledge(db, reader, "Synthetic course")] == [str(unit_id)]
        assert document_access_allowed(reader, doc, None, "answer")
        assert document_access_allowed(reader, doc, None, "original")
        assert not document_access_allowed(reader, doc, None, "manage")
        assert not document_access_allowed(reader, doc, None, "acl")
        await db.commit()
    response = await client.post(download, headers=headers)
    assert response.status_code == 200 and response.content == body

    # Operator can end the demo without rewriting existing grants or deleting data.
    monkeypatch.setattr(settings, "demo_public_knowledge", False)
    async with session_factory()() as db:
        reader = await db.get(User, reader_id)
        assert await search_knowledge(db, reader, "Synthetic course") == []
    assert (await client.post(download, headers=headers)).status_code == 404


@pytest.mark.parametrize("restriction", ["private", "unpublished", "deleted", "unit_deny"])
async def test_demo_keeps_publication_and_sensitive_excerpt_guards(
    corpus, plane, monkeypatch, restriction,
):
    _, _, _, _, _, doc_id, version_id, unit_id = corpus
    _, _, reader_id = await plane()
    monkeypatch.setattr(get_settings(), "demo_public_knowledge", True)
    async with session_factory()() as db:
        reader = await db.get(User, reader_id)
        doc = await db.get(Document, doc_id)
        assert await search_knowledge(db, reader, "Synthetic course")
        if restriction == "private":
            doc.scope = "private"
        elif restriction == "unpublished":
            doc.active_version_id = None
        elif restriction == "deleted":
            from partner_agent.infra.models import utcnow
            doc.deleted_at = utcnow()
        else:
            db.add(DisclosureAnswerDeny(unit_id=unit_id, user_id=reader_id))
        await db.commit()
        assert await search_knowledge(db, reader, "Synthetic course") == []
        if restriction != "unit_deny":
            assert not document_access_allowed(reader, doc, None, "original")
        doc.scope, doc.active_version_id, doc.deleted_at = "knowledge", version_id, None
        await db.commit()
