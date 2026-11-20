"""Run the labelled corpus through the document API and real infrastructure."""

import json
from contextlib import closing
from uuid import UUID, uuid4

import pytest
from demo_retrieval_corpus import DOCUMENTS, QUESTIONS, REMOVED
from sqlalchemy import delete, select, update
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_response_execution import configure_execution

from partner_agent.documents.edited_copy import copy_key
from partner_agent.documents.indexing import stage_version
from partner_agent.documents.ingest import process_stored_text, storage
from partner_agent.infra.db import session_factory
from partner_agent.infra.models import User
from partner_agent.infra.models_documents import (
    DisclosureUnit,
    Document,
    DocumentGrant,
    DocumentVersion,
)
from partner_agent.infra.settings import get_settings
from partner_agent.retrieval.index import FixtureIndex
from partner_agent.runtime.runner import execute_run

pytestmark = [pytest.mark.integration, pytest.mark.storage]
KNOWLEDGE = "/api/v1/knowledge"
WEB_SOURCE = {"url": "https://partner.example.org/courses/rag-204", "captured_on": "2026-11-21"}
WEB_DOCUMENTS = {"course", "venue"}


async def cleanup(document_ids):
    async with session_factory()() as db:
        versions = (
            await db.scalars(
                select(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids))
            )
        ).all()
        await db.execute(
            update(Document)
            .where(Document.id.in_(document_ids))
            .values(active_version_id=None, last_published_version_id=None)
        )
        await db.execute(delete(DocumentGrant).where(DocumentGrant.document_id.in_(document_ids)))
        await db.execute(
            delete(DisclosureUnit).where(
                DisclosureUnit.version_id.in_([version.id for version in versions])
            )
        )
        await db.execute(
            delete(DocumentVersion).where(DocumentVersion.document_id.in_(document_ids))
        )
        await db.execute(delete(Document).where(Document.id.in_(document_ids)))
        await db.commit()
    adapter = storage()
    index = FixtureIndex(get_settings().qdrant_url)
    with closing(adapter.client):
        for version in versions:
            if version.index_manifest:
                await index.delete_version(version.id)
            adapter.delete(adapter.originals_bucket, version.original_key)
            if version.canonical_key:
                adapter.delete(adapter.derived_bucket, version.canonical_key)
            if version.reviewed_copy_sha256:
                adapter.delete(
                    adapter.derived_bucket,
                    copy_key(version.document_id, version.id, version.reviewed_copy_sha256),
                )


async def check_answer(client, headers, question, source, hits, monkeypatch):
    mode = "grounded" if source else "insufficient"
    answer = question.quote if source else "В доступных документах нет ответа на этот вопрос."
    citation = {"evidence_id": source["evidence_id"], "quote": question.quote} if source else None
    usage = {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}
    calls = 0

    async def fake_completion(settings, messages):
        nonlocal calls
        calls += 1
        prefix = f"Question: {question.text}\nEvidence:\n"
        assert messages[-1]["content"].startswith(prefix), question.id
        context = messages[-1]["content"][len(prefix) :]
        blocks = []
        for item in hits:
            label = " (curator-reviewed text copy)"
            if item.get("web_source"):
                label += " (page snapshot captured 2026-11-21)"
            blocks.append(f"[{item['evidence_id']}]{label} {item['excerpt']}")
        expected = "\n\n".join(blocks)
        assert context == expected, question.id
        # The response cannot set or replace server-owned provenance.
        forged = (
            {**citation, "web_source": {"url": "https://forged.example.org"}} if citation else None
        )
        payload = {"answer": answer, "mode": mode, "citations": [forged] if forged else []}
        return {"choices": [{"message": {"content": json.dumps(payload)}}], "usage": usage}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", fake_completion)
    chat = await new_chat(client, headers)
    admitted = await submit(client, headers, chat, text=question.text)
    assert admitted.status_code == 202, question.id
    run_id = admitted.json()["run_id"]
    executed = await execute_run(UUID(run_id), dispatch_generation=0)
    assert executed["status"] == "succeeded" and calls == 1, question.id
    response = await client.get(f"/api/v1/chats/{chat['id']}/snapshot")
    assert response.status_code == 200
    snapshot = response.json()
    outcome = snapshot["runs"][0]["outcome"]
    expected_citations = []
    if citation and source:
        expected = {**citation, "title": f"Проверенная редакция · {source['title']}"}
        if "web_source" in source:
            expected["web_source"] = WEB_SOURCE
        expected_citations.append(expected)
    assert outcome["answer_mode"] == mode and outcome["citations"] == expected_citations
    assert outcome["usage"] == usage and outcome["usage_complete"] is True
    assert outcome["estimated_cost_usd"] == pytest.approx(0.00006)
    assert [
        message["content"] for message in snapshot["messages"] if message["role"] == "assistant"
    ] == [answer]


async def test_labelled_demo_retrieval_and_document_visibility(plane, monkeypatch):
    owner, owner_headers, owner_id = await plane(role="curator")
    reader, reader_headers, reader_id = await plane()
    outsider, outsider_headers, _ = await plane(role="admin")
    documents = {}
    versions = {}

    async def search(client, headers, query):
        response = await client.post(f"{KNOWLEDGE}/search", headers=headers, json={"query": query})
        assert response.status_code == 200
        return response.json()["items"]

    try:
        for key, (title, original) in DOCUMENTS.items():
            created = await owner.post(
                f"{KNOWLEDGE}/documents", headers=owner_headers, json={"title": title}
            )
            assert created.status_code == 201
            document_id = documents[key] = UUID(created.json()["id"])
            upload_path = f"{KNOWLEDGE}/documents/{document_id}/versions"
            upload_headers = {
                **owner_headers, "Content-Type": "text/plain", "Idempotency-Key": str(uuid4()),
            }
            params = {"expected_revision": 1}
            if key == "course":
                missing_date = await owner.post(
                    upload_path, params={**params, "source_url": WEB_SOURCE["url"]},
                    content=original.encode(), headers=upload_headers,
                )
                assert missing_date.status_code == 422
            if key in WEB_DOCUMENTS:
                params.update(
                    source_url=WEB_SOURCE["url"], source_captured_on=WEB_SOURCE["captured_on"]
                )
            uploaded = await owner.post(
                upload_path, params=params, content=original.encode(), headers=upload_headers,
            )
            assert uploaded.status_code == 202
            version_id = versions[key] = UUID(uploaded.json()["id"])
            if key == "course":
                reused = await owner.post(
                    upload_path, params=params, content=original.encode(), headers=upload_headers,
                )
                assert reused.status_code == 202 and reused.json()["id"] == str(version_id)
                assert reused.json()["reused"] is True
                conflict = await owner.post(
                    upload_path, params={**params, "source_captured_on": "2026-11-20"},
                    content=original.encode(), headers=upload_headers,
                )
                assert conflict.status_code == 409
                assert conflict.json()["code"] == "idempotency_conflict"
                assert (await outsider.get(f"{KNOWLEDGE}/versions/{version_id}")).status_code == 404
            listing = await owner.get(upload_path)
            expected_source = WEB_SOURCE if key in WEB_DOCUMENTS else None
            assert listing.json()["items"][0]["web_source"] == expected_source
            async with session_factory()() as db:
                actor = await db.get(User, owner_id)
                await process_stored_text(db, actor, document_id, version_id)
                await db.commit()
            path = f"{KNOWLEDGE}/versions/{version_id}"
            review = await owner.get(f"{path}/review")
            assert review.status_code == 200
            draft = review.json()
            assert "\n".join(block["text"] for block in draft["blocks"]) == original
            approved = await owner.post(
                f"{path}/approve",
                headers=owner_headers,
                json={
                    "expected_revision": 2,
                    "canonical_sha256": draft["canonical_sha256"],
                    "reviewed_text": original.replace(REMOVED, "[удалено]"),
                },
            )
            assert approved.status_code == 202
            async with session_factory()() as db:
                actor = await db.get(User, owner_id)
                await stage_version(db, actor, document_id, version_id)
                await db.commit()
            assert all(
                item["document_id"] != str(document_id)
                for item in await search(owner, owner_headers, original.splitlines()[0])
            )
            if key == "course":
                draft_summary = (await owner.get(f"{KNOWLEDGE}/summary")).json()
                assert draft_summary["documents"] == 1 and draft_summary["published_documents"] == 0
                assert draft_summary["searchable_excerpts"] == 0
            published = await owner.post(
                f"{path}/publish", headers=owner_headers, json={"expected_revision": 2}
            )
            assert published.status_code == 200
            if key != "staff":
                granted = await owner.put(
                    f"{KNOWLEDGE}/documents/{document_id}/grants/{reader_id}",
                    headers=owner_headers,
                    json={"expected_revision": 3, "allow_answer": True, "allow_original": False},
                )
                assert granted.status_code == 200

        assert len(QUESTIONS) == len({question.id for question in QUESTIONS}) == 20
        configure_execution(monkeypatch)
        monkeypatch.setenv("PARTNER_MODEL_INPUT_USD_PER_MILLION", "1")
        monkeypatch.setenv("PARTNER_MODEL_OUTPUT_USD_PER_MILLION", "2")
        monkeypatch.setenv("PARTNER_MODEL_DAILY_CAP_USD", "0")
        monkeypatch.setenv("PARTNER_MODEL_REQUEST_CAP_USD", "0")
        misses = []
        for question in QUESTIONS:
            items = await search(reader, reader_headers, question.text)
            assert all(
                item["document_id"] != str(documents["staff"]) and REMOVED not in item["excerpt"]
                for item in items
            ), question.id
            if question.blocked_document:
                assert any(
                    item["document_id"] == str(documents[question.blocked_document])
                    for item in await search(owner, owner_headers, question.text)
                ), question.id
            source = next(
                (
                    item
                    for item in items
                    if question.document is not None
                    and question.quote is not None
                    and item["document_id"] == str(documents[question.document])
                    and item["version_id"] == str(versions[question.document])
                    and question.quote in item["excerpt"]
                    and item["evidence_id"]
                    and item["locator"]["reviewed_copy"] is True
                ),
                None,
            )
            if source:
                expected_source = WEB_SOURCE if question.document in WEB_DOCUMENTS else None
                assert source.get("web_source") == expected_source
            if (question.document is None and items) or (question.document and source is None):
                misses.append(question.id)
                continue
            await check_answer(reader, reader_headers, question, source, items, monkeypatch)
        assert not misses, f"Unexpected results for: {', '.join(misses)}"

        assert await search(outsider, outsider_headers, "RAG-204") == []
        assert await search(owner, owner_headers, "STAFF-731")
        original_url = f"{KNOWLEDGE}/versions/{versions['support']}/original/download"
        assert (await reader.post(original_url, headers=reader_headers)).status_code == 404
        original = await owner.post(original_url, headers=owner_headers)
        assert original.status_code == 200 and original.content == DOCUMENTS["support"][1].encode()
        revoked = await owner.put(
            f"{KNOWLEDGE}/documents/{documents['course']}/grants/{reader_id}",
            headers=owner_headers,
            json={"expected_revision": 4, "allow_answer": False, "allow_original": False},
        )
        assert revoked.status_code == 200
        assert await search(reader, reader_headers, "RAG-204") == []

        summary_url = f"{KNOWLEDGE}/summary"
        summary = await owner.get(summary_url)
        assert summary.headers["cache-control"] == "private, no-store"
        assert summary.json() == {
            "documents": 4, "published_documents": 4, "searchable_documents": 4,
            "unique_source_urls": 1, "searchable_excerpts": 4,
        }
        assert (await reader.get(summary_url)).status_code == 403
        outsider_summary = await outsider.get(summary_url)
        assert outsider_summary.status_code == 200
        assert set(outsider_summary.json().values()) == {0}
        for key, expected_urls in [("course", 1), ("venue", 0)]:
            path = f"{KNOWLEDGE}/documents/{documents[key]}"
            revision = (await owner.get(path)).json()["revision"]
            withdrawn = await owner.post(
                f"{path}/unpublish", headers=owner_headers, json={"expected_revision": revision},
            )
            assert withdrawn.status_code == 200
            assert (await owner.get(summary_url)).json()["unique_source_urls"] == expected_urls
    finally:
        await cleanup(list(documents.values()))
