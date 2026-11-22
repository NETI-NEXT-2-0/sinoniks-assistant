"""Explicit protected-side selection survives storage, zero-call answers and ID binding."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_design_execution import reply
from test_response_execution import configure_execution

from partner_agent.infra.settings import get_settings
from partner_agent.requirements_service import without_sources
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


def open_pair():
    conditions = json.loads((Path(__file__).parents[1] / "fixtures/network-f02.json").read_text(
        encoding="utf-8",
    ))
    for network in conditions["networks"]:
        network["trust"] = "open"
    for flow in conditions["flows"]:
        flow["purpose"] = flow["id"]
    return conditions


async def test_durable_unknown_then_protected_choice_preserves_open_trust_and_archives(
    plane, monkeypatch,
):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)

    async def forbidden(*args, **kwargs):
        raise AssertionError("A typed protected choice must not call the model")

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", forbidden)
    chat = await new_chat(client, headers)
    base, branch = f"/api/v1/chats/{chat['id']}", chat["default_branch_id"]
    key = {**headers, "Idempotency-Key": str(uuid4())}
    original = {"branch_id": branch, "input": open_pair()}
    response = await client.post(base + "/designs", headers=key, json=original)
    assert response.status_code == 201
    first = response.json()
    assert any(q["field"] == "/protected_network_id" for q in first["questions"])
    assert first["result"]["synonyx"] is None
    explicit_null = deepcopy(original)
    explicit_null["input"]["protected_network_id"] = None
    assert (await client.post(base + "/designs", headers=key, json=explicit_null)).json() == first
    old_file = (await client.post(base + f"/designs/{first['id']}/download/requirements",
                                  headers=headers)).content
    current = first
    for unknown in (True, False):
        payload = {"branch_id": branch, "message": "Уточнение защищаемой стороны",
                   "request_mode": "design", "project_answer": {
                       "base_revision_id": current["id"], "path": "/protected_network_id",
                       "unknown": unknown, "value": None if unknown else "inner",
                   }}
        admitted = await client.post(base + "/runs", headers={**headers,
                                     "Idempotency-Key": str(uuid4())}, json=payload)
        assert admitted.status_code == 202
        result = await execute_run(UUID(admitted.json()["run_id"]), dispatch_generation=0)
        assert result["status"] == "succeeded"
        run = (await client.get("/api/v1/runs/" + admitted.json()["run_id"])).json()
        assert run["outcome"]["provider_calls"] == 0
        current = (await client.get(base + "/designs/latest", params={"branch_id": branch})
                   ).json()["design"]
        assert all(network["trust"] == "open" for network in current["input"]["networks"])
        if unknown:
            assert current["input"]["protected_network_id"] is None
            assert any(n["path"] == "/protected_network_id" for n in current["input"]["unknowns"])
        else:
            assert current["input"]["protected_network_id"] == "inner"
            assert current["result"]["confirmed_rules_count"] == 3
            assert any(a.get("kind") == "protected_network_choice"
                       for a in current["result"]["assumptions"])
    assert (await client.post(base + f"/designs/{first['id']}/download/requirements",
                              headers=headers)).content == old_file
    ledger = (await client.get(base + "/accounting")).json()
    assert ledger["summary"]["calls_count"] == 0 and ledger["accounting_complete"]


async def test_model_chosen_network_id_is_rebound_with_its_network(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    monkeypatch.setenv("PARTNER_DESIGN_INPUT_VERSION", "3")
    get_settings.cache_clear()
    conditions = {**open_pair(), "protected_network_id": "inner"}

    async def model(settings, messages):
        return reply(without_sources(conditions))

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    chat = await new_chat(client, headers)
    admitted = await submit(client, headers, chat, text="Обе сети открытые. Защищаем станцию.")
    assert (await execute_run(UUID(admitted.json()["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    latest = (await client.get(f"/api/v1/chats/{chat['id']}/designs/latest",
                               params={"branch_id": chat["default_branch_id"]})).json()["design"]
    protected = next(n for n in latest["input"]["networks"] if n["name"] == "Закрытая станция")
    assert protected["id"] != "inner" and protected["trust"] == "open"
    assert latest["input"]["protected_network_id"] == protected["id"]
    assert latest["result"]["confirmed_rules_count"] == 3


async def test_three_network_candidate_keeps_the_last_valid_revision(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    monkeypatch.setenv("PARTNER_DESIGN_INPUT_VERSION", "3")
    get_settings.cache_clear()
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    conditions = open_pair()
    conditions["networks"][1]["trust"] = "closed"
    saved = await client.post(base, headers={**headers, "Idempotency-Key": str(uuid4())},
                              json={"branch_id": chat["default_branch_id"], "input": conditions})
    assert saved.status_code == 201
    original = saved.json()
    conditions["networks"].append({"id": "third", "name": "Третья сеть", "trust": "open"})
    calls = []

    async def model(settings, messages):
        calls.append(messages)
        return reply(without_sources(conditions))

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    admitted = await submit(client, headers, chat, text="Добавь третью сеть в эту схему")
    assert (await execute_run(UUID(admitted.json()["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    current = (await client.get(base + "/latest", params={"branch_id": chat["default_branch_id"]})
               ).json()["design"]
    assert current == original and len(calls) == 1
    view = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    assert any(message["role"] == "assistant" and "пару" in message["content"]
               for message in view["messages"])
