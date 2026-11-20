"""Authorized-corpus ranking on real PostgreSQL, no storage/provider fixture."""

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import delete
from test_chat_data_plane import plane as plane

from partner_agent.infra.db import session_factory
from partner_agent.infra.models_documents import (
    DisclosureAnswerDeny,
    DisclosureUnit,
    Document,
    DocumentGrant,
    DocumentVersion,
)

pytestmark = pytest.mark.integration


async def test_morphology_ranking_ignores_foreign_denied_and_inactive_units(plane):
    client, headers, owner_id = await plane(role="curator")
    outsider, outsider_headers, outsider_id = await plane(role="admin")
    documents = []
    versions = []
    units = []

    async def add_document(owner, text, *, active=True):
        document_id, version_id, unit_id = uuid4(), uuid4(), uuid4()
        documents.append(document_id)
        versions.append(version_id)
        units.append(unit_id)
        async with session_factory()() as db:
            document = Document(id=document_id, owner_id=owner, scope="knowledge", title="Fixture")
            db.add(document)
            await db.flush()
            db.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    generation=1,
                    source_sha256="a" * 64,
                    media_type="text/markdown",
                    size_bytes=len(text.encode()),
                    original_key=f"{version_id}/original",
                    canonical_key="canonical",
                    canonical_sha256="b" * 64,
                    approved_canonical_sha256="b" * 64,
                    pipeline_version="canonical-v1",
                    status="ready_for_review",
                )
            )
            await db.flush()
            db.add(
                DisclosureUnit(
                    id=unit_id,
                    version_id=version_id,
                    block_id=uuid4(),
                    text=text,
                    text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    locator={"section": "Проверки", "start": 0, "end": len(text)},
                    policy_revision=1,
                )
            )
            if active:
                document.active_version_id = version_id
            await db.commit()
        return document_id, unit_id

    async def search(query="проверки файлов"):
        response = await client.post(
            "/api/v1/knowledge/search", headers=headers, json={"query": query}
        )
        assert response.status_code == 200
        assert response.json()["profile"] == "lexical_bm25_ru_v1"
        return response.json()["items"]

    try:
        _, target = await add_document(owner_id, "Проверка файла выполняется через ICAP.")
        _, other = await add_document(owner_id, "Обычные файлы хранятся в каталоге.")
        await add_document(owner_id, "Проверка файлов архивной версии.", active=False)
        baseline = await search()
        assert baseline[0]["evidence_id"] == str(target)
        assert baseline[0]["locator"]["section"] == "Проверки"
        assert {item["evidence_id"] for item in baseline} == {str(target), str(other)}
        for _ in range(4):
            await add_document(outsider_id, "Проверка файлов ICAP чужой секрет.")
        denied_doc, denied_unit = await add_document(outsider_id, "Проверка файлов ICAP скрыта.")
        async with session_factory()() as db:
            db.add(
                DocumentGrant(
                    document_id=denied_doc,
                    user_id=owner_id,
                    allow_answer=True,
                    allow_original=False,
                )
            )
            db.add(DisclosureAnswerDeny(unit_id=denied_unit, user_id=owner_id))
            await db.commit()
        assert await search() == baseline  # Hidden corpus cannot alter IDF or relevance scores.
        # An admin role alone does not reveal another owner's private excerpts.
        response = await outsider.post(
            "/api/v1/knowledge/search",
            headers=outsider_headers,
            json={"query": "Обычные файлы хранятся в каталоге"},
        )
        assert all(item["evidence_id"] != str(other) for item in response.json()["items"])
        # Live grant, then revoke: the next search must immediately forget both text and IDF.
        async with session_factory()() as db:
            deny = await db.get(DisclosureAnswerDeny, (denied_unit, owner_id))
            await db.delete(deny)
            await db.commit()
        assert str(denied_unit) in {item["evidence_id"] for item in await search()}
        async with session_factory()() as db:
            grant = await db.get(DocumentGrant, (denied_doc, owner_id))
            await db.delete(grant)
            await db.commit()
        assert await search() == baseline
        assert await search("ICAP 999999") == []
    finally:
        async with session_factory()() as db:
            for identifier in documents:
                document = await db.get(Document, identifier)
                if document:
                    document.active_version_id = None
            await db.flush()
            await db.execute(
                delete(DisclosureAnswerDeny).where(DisclosureAnswerDeny.unit_id.in_(units))
            )
            await db.execute(delete(DocumentGrant).where(DocumentGrant.document_id.in_(documents)))
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id.in_(versions)))
            await db.execute(delete(DocumentVersion).where(DocumentVersion.id.in_(versions)))
            await db.execute(delete(Document).where(Document.id.in_(documents)))
            await db.commit()
