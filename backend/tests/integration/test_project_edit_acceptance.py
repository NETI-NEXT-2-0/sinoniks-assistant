"""U03 in one persisted conversation, independent counters and unchanged read-only steps."""

import json
from pathlib import Path
from uuid import UUID
from xml.etree.ElementTree import fromstring

import httpx
import pytest
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_response_execution import configure_execution

from partner_agent.infra.settings import get_settings
from partner_agent.runtime.provider import chat_completion
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


async def test_u03_full_sequence_preserves_identity_history_and_all_artifacts(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    monkeypatch.setenv("PARTNER_DESIGN_INPUT_VERSION", "3")
    monkeypatch.setenv("PARTNER_MODEL_WIRE_API", "chat_completions")
    get_settings.cache_clear()
    calls = []

    async def model(settings, messages):
        calls.append(messages)
        step = len(calls)
        if step == 1:
            path = Path(__file__).parents[1] / "fixtures/network-f02.json"
            proposal = json.loads(path.read_text(encoding="utf-8"))
            proposal["flows"] = [flow for flow in proposal["flows"] if flow["id"] == "logs"]
            proposal["flows"][0]["sender_ids"] = [f"device-{n}" for n in range(3)]
        else:
            proposal = json.loads(messages[-1]["content"].split("Saved project conditions:\n")[1])
        if step == 3:
            network = proposal["flows"][0]["receiver_network_id"]
            proposal["systems"].append({"id": "extra", "name": "Резервный приёмник",
                                        "network_id": network})
            proposal["flows"][0]["receiver_ids"].append("extra")
        if step == 4:
            next(item for item in proposal["systems"] if item["name"] == "Резервный приёмник")[
                "name"
            ] = "Архив журналов"
        if step == 6:
            removed = next(item["id"] for item in proposal["systems"]
                           if item["name"] == "Архив журналов")
            proposal["systems"] = [item for item in proposal["systems"] if item["id"] != removed]
            proposal["flows"][0]["receiver_ids"].remove(removed)
        mode = ("project_explanation" if step == 2 else
                "insufficient" if step in (5, 7) else "design")
        def strip_sources(value):
            if isinstance(value, dict):
                return {k: strip_sources(v) for k, v in value.items() if k != "sources"}
            if isinstance(value, list):
                return [strip_sources(v) for v in value]
            return value

        response = {"id": f"synthetic-u03-{step}", "choices": [{"message": {
            "content": json.dumps({"mode": mode, "citations": [], "answer": "Synthetic response",
                                   "design": (strip_sources(proposal)
                                              if mode == "design" else None)}),
        }}], "usage": {"prompt_tokens": 100, "completion_tokens": 100}}
        return await chat_completion(settings, messages, client_factory=lambda **kwargs:
                                     httpx.AsyncClient(transport=httpx.MockTransport(
                                         lambda request: httpx.Response(200, json=response),
                                     ), **kwargs))

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}"

    async def current():
        return (await client.get(base + "/designs/latest", params={
            "branch_id": chat["default_branch_id"],
        })).json()["design"]

    async def artifacts(design):
        files = {}
        for kind in ("json", "drawio"):
            response = await client.post(base + f"/designs/{design['id']}/download/{kind}",
                                         headers=headers)
            assert response.status_code == 200
            files[kind] = response.content
        response = await client.get(base + f"/designs/{design['id']}/diagram")
        assert response.status_code == 200
        files["scene"] = response.content
        scene = response.json()["scene"]
        expert = json.loads(files["json"])
        native = fromstring(files["drawio"])
        assert len(scene["rule_rows"]) == len(expert["rules"])
        assert scene["rule_rows"] == json.loads(native.attrib["data-rule-rows"])
        assert expert["confirmed_rules_count"] == design["result"]["confirmed_rules_count"]
        return files

    cases = [
        ("начало", 1, 1, "design"),
        ("объяснить", 1, 1, "project_explanation"),
        ("добавить", 2, 2, "design"),
        ("переименовать", 2, 3, "design"),
        ("неоднозначно удалить", 2, 3, "insufficient"),
        ("удалить Архив журналов", 1, 4, "design"),
        ("открыть подсеть", 1, 4, "insufficient"),
    ]
    original = None
    previous = None
    previous_files = None
    added_id = None
    for step, (text, count, revision, mode) in enumerate(cases, 1):
        accepted = await submit(client, headers, chat, text="Синтетический U03 " + text)
        run_id = accepted.json()["run_id"]
        assert (await execute_run(UUID(run_id), dispatch_generation=0))["status"] == "succeeded"
        outcome = (await client.get("/api/v1/runs/" + run_id)).json()["outcome"]
        assert outcome["answer_mode"] == mode and len(calls) == step
        design = await current()
        assert design["revision"] == revision and design["result"]["confirmed_rules_count"] == count
        assert len(design["input"]["flows"][0]["sender_ids"]) == 3
        files = await artifacts(design)
        if previous is not None:
            assert design["input"]["id"] == previous["input"]["id"]
            assert design["input"]["flows"][0]["id"] == previous["input"]["flows"][0]["id"]
            if mode != "design":
                assert design == previous and files == previous_files
                assert "design_id" not in outcome
            else:
                assert design["parent_id"] == previous["id"]
        if text == "добавить":
            added_id = next(item["id"] for item in design["input"]["systems"]
                            if item["name"] == "Резервный приёмник")
        if text == "переименовать":
            renamed = next(item for item in design["input"]["systems"] if item["id"] == added_id)
            assert renamed["name"] == "Архив журналов"
            assert renamed["sources"][-1]["reference"] == accepted.json()["message_id"]
            assert "Архив журналов" in files["drawio"].decode()
            assert [rule["rule_id"] for rule in design["result"]["rules"]] == [
                rule["rule_id"] for rule in previous["result"]["rules"]
            ]
        if text == "удалить Архив журналов":
            assert added_id not in {item["id"] for item in design["input"]["systems"]}
        if original is None:
            original = (design, files)
        assert await artifacts(original[0]) == original[1]
        previous, previous_files = design, files
    snapshot = (await client.get(base + "/snapshot")).json()
    assert len([message for message in snapshot["messages"] if message["role"] == "assistant"]) == 7
    ledger = (await client.get(base + "/accounting")).json()
    assert ledger["summary"]["calls_count"] == 7
    assert len(ledger["turns"]) == 7
