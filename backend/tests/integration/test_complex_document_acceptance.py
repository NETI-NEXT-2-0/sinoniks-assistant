"""Opt-in acceptance for generated Russian documents on real Docling and storage."""

import os
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, update
from test_chat_data_plane import plane as plane

from partner_agent.documents.indexing import stage_version
from partner_agent.documents.ingest import storage
from partner_agent.documents.parser_host import acquire_host_lock, process_version
from partner_agent.infra.db import session_factory
from partner_agent.infra.models import User
from partner_agent.infra.models_documents import DisclosureUnit, Document, DocumentVersion
from partner_agent.infra.settings import get_settings
from partner_agent.retrieval.index import FixtureIndex

pytestmark = [pytest.mark.integration, pytest.mark.storage, pytest.mark.parser]
COURSE_CODE = "EDU-204"
NOTE = "Материалы доступны только участникам курса."
PRIVATE_CODE = "DEMO-PRIVATE-731"
KNOWLEDGE = "/api/v1/knowledge"


async def cleanup(document_id, version_id):
    async with session_factory()() as db:
        version = await db.get(DocumentVersion, version_id) if version_id else None
        await db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(active_version_id=None, last_published_version_id=None)
        )
        if version:
            await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
            await db.delete(version)
            await db.flush()
        await db.execute(delete(Document).where(Document.id == document_id))
        await db.commit()
    if version:
        await FixtureIndex(get_settings().qdrant_url).delete_version(version.id)
        adapter = storage()
        with closing(adapter.client):
            adapter.delete(adapter.originals_bucket, version.original_key)
            if version.canonical_key:
                adapter.delete(adapter.derived_bucket, version.canonical_key)


def block_with_text(blocks, text):
    return next(block for block in blocks if text in block["text"])


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("course.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("course-scan.pdf", "application/pdf"),
        ("course-scan.png", "image/png"),
        ("course-scan-150.png", "image/png"),
    ],
)
async def test_publish_and_retrieve_complex_russian_documents(plane, name, content_type):
    corpus = os.environ.get("PARTNER_COMPLEX_PARSER_CORPUS")
    if not corpus:
        pytest.skip(
            "Set PARTNER_COMPLEX_PARSER_CORPUS to the generated parser acceptance directory"
        )
    original = (Path(corpus) / name).read_bytes()
    owner, headers, owner_id = await plane(role="curator")
    created = await owner.post(f"{KNOWLEDGE}/documents", headers=headers, json={"title": name})
    assert created.status_code == 201
    document_id = UUID(created.json()["id"])
    version_id = None

    async def search(question):
        response = await owner.post(
            f"{KNOWLEDGE}/search", headers=headers, json={"query": question}
        )
        assert response.status_code == 200
        return response.json()["items"]

    try:
        uploaded = await owner.post(
            f"{KNOWLEDGE}/documents/{document_id}/versions?expected_revision=1",
            headers={**headers, "Content-Type": content_type, "Idempotency-Key": str(uuid4())},
            content=original,
        )
        assert uploaded.status_code == 202
        version_id = UUID(uploaded.json()["id"])
        lock = acquire_host_lock()
        try:
            assert await process_version(version_id, lock)
        finally:
            os.close(lock)

        path = f"{KNOWLEDGE}/versions/{version_id}"
        response = await owner.get(f"{path}/review")
        assert response.status_code == 200
        review = response.json()
        assert review["quality_block"] is None and not review["unresolved_notes"]
        assert review["next_offset"] is None
        assert ("png_dpi_assumed" in review["warnings"]) == (name == "course-scan.png")
        blocks = review["blocks"]
        course = block_with_text(blocks, COURSE_CODE)
        assert "Регламент" in course["text"]
        block_with_text(blocks, PRIVATE_CODE)
        approved = [course["id"]]
        table = next(block for block in blocks if block["cells"])
        assert {(cell["row"], cell["column"], cell["text"]) for cell in table["cells"]} == {
            (0, 0, "Модуль"),
            (0, 1, "Часы"),
            (1, 0, "Python"),
            (1, 1, "24"),
            (2, 0, "SQL"),
            (2, 1, "16"),
        }
        assert "Модуль | Часы | SQL | 16" in table["text"]
        approved.append(table["id"])
        if name.endswith((".docx", ".pdf")):
            note = block_with_text(blocks, NOTE)
            approved.append(note["id"])
        if name.endswith(".docx"):
            assert note["ordinal"] in review["required_notes"]
            assert note["kind"] == "footnote"
            missing_context = await owner.post(
                f"{path}/approve",
                headers=headers,
                json={
                    "expected_revision": 2,
                    "canonical_sha256": review["canonical_sha256"],
                    "block_ids": [course["id"]],
                },
            )
            assert missing_context.status_code == 409
            assert missing_context.json()["code"] == "canonical_review_required"
        else:
            assert any(
                position["page"] == 1 and position["bbox"] for position in course["positions"]
            )
        if name.endswith(".pdf"):
            assert any(position["page"] == 2 and position["bbox"] for position in note["positions"])

        approved_response = await owner.post(
            f"{path}/approve",
            headers=headers,
            json={
                "expected_revision": 2,
                "canonical_sha256": review["canonical_sha256"],
                "block_ids": approved,
            },
        )
        assert approved_response.status_code == 202
        async with session_factory()() as db:
            actor = await db.get(User, owner_id)
            await stage_version(db, actor, document_id, version_id)
            await db.commit()
        assert await search(COURSE_CODE) == []
        published = await owner.post(
            f"{path}/publish", headers=headers, json={"expected_revision": 2}
        )
        assert published.status_code == 200
        download = await owner.post(f"{path}/original/download", headers=headers)
        assert download.status_code == 200 and download.content == original

        hits = await search("Где регламент EDU-204?")
        (hit,) = hits
        assert hit["version_id"] == str(version_id)
        assert hit["evidence_id"] and COURSE_CODE in hit["excerpt"]
        assert course["positions"][0] in hit["locator"]["positions"]
        if name.endswith(".docx"):
            assert NOTE in hit["excerpt"]
            assert note["positions"][0] in hit["locator"]["context"][0]["positions"]
        assert any(
            "Модуль | Часы | SQL | 16" in item["excerpt"]
            for item in await search("Сколько часов у модуля SQL?")
        )
        if name.endswith(".pdf"):
            assert any(
                NOTE in item["excerpt"]
                and any(
                    position["page"] == 2 and position["bbox"]
                    for position in item["locator"]["positions"]
                )
                for item in await search("Кому доступны материалы курса?")
            )
        assert await search(PRIVATE_CODE) == []
        assert await search("EDU-2040") == []
    finally:
        await cleanup(document_id, version_id)
