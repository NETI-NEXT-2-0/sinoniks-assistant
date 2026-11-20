"""Real PG/Garage private files; fake provider transport never leaves the process."""
import json
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from test_chat_data_plane import new_chat
from test_chat_data_plane import plane as plane
from test_response_execution import configure_execution

from partner_agent.documents.chat_files import process_file
from partner_agent.documents.ingest import storage
from partner_agent.infra.db import session_factory
from partner_agent.infra.models import User
from partner_agent.infra.models_chat_files import ChatFile
from partner_agent.retrieval.service import search_knowledge
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration
SOURCE = b"Registration closes on 20 November.\nPrivate attachment context."


@pytest_asyncio.fixture
async def uploaded(plane):
    client, headers, owner = await plane()
    chat = await new_chat(client, headers)
    key = str(uuid4())
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/attachments?name=sample.txt",
        content=SOURCE, headers={**headers, "Content-Type": "text/plain", "Idempotency-Key": key},
    )
    assert response.status_code == 202
    file = response.json()
    yield client, headers, owner, chat, file, key
    async with session_factory()() as db:
        item = await db.get(ChatFile, UUID(file["id"]))
        adapter = storage()
        try:
            adapter.delete(adapter.originals_bucket, item.original_key)
        finally:
            adapter.client.close()


async def send(client, headers, chat, files, message="When does registration close?"):
    return await client.post(
        f"/api/v1/chats/{chat['id']}/runs",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"branch_id": chat["default_branch_id"], "message": message,
              "attachment_ids": files, "web_mode": "off"},
    )


@pytest.mark.storage
async def test_chat_upload_is_idempotent_private_and_must_be_ready(uploaded, plane):
    client, headers, owner, chat, file, key = uploaded
    path = f"/api/v1/chats/{chat['id']}/attachments?name=sample.txt"
    replay = await client.post(path, content=SOURCE, headers={
        **headers, "Content-Type": "text/plain", "Idempotency-Key": key,
    })
    assert replay.status_code == 202 and replay.json()["id"] == file["id"]
    conflict = await client.post(path, content=b"different", headers={
        **headers, "Content-Type": "text/plain", "Idempotency-Key": key,
    })
    assert conflict.status_code == 409
    other, other_headers, _ = await plane()
    assert (await other.get(f"/api/v1/chats/{chat['id']}/attachments")).status_code == 404
    assert (await other.delete(
        f"/api/v1/chats/{chat['id']}/attachments/{file['id']}", headers=other_headers
    )).status_code == 404
    second = await new_chat(client, headers)
    assert (await send(client, headers, second, [file["id"]])).status_code == 404
    pending = await send(client, headers, chat, [file["id"]])
    assert pending.status_code == 409 and pending.json()["code"] == "attachment_not_ready"
    assert await process_file(UUID(file["id"]))
    assert (await send(client, headers, chat, [file["id"]])).status_code == 202
    async with session_factory()() as db:
        actor = await db.get(User, owner)
        assert await search_knowledge(db, actor, "registration") == []


@pytest.mark.storage
@pytest.mark.parametrize("revoke", [False, True])
async def test_attachment_answer_and_source_revocation(uploaded, monkeypatch, revoke):
    client, headers, _, chat, file, _ = uploaded
    assert await process_file(UUID(file["id"]))
    configure_execution(monkeypatch)
    seen = []

    async def local_completion(settings, messages):
        seen.append(messages)
        assert "Registration closes on 20 November." in messages[-1]["content"]
        if revoke:
            response = await client.delete(
                f"/api/v1/chats/{chat['id']}/attachments/{file['id']}", headers=headers
            )
            assert response.status_code == 204
        content = {"answer": "Registration closes on 20 November.", "mode": "grounded",
                   "citations": [{"evidence_id": f"attachment:{file['id']}:0",
                                  "quote": "Registration closes on 20 November."}]}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", local_completion)
    response = await send(client, headers, chat, [file["id"]])
    first = response.json()
    result = await execute_run(UUID(first["run_id"]), dispatch_generation=0)
    assert result["status"] == ("failed" if revoke else "succeeded")
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    if revoke:
        assert snapshot["runs"][0]["outcome"]["code"] == "source_context_changed"
        assert not any(item["role"] == "assistant" for item in snapshot["messages"])
    else:
        assert snapshot["runs"][0]["attachments"][0]["name"] == "sample.txt"
        second = (await send(client, headers, chat, [], "А когда заканчивается запись?")).json()
        assert (await execute_run(UUID(second["run_id"]), dispatch_generation=0))[
            "status"
        ] == "succeeded"
        assert len(seen) == 2 and seen[1][1]["role"] == "user"
        assert seen[1][2]["content"] == "Registration closes on 20 November."


async def test_conversation_without_sources_remains_in_own_history(plane, monkeypatch):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    configure_execution(monkeypatch)
    seen = []

    async def local_completion(settings, messages):
        seen.append(messages)
        content = {"answer": "Привет! Чем помочь?", "mode": "conversation", "citations": []}
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", local_completion)
    for question in ("Привет", "Как дела?"):
        admitted = (await send(client, headers, chat, [], question)).json()
        assert (await execute_run(UUID(admitted["run_id"]), dispatch_generation=0))[
            "status"
        ] == "succeeded"
    assert seen[1][1]["content"] == "Привет"
    assert seen[1][2]["content"] == "Привет! Чем помочь?"
