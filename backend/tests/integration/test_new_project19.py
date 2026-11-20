"""The first project request is v3; ordinary/reference replies persist no scaffold."""

import json
from uuid import UUID

import pytest
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_design_execution import reply
from test_design_revisions import design_input
from test_requirements_revisions import data_project, save
from test_response_execution import configure_execution

from partner_agent.infra.settings import get_settings
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


def configure_new(monkeypatch):
    configure_execution(monkeypatch)
    monkeypatch.setenv("PARTNER_DESIGN_INPUT_VERSION", "3")
    get_settings.cache_clear()


async def test_first_request_saves_v3_without_a_preliminary_editor_save(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_new(monkeypatch)
    chat = await new_chat(client, headers)
    seen = []

    async def model(settings, messages):
        seen.append(messages)
        assert "UNSAVED NEW PROJECT" in messages[0]["content"]
        assert "New draft requirements (not yet saved):" in messages[-1]["content"]
        assert "Saved project conditions:" not in messages[-1]["content"]
        return reply(data_project())

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    response = (await submit(client, headers, chat, text="Цех отправляет журналы в офис")).json()
    assert (await execute_run(UUID(response["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    base = f"/api/v1/chats/{chat['id']}/designs"
    current = (
        await client.get(
            base + "/latest",
            params={
                "branch_id": chat["default_branch_id"],
            },
        )
    ).json()["design"]
    assert current["revision"] == 1 and current["parent_id"] is None
    assert current["input"]["schema_version"] == 3
    assert current["input"]["id"] != "project-1"
    assert current["status"] == "completed" and current["result"]["confirmed_rules_count"] == 1
    assert current["input"]["flows"][0]["sources"][0]["reference"] == response["message_id"]
    assert len(seen) == 1
    run = (await client.get(f"/api/v1/runs/{response['run_id']}")).json()
    assert run["outcome"]["design_input_version"] == 3


@pytest.mark.parametrize("mode", ["conversation", "insufficient"])
async def test_ordinary_or_insufficient_answer_does_not_save_empty_requirements(
    plane, monkeypatch, mode
):
    client, headers, _ = await plane()
    configure_new(monkeypatch)
    chat = await new_chat(client, headers)

    async def model(settings, messages):
        assert "UNSAVED NEW PROJECT" in messages[0]["content"]
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "mode": mode,
                                "answer": "Расскажите о своей задаче.",
                                "citations": [],
                                "design": None,
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    response = (await submit(client, headers, chat, text="Привет")).json()
    assert (await execute_run(UUID(response["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    assert (
        await client.get(
            f"/api/v1/chats/{chat['id']}/designs/latest",
            params={
                "branch_id": chat["default_branch_id"],
            },
        )
    ).json()["design"] is None


async def test_existing_legacy_project_keeps_its_version_after_default_changes(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_new(monkeypatch)
    chat = await new_chat(client, headers)
    base, branch = f"/api/v1/chats/{chat['id']}/designs", chat["default_branch_id"]
    first = (await save(client, headers, base, branch, design_input())).json()

    async def model(settings, messages):
        assert "UNSAVED NEW PROJECT" not in messages[0]["content"]
        assert "Saved project conditions:" in messages[-1]["content"]
        return reply(design_input())

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    response = (await submit(client, headers, chat, text="Оставь прежние условия")).json()
    assert (await execute_run(UUID(response["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    current = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert current["input"]["schema_version"] == 2 and current["parent_id"] == first["id"]


async def test_new_project_cannot_accept_a_silently_downgraded_model_reply(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_new(monkeypatch)
    chat = await new_chat(client, headers)

    async def model(settings, messages):
        return reply(design_input())

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    response = (await submit(client, headers, chat, text="Спроектируй обмен")).json()
    assert (await execute_run(UUID(response["run_id"]), dispatch_generation=0))[
        "status"
    ] == "failed"
    current = (
        await client.get(
            f"/api/v1/chats/{chat['id']}/designs/latest",
            params={
                "branch_id": chat["default_branch_id"],
            },
        )
    ).json()["design"]
    assert current is None


async def test_regenerate_empty_base_keeps_original_schema_after_default_changes(
    plane, monkeypatch
):
    from test_design_branches import fork

    client, headers, _ = await plane()
    configure_new(monkeypatch)
    chat = await new_chat(client, headers)
    seen = []

    async def model(settings, messages):
        seen.append(messages)
        assert "UNSAVED NEW PROJECT" in messages[0]["content"]
        return reply(data_project())

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    admitted = (await submit(client, headers, chat, text="Описание инфраструктуры")).json()
    assert (await execute_run(UUID(admitted["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    assistant = next(item for item in snapshot["messages"] if item["role"] == "assistant")
    monkeypatch.setenv("PARTNER_DESIGN_INPUT_VERSION", "2")
    get_settings.cache_clear()
    child = await fork(client, headers, chat, assistant["id"])
    assert child.status_code == 201, child.text
    child = child.json()
    assert (await execute_run(UUID(child["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    current = (
        await client.get(
            f"/api/v1/chats/{chat['id']}/designs/latest",
            params={
                "branch_id": child["branch_id"],
            },
        )
    ).json()["design"]
    assert current["input"]["schema_version"] == 3 and current["parent_id"] is None
    assert len(seen) == 2
