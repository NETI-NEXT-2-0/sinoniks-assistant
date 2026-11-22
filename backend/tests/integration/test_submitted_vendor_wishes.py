"""Explicit free-text answers are durable before the model is called."""

import json
from uuid import UUID, uuid4

import pytest
from test_chat_data_plane import new_chat
from test_chat_data_plane import plane as plane
from test_response_execution import configure_execution

from partner_agent.runtime.provider import ProviderError
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("ending", ["cancel", "failed", "succeeded"])
async def test_explicit_wish_survives_model_failure_cancel_and_repeat(plane, monkeypatch, ending):
    client, headers, _ = await plane()
    outsider, outsider_headers, _ = await plane("admin")
    chat = await new_chat(client, headers)
    other_chat = await new_chat(client, headers)
    text = "  Нужен собственный формат CSV <&> без потери смысла.  "
    body = {"branch_id": chat["default_branch_id"], "message": "Другое: " + text,
            "vendor_wish": {"kind": "free_text", "text": text}}
    key = {**headers, "Idempotency-Key": str(uuid4())}
    path = f"/api/v1/chats/{chat['id']}"
    admitted = await client.post(path + "/runs", headers=key, json=body)
    assert admitted.status_code == 202 and admitted.json()["vendor_wish"]["text"] == text
    before = (await client.get(path + "/vendor-wishes/export")).json()
    wish = before["wishes"][0]
    assert wish["text"] == text and wish["origin"] == "user_submission"
    assert wish["message_id"] == admitted.json()["message_id"]
    assert (await client.post(path + "/runs", headers=key, json=body)).json() == admitted.json()
    assert (await client.post(path + "/runs", headers=key,
                              json={**body, "vendor_wish": None})).status_code == 409
    if ending == "cancel":
        cancelled = await client.post(f"/api/v1/runs/{admitted.json()['run_id']}/cancel",
                                       headers=headers)
        assert cancelled.status_code == 200
    else:
        configure_execution(monkeypatch)

        async def model(settings, messages):
            if ending == "failed":
                raise ProviderError("provider_request_failed")
            return {"choices": [{"message": {"content": json.dumps({
                "answer": "Свободный ответ принят", "mode": "conversation", "citations": [],
                "vendor_wishes": [{"kind": "free_text", "text": text}],
            }, ensure_ascii=False)}}]}

        monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
        result = await execute_run(UUID(admitted.json()["run_id"]), dispatch_generation=0)
        assert result["status"] == "succeeded"
        if ending == "failed":
            run = await client.get(f"/api/v1/runs/{admitted.json()['run_id']}")
            assert run.json()["outcome"]["code"] == "provider_request_failed"
            assert run.json()["outcome"]["task_completed"] is False
    # Completion/cancellation replaced outcome; the admission receipt remains immutable.
    response = await client.get(path + "/vendor-wishes/export")
    assert response.json() == before
    assert response.headers["cache-control"] == "private, no-store"
    assert (await outsider.get(path + "/vendor-wishes/export")).status_code == 404
    assert (await outsider.post(path + "/runs", json=body,
                                 headers={**outsider_headers, "Idempotency-Key": str(uuid4())})
            ).status_code == 404
    assert (await client.get(f"/api/v1/chats/{other_chat['id']}/vendor-wishes/export")
            ).json()["wishes"] == []
    assert (await client.delete(path, headers=headers)).status_code == 204
    assert (await client.get(path + "/vendor-wishes/export")).status_code == 404


@pytest.mark.parametrize("wish", [
    {"text": "not in the request"}, {"text": " "}, {"text": "x" * 501},
    {"kind": "unsupported", "text": "hello"}, {"text": "hello", "trusted": True},
])
async def test_invalid_explicit_wish_is_not_admitted(plane, wish):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    response = await client.post(f"/api/v1/chats/{chat['id']}/runs",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"branch_id": chat["default_branch_id"], "message": "hello", "vendor_wish": wish})
    assert response.status_code == 422
    exported = await client.get(f"/api/v1/chats/{chat['id']}/vendor-wishes/export")
    assert exported.json()["wishes"] == []
