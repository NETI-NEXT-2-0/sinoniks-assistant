"""The first project request is v3; ordinary/reference replies persist no scaffold."""

import json
from copy import deepcopy
from pathlib import Path
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


def usb_requirements():
    path = Path(__file__).parents[1] / "fixtures/network-f07.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("same_network", [False, True])
async def test_f07_preserves_direct_usb_and_rejects_a_same_network_exchange(
    plane, monkeypatch, same_network,
):
    client, headers, _ = await plane()
    configure_new(monkeypatch)
    chat = await new_chat(client, headers)
    proposal = usb_requirements()
    if same_network:
        proposal["flows"][-1]["sender_network_id"] = "plant"

    async def model(settings, messages):
        assert "Never create a USB network" in messages[0]["content"]
        return reply(proposal)

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    response = (await submit(client, headers, chat, text=(
        "Сеть цеха закрытая, офисная сеть обычная. Нужно три вещи. Первое: MQTT-брокер цеха "
        "передаёт показания датчиков на MQTT-брокер в офисе. Второе: сервер времени из офиса "
        "передаёт точное время на сервер времени цеха по NTP, UDP 123. Третье: обновления "
        "программ для цеха приносит подрядчик на флешке; после проверки внешним антивирусом "
        "они должны автоматически попадать на файловый сервер цеха по SMB, из цеха на флешку "
        "ничего не выгружается. Где ставить Синоникс и сколько правил?"
    ))).json()
    await execute_run(UUID(response["run_id"]), dispatch_generation=0)
    base = f"/api/v1/chats/{chat['id']}"
    snap = (await client.get(base + "/snapshot")).json()
    answer = next(message["content"] for message in snap["messages"]
                  if message["role"] == "assistant")
    outcome = snap["runs"][0]["outcome"]
    if same_network:
        assert outcome["code"] == "provider_invalid_design" and "design_id" not in outcome
        assert "первый проект" in answer
        return
    design = (await client.get(base + "/designs/latest", params={
        "branch_id": chat["default_branch_id"],
    })).json()["design"]
    assert design["result"]["confirmed_rules_count"] == 3
    assert design["result"]["file_users_count"] == 1
    usb = next(system for system in design["input"]["systems"] if system["kind"] == "usb")
    assert usb["network_id"] is None and usb["attachment"] == "node-A"
    assert len(design["input"]["networks"]) == 2
    file_rule = next(rule for rule in design["result"]["rules"] if rule["type"] == "file")
    assert file_rule["checks"] == ["ICAP: внешний антивирус"]


async def test_n05_followup_preserves_logs_and_finishes_the_usb_rule(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_new(monkeypatch)
    chat = await new_chat(client, headers)
    initial = data_project()
    incoming = usb_requirements()
    flow = deepcopy(incoming["flows"][-1])
    initial["flows"].append({
        **flow, "sender_ids": [], "receiver_ids": [],
        "sides": [{"network_id": side["network_id"]} for side in flow["sides"]],
    })
    turns = []

    async def model(settings, messages):
        turns.append(messages)
        if len(turns) == 1:
            return reply(initial)
        context = json.loads(messages[-1]["content"].split("Saved project conditions:\n")[1])
        existing = next(item for item in context["flows"] if item["kind"] == "file")
        networks = {item["name"]: item["id"] for item in context["networks"]}
        office, plant = networks["Офис"], networks["Цех"]
        source, destination = deepcopy(flow["sides"])
        source.update(network_id=office, actor_ids=[])
        destination["network_id"] = plant
        operations = [
            {"op": "add", "collection": "systems", "id": "usb",
             "values": {"name": "USB обновлений", "kind": "usb", "attachment": "node-A"}},
            {"op": "add", "collection": "systems", "id": "store",
             "values": {"name": "Файловый сервер", "kind": "storage", "network_id": plant}},
            {"op": "update", "collection": "flows", "id": existing["id"],
             "values": {"sender_ids": ["usb"], "receiver_ids": ["store"],
                        "sides": [source, destination]}},
        ]
        return {"choices": [{"message": {"content": json.dumps({
            "mode": "design", "answer": "Сырой текст: расчёта пока нет.",
            "citations": [], "design": None, "design_patch": {"operations": operations},
        })}}]}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    base = f"/api/v1/chats/{chat['id']}"

    async def run(text):
        admitted = (await submit(client, headers, chat, text=text)).json()
        await execute_run(UUID(admitted["run_id"]), dispatch_generation=0)
        return (await client.get(base + "/designs/latest", params={
            "branch_id": chat["default_branch_id"],
        })).json()["design"]

    first = await run(
        "Цех закрыт, офис открыт. Передавать журналы наружу по UDP 514. В цех также нужны файлы."
    )
    assert first["result"]["rules_lower_bound"] == 2
    assert first["result"]["confirmed_rules_count"] is None
    second = await run(
        "Обновления приносят на флешке. В цехе они должны автоматически попадать на файловый "
        "сервер по SMB. Проверять внешним антивирусом. Обратно файлы не передаются."
    )
    assert second["parent_id"] == first["id"] and second["revision"] == 2
    assert second["result"]["confirmed_rules_count"] == 2
    assert second["input"]["flows"][0] == first["input"]["flows"][0]
    assert second["result"]["rules"][0]["rule_id"] == first["result"]["rules"][0]["rule_id"]
    snap = (await client.get(base + "/snapshot")).json()
    final = [item["content"] for item in snap["messages"] if item["role"] == "assistant"][-1]
    assert "Минимальное число правил" in final and "USB" in final
    assert "Сырой текст" not in final


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
    ] == "succeeded"
    outcome = (await client.get(f"/api/v1/runs/{response['run_id']}")).json()["outcome"]
    assert outcome["response_status"] == "needs_clarification"
    assert outcome["task_completed"] is False
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
