"""Rejected model payloads become safe dialogue, never project writes or false success."""

import json
from uuid import UUID, uuid4

import pytest
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_design_revisions import design_input
from test_response_execution import configure_execution

from partner_agent.runtime.provider import ProviderError
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("failure", ["source", "json", "design", "timeout"])
async def test_rejected_payload_is_not_exposed_and_saved_project_is_unchanged(
    plane, monkeypatch, failure
):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    original = (
        await client.post(
            base,
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"branch_id": chat["default_branch_id"], "input": design_input()},
        )
    ).json()
    calls = []

    async def model(settings, messages):
        calls.append(messages)
        if failure == "timeout":
            raise ProviderError("provider_request_timeout")
        body = {
            "answer": "REJECTED_PRIVATE_MODEL_TEXT",
            "mode": "design",
            "design": design_input(),
            "citations": [],
        }
        if failure == "source":
            body["citations"] = [{"evidence_id": "unavailable-private-id"}]
        if failure == "design":
            body["design"] = {"network_a": ["invalid"]}
        content = "not JSON" if failure == "json" else json.dumps(body)
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    admitted = (await submit(client, headers, chat, text="Измени схему")).json()
    assert (await execute_run(UUID(admitted["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    outcome = next(r["outcome"] for r in snapshot["runs"] if r["id"] == admitted["run_id"])
    assert outcome["answer_mode"] == "insufficient" and outcome["task_completed"] is False
    assert outcome["response_status"] == "needs_clarification" and outcome["citations"] == []
    assert "REJECTED_PRIVATE_MODEL_TEXT" not in str(snapshot)
    assert any(m["role"] == "assistant" and m["content"] for m in snapshot["messages"])
    after = (
        await client.get(base + "/latest", params={"branch_id": chat["default_branch_id"]})
    ).json()["design"]
    assert after["id"] == original["id"] and after["artifact_sha256"] == original["artifact_sha256"]
    assert len(calls) == 1  # No hidden paid repair call.
    ledger = (await client.get(f"/api/v1/chats/{chat['id']}/accounting")).json()
    assert ledger["turns"][0]["task_completed"] is False
