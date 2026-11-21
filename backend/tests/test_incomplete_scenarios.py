"""N01/N05/U04: meaningful gaps remain unknown, while supported work remains available."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from partner_agent.infra.settings import Settings
from partner_agent.requirements_service import without_sources
from partner_agent.runtime.rag_provider import answer_with_provider
from partner_agent.sinoniks_calculation import solve_project
from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_contract19_io import assess
from partner_agent.sinoniks_result_export import result_document


def baseline():
    return json.loads((Path(__file__).parent / "fixtures/network-f02.json").read_text(
        encoding="utf-8",
    ))


def unknown_file(direction="both"):
    return {"id": "files", "kind": "file", "direction": direction,
            "sender_network_id": "outer", "receiver_network_id": "inner",
            "sender_ids": [], "receiver_ids": [],
            "sides": [{"network_id": "outer"}, {"network_id": "inner"}]}


def test_n01_file_methods_checks_users_and_visibility_are_not_invented():
    raw = {**baseline(), "flows": [unknown_file()]}
    request = Requirements19.model_validate(raw)
    document = result_document(request, solve_project(request), session_id="synthetic-n01")
    assert document["confirmed_rules_count"] is None and document["file_users_count"] is None
    assert document["status"] in ("preliminary", "needs_clarification")
    fields = {issue.path for issue in assess(request).issues}
    assert "/flows/0/sides/0/manual" in fields and "/flows/0/sides/1/checks" in fields
    assert "/manual_users" in fields
    assert all(side.manual is None and side.checks is None for side in request.flows[0].sides)
    assert all(not field.endswith("/purpose") for field in fields)


def test_n05_known_logs_survive_until_usb_file_requirements_are_completed():
    raw = baseline()
    logs = next(flow for flow in raw["flows"] if flow["id"] == "logs")
    raw["flows"] = [logs, unknown_file("forward")]
    partial = Requirements19.model_validate(raw)
    result = solve_project(partial)
    assert result.minimum is None and len(result.data.rules) == 1
    raw["systems"].extend([
        {"id": "usb", "kind": "usb", "attachment": "node-A"},
        {"id": "store", "kind": "storage", "network_id": "inner"},
    ])
    raw["actors"] = [{"id": "operator"}]
    raw["manual_users"] = {"lower": 1, "upper": 1}
    raw["flows"][1].update(sender_ids=["usb"], receiver_ids=["store"], sides=[
        {"network_id": "outer", "manual": False, "usb": True, "actor_ids": ["operator"],
         "checks": [{"kind": "icap", "icap_service": "antivirus"}],
         "received_files_shared": True},
        {"network_id": "inner", "manual": False, "usb": False, "checks": [],
         "received_files_shared": True,
         "automations": [{"operation": "PUSH", "protocol": "SMB", "storage_id": "store"}]},
    ])
    complete = Requirements19.model_validate(raw)
    final = result_document(complete, solve_project(complete), session_id="synthetic-n05")
    assert final["confirmed_rules_count"] == 2 and final["file_users_count"] == 1
    assert len([rule for rule in final["rules"] if rule["type"] == "data"]) == 1
    assert len([rule for rule in final["rules"] if rule["type"] == "file"]) == 1
    assert partial.flows[1].sender_ids == [] and partial.manual_users is None


def test_only_meaningful_network_fields_are_asked_and_unknown_port_is_optional():
    raw = baseline()
    raw["flows"][0]["destination_port"] = None
    request = Requirements19.model_validate(raw)
    issues = assess(request).issues
    assert all(not issue.path.endswith("/purpose") for issue in issues)
    assert any(issue.path == "/flows/0/destination_port" and issue.kind == "optional"
               for issue in issues)


def test_mixed_supported_and_unsupported_request_retains_the_supported_rule():
    raw = baseline()
    raw["flows"] = [next(flow for flow in raw["flows"] if flow["id"] == "logs")]
    raw["requests_for_vendor"] = [{"id": "wish", "kind": "unsupported",
                                   "text": "Нужен встроенный антивирус"}]
    request = Requirements19.model_validate(raw)
    result = result_document(request, solve_project(request), session_id="synthetic-mixed")
    assert result["confirmed_rules_count"] == 1
    assert result["requests_for_vendor"] == ["Нужен встроенный антивирус"]


@pytest.mark.asyncio
async def test_three_network_proposal_asks_for_a_pair_without_a_repair_call(monkeypatch):
    original = baseline()
    proposal = without_sources(deepcopy(original))
    proposal["networks"].append({"id": "third", "name": "Третья сеть", "trust": "open"})
    calls = []

    async def model(settings, messages):
        calls.append(messages)
        return {"choices": [{"message": {"content": json.dumps({
            "answer": "Непроверенное решение для трёх сетей", "mode": "design", "citations": [],
            "design": proposal,
        }, ensure_ascii=False)}}], "usage": {"prompt_tokens": 123, "completion_tokens": 45}}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    result = await answer_with_provider(Settings(_env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/db"),
        "Соедини три сети", [], design_context=original, request_mode="design")
    assert result["answer_mode"] == "insufficient" and "design" not in result
    assert "пару" in result["answer"] and result["usage"]["prompt_tokens"] == 123
    assert len(calls) == 1 and len(original["networks"]) == 2
