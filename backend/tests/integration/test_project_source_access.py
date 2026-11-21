"""Saved derived project data must follow current source permissions."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_chat_files import send
from test_chat_files import uploaded as uploaded
from test_design_execution import reply
from test_design_revisions import design_input
from test_requirements_revisions import data_project
from test_response_execution import configure_execution
from test_response_execution import corpus as corpus

from partner_agent.documents.chat_files import process_file
from partner_agent.infra.db import session_factory
from partner_agent.infra.models_designs import DesignRevision
from partner_agent.infra.models_documents import DisclosureAnswerDeny, Document, DocumentVersion
from partner_agent.infra.models_stage2 import Run
from partner_agent.infra.settings import get_settings
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("change", ["grant", "deny", "unpublish", "version"])
async def test_saved_project_reads_exports_writes_and_ancestors_follow_sources(
    corpus,
    plane,
    monkeypatch,
    version,
    change,
):
    curator, curator_headers, client, headers, user_id, document_id, version_id, unit_id = corpus
    outsider, outsider_headers, _ = await plane(role="admin")
    configure_execution(monkeypatch)
    monkeypatch.setenv("PARTNER_DESIGN_INPUT_VERSION", str(version))
    get_settings.cache_clear()
    conditions = data_project() if version == 3 else design_input()
    secret = "Synthetic course costs 100."
    if version == 3:
        conditions["systems"][0]["name"] = secret
    else:
        conditions["exchanges"][0]["sender"] = secret
    calls = []

    async def model(settings, messages):
        calls.append(messages)
        assert secret in messages[-1]["content"]
        return reply(conditions)

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    chat = await new_chat(client, headers)
    admitted = await submit(client, headers, chat, text="Synthetic course")
    assert admitted.status_code == 202
    assert (await execute_run(UUID(admitted.json()["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    base, branch = f"/api/v1/chats/{chat['id']}/designs", chat["default_branch_id"]
    first = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    key = str(uuid4())
    saved_body = {"branch_id": branch, "parent_id": first["id"], "input": first["input"]}
    child_response = await client.post(
        base, headers={**headers, "Idempotency-Key": key}, json=saved_body
    )
    assert child_response.status_code == 201
    child = child_response.json()
    revisions = [first, child]
    upgrade_key = str(uuid4())
    if version == 2:
        upgraded = await client.post(
            base + f"/{child['id']}/upgrade",
            headers={**headers, "Idempotency-Key": upgrade_key},
            json={"branch_id": branch},
        )
        assert upgraded.status_code == 201
        revisions.append(upgraded.json())
    latest = revisions[-1]
    archives = {}
    diagrams = {}
    for row in revisions:
        suffixes = (
            ["json", "txt", "requirements"]
            if row["input"]["schema_version"] == 3
            else ["json", "txt"]
        )
        artifacts = row["artifact_sha256"]
        if row["status"] in ("conceptual", "completed"):
            assert {"drawio", "diagram"} <= artifacts.keys()
        if "drawio" in artifacts:
            suffixes.append("drawio")
        for suffix in suffixes:
            url = base + f"/{row['id']}/download/{suffix}"
            response = await client.post(url, headers=headers)
            assert response.status_code == 200
            archives[url] = response.content
        if "diagram" in artifacts:
            url = base + f"/{row['id']}/diagram"
            response = await client.get(url)
            assert response.status_code == 200
            diagrams[url] = response.json()
    # The ancestor's manifest still governs manually saved/upgraded descendants.
    if change == "grant":
        revoked = await curator.put(
            f"/api/v1/knowledge/documents/{document_id}/grants/{user_id}",
            headers=curator_headers,
            json={"expected_revision": 1, "allow_answer": False, "allow_original": False},
        )
        assert revoked.status_code == 200
    else:
        async with session_factory()() as db:
            if change == "deny":
                db.add(DisclosureAnswerDeny(unit_id=unit_id, user_id=user_id))
            elif change == "unpublish":
                (await db.get(Document, document_id)).active_version_id = None
            else:
                (await db.get(DocumentVersion, version_id)).status = "processing"
            await db.commit()
    reads = [(base + "/latest", {"branch_id": branch})]
    for row in revisions:
        reads.extend([(base + f"/{row['id']}", {}), (base + f"/{row['id']}/diagram", {})])
        if row["input"]["schema_version"] == 3:
            reads.append((base + f"/{row['id']}/questions", {"branch_id": branch}))
    for url, params in reads:
        denied = await client.get(url, params=params)
        assert denied.status_code == 409, url
        assert denied.json()["code"] == "source_context_changed"
        assert secret not in denied.text
    for url in archives:
        denied = await client.post(url, headers=headers)
        assert denied.status_code == 409
        assert denied.json()["code"] == "source_context_changed" and secret not in denied.text
    replay = await client.post(base, headers={**headers, "Idempotency-Key": key}, json=saved_body)
    assert replay.status_code == 409 and replay.json()["code"] == "source_context_changed"
    fresh = await client.post(
        base,
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"branch_id": branch, "parent_id": latest["id"], "input": latest["input"]},
    )
    assert fresh.status_code == 409 and fresh.json()["code"] == "source_context_changed"
    upgrade = await client.post(
        base + f"/{child['id']}/upgrade",
        headers={**headers, "Idempotency-Key": upgrade_key},
        json={"branch_id": branch},
    )
    assert upgrade.status_code == 409 and upgrade.json()["code"] == "source_context_changed"
    current_input = latest["input"]
    typed = await client.post(
        f"/api/v1/chats/{chat['id']}/runs",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={
            "branch_id": branch,
            "message": "Synthetic field answer",
            "project_answer": {
                "base_revision_id": latest["id"],
                "path": "/networks/0/name",
                "target_id": current_input["networks"][0]["id"],
                "unknown": True,
            },
        },
    )
    assert typed.status_code == 409 and typed.json()["code"] == "source_context_changed"
    assert (await outsider.get(base + f"/{first['id']}")).status_code == 404
    assert (await outsider.post(next(iter(archives)), headers=outsider_headers)).status_code == 404
    async with session_factory()() as db:
        stored = list(
            (
                await db.scalars(
                    select(DesignRevision).where(DesignRevision.chat_id == UUID(chat["id"]))
                )
            ).all()
        )
        assert len(stored) == len(revisions)
        assert (
            await db.scalar(
                select(func.count()).select_from(Run).where(Run.chat_id == UUID(chat["id"]))
            )
        ) == 1
        frozen = {str(row.id): deepcopy(row.artifacts) for row in stored}
    assert len(calls) == 1
    # Independent operator input does not inherit this other chat's source lineage.
    independent = await new_chat(client, headers)
    created = await client.post(
        f"/api/v1/chats/{independent['id']}/designs",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"branch_id": independent["default_branch_id"], "input": data_project()},
    )
    assert created.status_code == 201
    if change == "grant":
        restored = await curator.put(
            f"/api/v1/knowledge/documents/{document_id}/grants/{user_id}",
            headers=curator_headers,
            json={"expected_revision": 2, "allow_answer": True, "allow_original": False},
        )
        assert restored.status_code == 200
    else:
        async with session_factory()() as db:
            if change == "deny":
                await db.delete(await db.get(DisclosureAnswerDeny, (unit_id, user_id)))
            elif change == "unpublish":
                (await db.get(Document, document_id)).active_version_id = version_id
            else:
                (await db.get(DocumentVersion, version_id)).status = "ready_for_review"
            await db.commit()
    for url, original in archives.items():
        response = await client.post(url, headers=headers)
        assert response.status_code == 200 and response.content == original
        assert response.headers["cache-control"] == "private, no-store"
    for url, original in diagrams.items():
        response = await client.get(url)
        assert response.status_code == 200 and response.json() == original
        assert response.headers["cache-control"] == "private, no-store"
    async with session_factory()() as db:
        for identifier, artifacts in frozen.items():
            assert (await db.get(DesignRevision, UUID(identifier))).artifacts == artifacts


@pytest.mark.storage
@pytest.mark.parametrize("version", [2, 3])
async def test_deleted_chat_attachment_blocks_derived_project(uploaded, monkeypatch, version):
    client, headers, _, chat, file, _ = uploaded
    assert await process_file(UUID(file["id"]))
    configure_execution(monkeypatch)
    monkeypatch.setenv("PARTNER_DESIGN_INPUT_VERSION", str(version))
    get_settings.cache_clear()
    conditions = data_project() if version == 3 else design_input()
    calls = []

    async def model(settings, messages):
        calls.append(messages)
        assert "Registration closes on 20 November." in messages[-1]["content"]
        return reply(conditions)

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    admitted = await send(client, headers, chat, [file["id"]], "Спроектируй обмен по вложению")
    assert admitted.status_code == 202
    assert (await execute_run(UUID(admitted.json()["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    base = f"/api/v1/chats/{chat['id']}/designs"
    params = {"branch_id": chat["default_branch_id"]}
    row = (await client.get(base + "/latest", params=params)).json()["design"]
    child = await client.post(
        base, headers={**headers, "Idempotency-Key": str(uuid4())},
        json={
            "branch_id": chat["default_branch_id"], "parent_id": row["id"], "input": row["input"],
        },
    )
    assert child.status_code == 201
    child_id = child.json()["id"]
    export = base + f"/{child_id}/download/json"
    assert (await client.post(export, headers=headers)).status_code == 200
    assert (await client.delete(
        f"/api/v1/chats/{chat['id']}/attachments/{file['id']}", headers=headers,
    )).status_code == 204
    for url, query in [(base + "/latest", params), (base + f"/{row['id']}", {}),
                       (base + f"/{child_id}", {}), (base + f"/{child_id}/diagram", {})]:
        denied = await client.get(url, params=query)
        assert denied.status_code == 409 and denied.json()["code"] == "source_context_changed"
    denied = await client.post(export, headers=headers)
    assert denied.status_code == 409 and denied.json()["code"] == "source_context_changed"
    assert len(calls) == 1
