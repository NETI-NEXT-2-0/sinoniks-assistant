"""Immutable v3 history, chat updates and fencing on real PostgreSQL."""

import json
from copy import deepcopy
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_design_execution import reply
from test_design_revisions import design_input
from test_response_execution import configure_execution

from partner_agent.requirements_service import without_sources
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


def data_project():
    return {
        "schema_version": 3,
        "id": "project-1",
        "networks": [
            {"id": "office", "name": "Офис", "trust": "open"},
            {"id": "plant", "name": "Цех", "trust": "closed"},
        ],
        "systems": [
            {"id": "sender", "name": "Журналы", "network_id": "plant"},
            {"id": "receiver", "name": "SIEM", "network_id": "office"},
        ],
        "flows": [
            {
                "id": "logs",
                "kind": "data",
                "sender_network_id": "plant",
                "receiver_network_id": "office",
                "sender_ids": ["sender"],
                "receiver_ids": ["receiver"],
                "purpose": "Журналы",
                "mode": "one-way",
                "protocol": "UDP",
                "destination_port": 514,
            }
        ],
    }


async def save(client, headers, base, branch, conditions, parent=None, key=None):
    response = await client.post(
        base,
        json={"branch_id": branch, "parent_id": parent, "input": conditions},
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
    )
    return response


async def test_requirements_revision_ids_retry_conflict_archives_and_access(plane):
    client, headers, _ = await plane()
    admin, admin_headers, _ = await plane("admin")
    chat = await new_chat(client, headers)
    base, branch = f"/api/v1/chats/{chat['id']}/designs", chat["default_branch_id"]
    key = str(uuid4())
    first = await save(client, headers, base, branch, data_project(), key=key)
    assert first.status_code == 201, first.text
    row = first.json()
    assert row["status"] == "completed" and row["result"]["confirmed_rules_count"] == 1
    archive = (await client.post(base + f"/{row['id']}/download/json", headers=headers)).content
    assert json.loads(archive)["schema_version"] == "1.1"
    assert row["input"]["flows"][0]["sources"][0]["kind"] == "operator"
    scene_url = base + f"/{row['id']}/diagram"
    drawio_url = base + f"/{row['id']}/download/drawio"
    scene_response = await client.get(scene_url)
    drawio_response = await client.post(drawio_url, headers=headers)
    assert scene_response.status_code == drawio_response.status_code == 200
    scene = scene_response.json()
    drawio = drawio_response.content
    assert scene["revision_id"] == row["id"]
    assert scene["scene"]["profile"] == "calculated-sinoniks-19"
    assert row["artifact_sha256"]["drawio"] == sha256(drawio).hexdigest()
    assert (await admin.get(scene_url)).status_code == 404
    assert (await admin.post(drawio_url, headers=admin_headers)).status_code == 404
    assert (await save(client, headers, base, branch, data_project(), key=key)).json() == row
    changed = data_project()
    changed["networks"][0]["name"] = "Новый офис"
    assert (await save(client, headers, base, branch, changed, key=key)).status_code == 409
    second = await save(client, headers, base, branch, changed, parent=row["id"])
    assert second.status_code == 201, second.text
    assert second.json()["input"]["flows"][0]["id"] == row["input"]["flows"][0]["id"]
    assert (await save(client, headers, base, branch, changed, parent=row["id"])).status_code == 409
    assert (
        await client.post(base + f"/{row['id']}/download/json", headers=headers)
    ).content == archive
    assert (await client.get(scene_url)).json() == scene
    assert (await client.post(drawio_url, headers=headers)).content == drawio
    assert (await admin.get(base + f"/{row['id']}")).status_code == 404
    assert (
        await admin.post(
            base,
            headers={**admin_headers, "Idempotency-Key": str(uuid4())},
            json={"branch_id": branch, "input": data_project()},
        )
    ).status_code == 404
    assert (
        await client.post(
            base,
            headers={"Idempotency-Key": str(uuid4())},
            json={"branch_id": branch, "input": data_project()},
        )
    ).status_code == 403
    assert (
        await save(client, headers, base, branch, design_input(), parent=second.json()["id"])
    ).json()["code"] == "design_downgrade_rejected"


async def test_explicit_upgrade_uses_original_input_keeps_old_artifacts_and_is_idempotent(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    base, branch = f"/api/v1/chats/{chat['id']}/designs", chat["default_branch_id"]
    first = (await save(client, headers, base, branch, design_input())).json()
    old = (await client.post(base + f"/{first['id']}/download/drawio", headers=headers)).content
    assert (await save(client, headers, base, branch, data_project(), parent=first["id"])).json()[
        "code"
    ] == "design_upgrade_required"
    key = str(uuid4())
    url = base + f"/{first['id']}/upgrade"
    upgraded = await client.post(
        url, headers={**headers, "Idempotency-Key": key}, json={"branch_id": branch}
    )
    assert upgraded.status_code == 201, upgraded.text
    row = upgraded.json()
    assert row["parent_id"] == first["id"] and row["input"]["schema_version"] == 3
    assert all(network["trust"] is None for network in row["input"]["networks"])
    assert row["input"]["legacy"]["firewall"] == "B"
    assert (
        await client.post(
            url, headers={**headers, "Idempotency-Key": key}, json={"branch_id": branch}
        )
    ).json() == row
    assert (
        await client.post(base + f"/{first['id']}/download/drawio", headers=headers)
    ).content == old


async def test_chat_clarification_preserves_ids_and_never_claims_model_counts(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    base, branch = f"/api/v1/chats/{chat['id']}/designs", chat["default_branch_id"]
    conditions = data_project()
    conditions["networks"][0]["trust"] = None
    first = (await save(client, headers, base, branch, conditions)).json()
    proposed = without_sources(first["input"])
    proposed["networks"][0]["trust"] = "open"
    proposed["systems"].append(
        {
            "id": "temporary-new",
            "name": "Резервный сервер",
            "network_id": "office",
            "kind": "system",
        }
    )
    proposed["flows"].append(
        deepcopy(proposed["flows"][0])
        | {"id": "temporary-flow", "receiver_ids": ["temporary-new"], "purpose": "Копия журналов"}
    )
    calls = []

    async def model(settings, messages):
        calls.append(messages)
        assert "REQUIREMENTS v3" in messages[0]["content"]
        assert '"sources"' not in messages[-1]["content"]
        return reply(proposed)

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    admitted = (
        await submit(client, headers, chat, text="Офис открытый; добавь резервный сервер")
    ).json()
    result = await execute_run(UUID(admitted["run_id"]), dispatch_generation=0)
    assert result["status"] == "succeeded"
    current = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert current["parent_id"] == first["id"] and current["status"] == "completed"
    assert current["result"]["confirmed_rules_count"] == 2
    assert "requirements" in current["artifact_sha256"]
    assert current["input"]["flows"][0]["id"] == "logs"
    new_system = current["input"]["systems"][-1]
    new_flow = current["input"]["flows"][-1]
    assert new_system["id"] != "temporary-new" and new_flow["id"] != "temporary-flow"
    assert new_flow["receiver_ids"] == [new_system["id"]]
    assert new_flow["sources"][-1]["kind"] == "message"
    detail = (await client.get(f"/api/v1/runs/{admitted['run_id']}")).json()
    assert detail["outcome"]["design_status"] == "completed"
    assert len(calls) == 1
    # The model's fake '999' count is replaced with the server's draft explanation.
    response = await client.get(
        f"/api/v1/chats/{chat['id']}/snapshot", params={"branch_id": branch}
    )
    answers = [
        message["content"]
        for message in response.json()["messages"]
        if message["role"] == "assistant"
    ]
    assert len(answers) == 1 and "999" not in answers[0]
    assert "Минимальное число правил Синоникса" in answers[0]


async def test_stale_v3_model_update_cannot_overwrite_manual_revision(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    base, branch = f"/api/v1/chats/{chat['id']}/designs", chat["default_branch_id"]
    first = (await save(client, headers, base, branch, data_project())).json()
    admitted = (await submit(client, headers, chat, text="Переименуй офис")).json()
    changed = without_sources(first["input"])
    changed["networks"][0]["name"] = "Ручная правка"
    newer = {}

    async def model(settings, messages):
        newer.update(
            (await save(client, headers, base, branch, changed, parent=first["id"])).json()
        )
        return reply(without_sources(first["input"]))

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    assert (await execute_run(UUID(admitted["run_id"]), dispatch_generation=0))[
        "status"
    ] == "failed"
    current = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert current["id"] == newer["id"]
    run = (await client.get(f"/api/v1/runs/{admitted['run_id']}")).json()
    assert run["outcome"]["code"] == "design_revision_conflict"


@pytest.mark.parametrize("kind", ["provenance", "downgrade", "reference", "explain"])
async def test_v3_provenance_modes_and_read_only_explanation(plane, monkeypatch, kind):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    base, branch = f"/api/v1/chats/{chat['id']}/designs", chat["default_branch_id"]
    first = (await save(client, headers, base, branch, data_project())).json()

    async def model(settings, messages):
        if kind == "explain":
            assert '"has_architecture":true' in messages[-1]["content"]
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "mode": "project_explanation",
                                    "answer": "Сохранён поток журналов.",
                                    "citations": [],
                                    "design": None,
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            }
        conditions = without_sources(first["input"])
        if kind == "provenance":
            conditions["flows"][0]["sources"] = [{"kind": "document", "reference": "foreign"}]
        if kind == "reference":
            assert "Saved project conditions" not in messages[-1]["content"]
            return reply(design_input())
        return reply(design_input() if kind == "downgrade" else conditions)

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/runs",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={
            "branch_id": branch,
            "message": "Поясни проект",
            "request_mode": "reference" if kind == "reference" else "auto",
        },
    )
    assert response.status_code == 202, response.text
    result = await execute_run(UUID(response.json()["run_id"]), dispatch_generation=0)
    assert result["status"] == ("succeeded" if kind == "explain" else "failed")
    latest = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert latest == first
