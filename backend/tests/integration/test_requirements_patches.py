"""Addressed model edits use the real revision/ownership/recalculation path."""

import json
from copy import deepcopy
from uuid import UUID

import pytest
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_requirements_revisions import data_project, save
from test_response_execution import configure_execution

from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


async def arrange(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    base, branch = f"/api/v1/chats/{chat['id']}/designs", chat["default_branch_id"]
    first = (await save(client, headers, base, branch, data_project())).json()
    return client, headers, chat, base, branch, first


def provider(monkeypatch, operations, before_reply=None):
    async def model(settings, messages):
        assert "EDIT CONTRACT" in messages[0]["content"]
        if before_reply is not None:
            await before_reply()
        return {"choices": [{"message": {"content": json.dumps({
            "answer": "Изменил", "citations": [], "mode": "design", "design": None,
            "design_patch": {"operations": operations},
        })}}], "usage": {"prompt_tokens": 100, "completion_tokens": 30}}
    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)


async def test_patch_add_rename_remove_recalculates_and_keeps_archive(plane, monkeypatch):
    client, headers, chat, base, branch, first = await arrange(plane, monkeypatch)
    archive = (await client.post(base + f"/{first['id']}/download/drawio",
                                 headers=headers)).content
    current = first
    for operations in [
        [{"op": "add", "collection": "systems", "id": "extra", "values": {
            "name": "Резерв", "network_id": "office"}},
         {"op": "update", "collection": "flows", "id": "logs",
          "values": {"receiver_ids": ["receiver", "extra"]}}],
        [{"op": "update", "collection": "systems", "id": "PLACEHOLDER",
          "values": {"name": "Архив"}}],
        [{"op": "remove", "collection": "systems", "id": "PLACEHOLDER"},
         {"op": "update", "collection": "flows", "id": "logs",
          "values": {"receiver_ids": ["receiver"]}}],
    ]:
        previous = current
        for op in operations:
            if op["id"] == "PLACEHOLDER":
                op["id"] = next(row["id"] for row in current["input"]["systems"]
                                if row["name"] in ("Резерв", "Архив"))
        provider(monkeypatch, operations)
        accepted = await submit(client, headers, chat, text="Изменить проект")
        assert accepted.status_code == 202
        await execute_run(UUID(accepted.json()["run_id"]), dispatch_generation=0)
        run = (await client.get('/api/v1/runs/' + accepted.json()["run_id"])).json()
        assert run["status"] == "succeeded" and not run["outcome"].get("code"), run
        current = (await client.get(
            base + "/latest", params={"branch_id": branch},
        )).json()["design"]
        assert current["parent_id"] == previous["id"]
        assert current["revision"] == previous["revision"] + 1
        assert current["input"]["networks"] == first["input"]["networks"]
        assert current["input"]["systems"][0] == first["input"]["systems"][0]
        assert current["result"]["confirmed_rules_count"] == (
            1 if operations[0]["op"] == "remove" else 2
        )
        assert (await client.post(base + f"/{first['id']}/download/drawio",
                                  headers=headers)).content == archive


async def test_stale_patch_cannot_replace_manual_saved_revision(plane, monkeypatch):
    client, headers, chat, base, branch, first = await arrange(plane, monkeypatch)
    accepted = (await submit(client, headers, chat, text="Переименовать")).json()
    changed = deepcopy(first["input"])
    changed["networks"][0]["name"] = "Ручная версия"
    manual = {}
    async def concurrent_save():
        manual.update((await save(
            client, headers, base, branch, changed, parent=first["id"],
        )).json())
    provider(monkeypatch, [{"op": "update", "collection": "systems", "id": "sender",
                            "values": {"name": "Поздний ответ"}}], concurrent_save)
    await execute_run(UUID(accepted["run_id"]), dispatch_generation=0)
    latest = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert latest["id"] == manual["id"]
    assert latest["input"] == manual["input"]


async def test_invalid_patch_does_not_save_partial_revision(plane, monkeypatch):
    client, headers, chat, base, branch, first = await arrange(plane, monkeypatch)
    provider(monkeypatch, [
        {"op": "update", "collection": "systems", "id": "sender",
         "values": {"name": "Не сохранять"}},
        {"op": "remove", "collection": "systems", "id": "receiver"},
    ])
    accepted = (await submit(client, headers, chat, text="Изменить")).json()
    await execute_run(UUID(accepted["run_id"]), dispatch_generation=0)
    run = (await client.get('/api/v1/runs/' + accepted["run_id"])).json()
    assert run["status"] == "succeeded"
    assert run["outcome"]["task_completed"] is False
    latest = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert latest == first


async def test_server_only_edit_is_visible_and_does_not_claim_a_new_exchange(plane, monkeypatch):
    client, headers, chat, base, branch, first = await arrange(plane, monkeypatch)
    provider(monkeypatch, [{"op": "add", "collection": "systems", "id": "garage",
                            "values": {"name": "Сервер сервиса", "network_id": "office"}}])
    accepted = (await submit(client, headers, chat, text="Добавить сервер и обмен")).json()
    await execute_run(UUID(accepted["run_id"]), dispatch_generation=0)
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    answer = next(m["content"] for m in snapshot["messages"] if m["role"] == "assistant")
    assert "Сервер сервиса" in answer and "Новые обмены не сохранены" in answer
    outcome = next(r["outcome"] for r in snapshot["runs"] if r["id"] == accepted["run_id"])
    assert len(outcome["design_changes"]["new_systems_without_exchange"]) == 1
    latest = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert latest["input"]["flows"] == first["input"]["flows"]
    scene = (await client.get(base + f"/{latest['id']}/diagram")).json()["scene"]
    assert "Сервер сервиса" in json.dumps(scene, ensure_ascii=False)


async def test_compound_edit_preserves_tcp_intent_until_direction_is_known(plane, monkeypatch):
    client, headers, chat, base, branch, first = await arrange(plane, monkeypatch)
    provider(monkeypatch, [
        {"op": "add", "collection": "systems", "id": "garage",
         "values": {"name": "Сервер сервиса", "network_id": "office"}},
        {"op": "add", "collection": "flows", "id": "service-channel",
         "values": {"kind": "data", "protocol": "TCP", "mode": None,
                    "purpose": "Между сервером сервиса и компьютером цеха"}},
    ])
    accepted = (await submit(client, headers, chat, text="Добавить сервер с TCP-обменом")).json()
    await execute_run(UUID(accepted["run_id"]), dispatch_generation=0)
    latest = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert len(latest["input"]["flows"]) == 2
    assert latest["input"]["flows"][0] == first["input"]["flows"][0]
    assert latest["input"]["flows"][1]["protocol"] == "TCP"
    assert latest["input"]["flows"][1]["sender_network_id"] is None
    assert latest["questions"] and latest["status"] != "completed"
    assert latest["result"]["confirmed_rules_count"] is None
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    answer = next(m["content"] for m in snapshot["messages"] if m["role"] == "assistant")
    assert "Уточните:" in answer and "TCP" in answer


async def test_finishing_exchange_for_existing_server_does_not_duplicate_it(plane, monkeypatch):
    client, headers, chat, base, branch, first = await arrange(plane, monkeypatch)
    added = deepcopy(first["input"])
    added["systems"].append({"id": "garage", "name": "Сервер сервиса", "network_id": "office"})
    existing = (await save(client, headers, base, branch, added, parent=first["id"])).json()
    provider(monkeypatch, [{"op": "add", "collection": "flows", "id": "tcp-service",
                            "values": {"kind": "data", "protocol": "TCP", "mode": "two-way",
                                       "destination_port": 8443, "sender_network_id": "plant",
                                       "receiver_network_id": "office", "sender_ids": ["sender"],
                                       "receiver_ids": ["garage"], "purpose": "Диагностика"}}])
    accepted = (await submit(client, headers, chat, text="Завершить обмен с сервером")).json()
    await execute_run(UUID(accepted["run_id"]), dispatch_generation=0)
    latest = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert latest["parent_id"] == existing["id"]
    assert len(latest["input"]["systems"]) == 3
    assert len(latest["input"]["flows"]) == 2
    assert latest["result"]["confirmed_rules_count"] == 2
