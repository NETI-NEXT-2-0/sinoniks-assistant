"""Readiness belongs to the saved owner revision, not a mutable chat badge."""

from copy import deepcopy
from uuid import UUID

import pytest
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_requirements_patches import provider
from test_requirements_revisions import data_project, save
from test_response_execution import configure_execution

from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


async def test_readiness_follows_revision_and_keeps_expert_contract_and_old_archive(plane):
    client, headers, _ = await plane()
    other, other_headers, _ = await plane()
    chat = await new_chat(client, headers)
    base, branch = f"/api/v1/chats/{chat['id']}/designs", chat["default_branch_id"]
    raw = data_project()
    raw["flows"][0]["mode"] = None
    first = (await save(client, headers, base, branch, raw)).json()
    assert first["result"]["readiness"]["status"] == "needs_clarification"
    native = (await client.post(base + f"/{first['id']}/download/drawio", headers=headers)).content
    assert "Нужны уточнения" in native.decode()
    complete = deepcopy(first["input"])
    complete["flows"][0]["mode"] = "one-way"
    second = (await save(client, headers, base, branch, complete, parent=first["id"])).json()
    assert second["result"]["readiness"]["status"] == "checked"
    old = (await client.get(base + f"/{first['id']}")).json()
    assert old["result"]["readiness"] == first["result"]["readiness"]
    assert (await client.post(base + f"/{first['id']}/download/drawio",
                              headers=headers)).content == native
    expert = (await client.post(base + f"/{second['id']}/download/json", headers=headers)).json()
    assert expert["schema_version"] == "1.1" and "readiness" not in expert
    assert (await other.get(base + f"/{second['id']}")).status_code in (403, 404)
    assert (await other.post(base + f"/{second['id']}/download/drawio",
                             headers=other_headers)).status_code in (403, 404)


async def test_chat_card_gets_report_of_its_own_saved_revision(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    base, branch = f"/api/v1/chats/{chat['id']}/designs", chat["default_branch_id"]
    first = (await save(client, headers, base, branch, data_project())).json()
    provider(monkeypatch, [{"op": "update", "collection": "flows", "id": "logs",
                            "values": {"mode": None}}])
    accepted = (await submit(client, headers, chat, text="Неизвестен режим обмена")).json()
    await execute_run(UUID(accepted["run_id"]), dispatch_generation=0)
    run = (await client.get('/api/v1/runs/' + accepted["run_id"])).json()
    assert run["outcome"]["design_readiness"]["status"] == "needs_clarification"
    old = (await client.get(base + f"/{first['id']}")).json()
    assert old["result"]["readiness"]["status"] == "checked"
