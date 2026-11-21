"""Reopening sources rechecks chat ownership, current grants and manifest membership."""

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_response_execution import configure_execution
from test_response_execution import corpus as corpus

from partner_agent.documents.chat_files import chunk_digest
from partner_agent.infra.db import session_factory
from partner_agent.infra.models_chat_files import ChatFile
from partner_agent.infra.models_documents import (
    DisclosureAnswerDeny,
    Document,
    DocumentGrant,
    DocumentVersion,
)
from partner_agent.infra.models_stage2 import ContextManifest, Run
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


async def cited_run(corpus, monkeypatch):
    _, _, client, headers, _, _, _, unit = corpus
    configure_execution(monkeypatch)

    async def model(settings, messages):
        assert str(unit) in messages[-1]["content"]
        return {"choices": [{"message": {"content": json.dumps({
            "answer": "Synthetic course costs 100.", "mode": "grounded", "citations": [{
                "evidence_id": str(unit), "quote": "course costs 100.",
                "title": "Forged title", "location": "Forged page",
            }],
        })}}]}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    chat = await new_chat(client, headers)
    admitted = await submit(client, headers, chat, text="How much does the Synthetic course cost?")
    run = admitted.json()["run_id"]
    assert (await execute_run(UUID(run), dispatch_generation=0))["status"] == "succeeded"
    return client, headers, f"/api/v1/runs/{run}/citations/0", run


async def test_source_reopens_real_excerpt_and_keeps_original_permission_separate(
    corpus, plane, monkeypatch,
):
    client, headers, path, _ = await cited_run(corpus, monkeypatch)
    response = await client.get(path)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["title"] == "Synthetic" and data["location"] == "Строка источника 1"
    assert data["quote"] == "course costs 100."
    assert data["excerpt"] == "Synthetic course costs 100."
    assert data["original_download_path"] is None
    assert response.headers["cache-control"] == "private, no-store"
    admin, _, _ = await plane("admin")
    assert (await admin.get(path)).status_code == 404
    assert (await corpus[0].get(path)).status_code == 404
    assert (await client.get(path[:-1] + "1")).status_code == 404
    assert (await client.get(path[:-1] + "50")).status_code == 422
    assert (await client.get(f"/api/v1/runs/{uuid4()}/citations/0")).status_code == 404
    download = f"/api/v1/knowledge/versions/{corpus[6]}/original/download"
    assert (await client.post(download, headers=headers)).status_code == 404


@pytest.mark.parametrize("change", ["grant", "unit_deny", "unpublish", "version"])
async def test_reopening_old_citation_revalidates_the_source(corpus, monkeypatch, change):
    client, _, path, _ = await cited_run(corpus, monkeypatch)
    assert (await client.get(path)).status_code == 200
    _, _, _, _, reader, document, version, unit = corpus
    async with session_factory()() as db:
        if change == "grant":
            grant = await db.get(DocumentGrant, (document, reader))
            await db.delete(grant)
        elif change == "unit_deny":
            db.add(DisclosureAnswerDeny(unit_id=unit, user_id=reader))
        elif change == "unpublish":
            (await db.get(Document, document)).active_version_id = None
        else:
            (await db.get(DocumentVersion, version)).status = "processing"
        await db.commit()
    denied = await client.get(path)
    assert denied.status_code == 409
    assert denied.json()["code"] == "source_context_changed"
    assert "course costs" not in denied.text


async def test_original_download_rechecks_permission_after_source_was_opened(corpus, monkeypatch):
    client, headers, path, _ = await cited_run(corpus, monkeypatch)
    reader, document = corpus[4], corpus[5]
    async with session_factory()() as db:
        grant = await db.get(DocumentGrant, (document, reader))
        grant.allow_original = True
        await db.commit()
    download = (await client.get(path)).json()["original_download_path"]
    assert download == f"/knowledge/versions/{corpus[6]}/original/download"
    async with session_factory()() as db:
        (await db.get(DocumentGrant, (document, reader))).allow_original = False
        await db.commit()
    assert (await client.post("/api/v1" + download, headers=headers)).status_code == 404
    assert (await client.get(path)).json()["original_download_path"] is None


async def test_saved_citation_must_belong_to_the_run_manifest(corpus, monkeypatch):
    client, _, path, identifier = await cited_run(corpus, monkeypatch)
    async with session_factory()() as db:
        run = await db.get(Run, UUID(identifier))
        run.outcome = {**run.outcome, "citations": [
            {"evidence_id": str(uuid4()), "quote": "secret"},
        ]}
        await db.commit()
    response = await client.get(path)
    assert response.status_code == 404 and "secret" not in response.text


@pytest.mark.parametrize("change", ["delete", "other_chat", "text"])
async def test_private_attachment_fragment_stays_bound_to_its_chat(corpus, monkeypatch, change):
    client, headers, path, identifier = await cited_run(corpus, monkeypatch)
    chunks = [{"text": "Synthetic attachment quote.", "locator": {"positions": [{"page": 7}]}}]
    file_id = uuid4()
    evidence_id = f"attachment:{file_id}:0"
    other = await new_chat(client, headers)
    async with session_factory()() as db:
        run = await db.get(Run, UUID(identifier))
        db.add(ChatFile(id=file_id, chat_id=run.chat_id, owner_id=run.user_id,
                        upload_key=uuid4(), name="Synthetic.pdf", media_type="application/pdf",
                        size_bytes=10, source_sha256="a" * 64, original_key=f"{file_id}/source",
                        canonical_sha256=chunk_digest(chunks), chunks=chunks, status="ready"))
        run.outcome = {**run.outcome, "citations": [
            {"evidence_id": evidence_id, "quote": "attachment quote."},
        ]}
        manifest = await db.scalar(select(ContextManifest).where(ContextManifest.run_id == run.id))
        manifest.evidence_ids = [evidence_id]
        await db.commit()
    response = await client.get(path)
    assert response.status_code == 200
    assert response.json()["title"] == "Synthetic.pdf"
    assert response.json()["location"] == "Страницы фрагмента: 7"
    assert response.json()["original_download_path"] is None
    async with session_factory()() as db:
        item = await db.get(ChatFile, file_id)
        if change == "delete":
            from partner_agent.infra.models import utcnow

            item.deleted_at = utcnow()
        elif change == "other_chat":
            item.chat_id = UUID(other["id"])
        else:
            item.chunks = [{"text": "Changed text.", "locator": {"line": 1}}]
            item.canonical_sha256 = chunk_digest(item.chunks)
        await db.commit()
    denied = await client.get(path)
    assert denied.status_code in (404, 409) and "attachment quote" not in denied.text
