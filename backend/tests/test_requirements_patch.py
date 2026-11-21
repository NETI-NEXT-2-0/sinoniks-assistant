import json
from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_sinoniks_contract19 import data_project, file_project

from partner_agent.infra.settings import Settings
from partner_agent.requirements_patch import PatchError, apply_patch, compact_project
from partner_agent.runtime.provider import ProviderError
from partner_agent.runtime.rag_provider import answer_with_provider, prepare_request
from partner_agent.sinoniks_contract19 import Requirements19


def edit(op="update", collection="systems", identifier="sender", **values):
    return {"op": op, "collection": collection, "id": identifier, "values": values}


def apply(base, *operations):
    return apply_patch(base, {"operations": list(operations)})


def test_rename_preserves_every_unrelated_field_and_base():
    base = Requirements19.model_validate(file_project()).model_dump(mode="json")
    base["systems"][0]["sources"] = [{"kind": "operator", "reference": "original"}]
    base = Requirements19.model_validate(base).model_dump(mode="json")
    before = deepcopy(base)
    updated = apply(base, edit(name="Новое имя")).model_dump(mode="json")
    expected = deepcopy(base)
    expected["systems"][0]["name"] = "Новое имя"
    assert updated == expected
    assert base == before


def test_add_does_not_silently_change_existing_exchange():
    base = data_project()
    updated = apply(base, edit("add", identifier="new-computer", name="Компьютер 3",
                               network_id="plant"))
    assert len(updated.systems) == 3
    assert updated.flows[0].sender_ids == ["sender"]


def test_removal_and_reference_update_are_atomic():
    base = data_project()
    base["systems"].append({"id": "extra", "network_id": "plant"})
    base["flows"][0]["sender_ids"].append("extra")
    before = deepcopy(base)
    with pytest.raises(ValidationError):
        apply(base, edit("remove", identifier="sender"))
    assert base == before
    updated = apply(base, edit("remove", identifier="sender"),
                    edit(collection="flows", identifier="logs", sender_ids=["extra"]))
    assert updated.flows[0].sender_ids == ["extra"]
    assert base == before


@pytest.mark.parametrize("operation", [
    edit(identifier="missing", name="No"),
    edit("add", identifier="office", name="Duplicate"),
    edit("remove", name="No"),
    edit(kind="usb"),
    edit(collection="project", identifier=None, id="stolen"),
    edit(collection="project", identifier=None, legacy=None),
    edit(sources=[]),
    edit(unrecognized="value"),
    edit(network_id="missing"),
])
def test_invalid_edits_leave_original_unchanged(operation):
    base = data_project()
    before = deepcopy(base)
    with pytest.raises((PatchError, ValidationError)):
        apply(base, operation)
    assert base == before


def test_duplicate_operations_and_empty_patch_rejected():
    with pytest.raises(PatchError):
        apply(data_project(), edit(name="First"), edit(name="Second"))
    with pytest.raises(ValidationError):
        apply(data_project())


def test_unknown_notes_follow_entity_and_clear_when_answered():
    base = data_project()
    base["systems"].append({"id": "third", "name": None, "network_id": "plant"})
    base["unknowns"] = [{"path": "/systems/2/name", "target_id": "third",
                         "reason": "user_unknown"}]
    updated = apply(base, edit("remove", identifier="receiver"),
                    edit(collection="flows", identifier="logs", receiver_ids=[]))
    assert updated.unknowns[0].path == "/systems/1/name"
    answered = apply(updated.model_dump(mode="json"), edit(identifier="third", name="Ответ"))
    assert answered.unknowns == []


def test_root_change_clears_old_assumption_for_that_choice_only():
    base = data_project()
    base["protected_network_id"] = "plant"
    base["assumptions"] = [{"path": "/protected_network_id", "target_id": None,
                            "explanation": "Прежний выбор", "basis": "user_minimum"}]
    updated = apply(base, edit(collection="project", identifier=None, protected_network_id=None))
    assert updated.assumptions == []
    assert updated.protected_network_id is None


def test_compact_view_preserves_semantic_null_and_empty_checks():
    base = Requirements19.model_validate(file_project()).model_dump(mode="json")
    view = compact_project(base)
    assert "sources" not in json.dumps(view)
    assert Requirements19.model_validate(view) == Requirements19.model_validate(base)
    assert view["flows"][0]["sides"][0]["checks"] == []
    base["flows"][0]["sides"][0]["checks"] = None
    assert Requirements19.model_validate(compact_project(base)).flows[0].sides[0].checks is None
    assert len(json.dumps(view)) < len(json.dumps(base))


def test_large_project_uses_compact_view_without_losing_any_entity():
    base = Requirements19.model_validate(data_project()).model_dump(mode="json")
    base["systems"] += [{"id": f"pc-{i}", "name": "К" * 100, "network_id": "plant"}
                        for i in range(200)]
    question = "Переименуй pc-199"
    with pytest.raises(ProviderError, match="rag_context_limit"):
        prepare_request(question, [], design_context=base)
    prepared = prepare_request(question, [], design_context=base,
                               context_window_tokens=65536, max_output_tokens=3000)
    wire = prepared.messages[-1]["content"].split("Saved project conditions:\n")[1]
    restored = Requirements19.model_validate(json.loads(wire))
    assert len(restored.systems) == 202
    assert restored.systems[-1].id == "pc-199"
    assert prepared.patch_base == base
    assert "design_patch" in prepared.messages[0]["content"]
    assert "Return the COMPLETE updated saved document" not in prepared.messages[0]["content"]
    assert sum(len(m["content"].encode()) + 64 for m in prepared.messages) < 62536


def test_repeat_history_is_removed_as_whole_pairs_and_current_request_preserved():
    history = [{"role": role, "content": text} for text, role in
               [("Old", "user"), ("Answer", "assistant"),
                ("Old", "user"), ("Answer", "assistant"),
                ("Current", "user"), ("Earlier", "assistant")]]
    prepared = prepare_request("Current", [], history)
    assert prepared.messages[1:-1] == history[:2]
    assert prepared.messages[-1]["content"].count("Current") == 1


def test_compaction_alone_fits_large_default_heavy_project_inside_legacy_bound():
    base = Requirements19.model_validate(data_project()).model_dump(mode="json")
    base["systems"] += [Requirements19.model_validate({
        "id": "scratch", "systems": [{"id": f"pc-{i}", "name": "Узел"}],
    }).model_dump(mode="json")["systems"][0] for i in range(240)]
    request = prepare_request("Переименуй pc-239", [], design_context=base)
    wire = json.dumps(request.messages, ensure_ascii=False)
    full = json.dumps(base, ensure_ascii=False)
    assert len(full) + len(request.messages[0]["content"]) > 32000
    assert len(wire) <= 32000
    compact = json.loads(request.messages[-1]["content"].split("Saved project conditions:\n")[1])
    assert len(Requirements19.model_validate(compact).systems) == 242


def test_oversized_calculation_keeps_all_rule_links_counts_and_displayed_sides():
    calculation = {"counts": {"rules_count": 1}, "displayed_networks": {"right": "plant"},
                   "rules": [{"id": 1, "flow_ids": ["logs"], "description": "x" * 32000}]}
    request = prepare_request("Добавь справа", [], design_context=data_project(),
                              project_calculation=calculation)
    wire = request.messages[-1]["content"]
    assert request.can_explain_project
    assert '"right":"plant"' in wire and '"flow_ids":["logs"]' in wire
    assert '"rules_count":1' in wire
    assert "x" * 100 not in wire


@pytest.mark.asyncio
async def test_too_small_declared_window_stops_before_provider_call(monkeypatch):
    calls = []
    async def model(settings, messages):
        calls.append(messages)
        raise AssertionError("Oversized request must not reach provider")
    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db",
                        model_context_window_tokens=2000, model_max_output_tokens=1000)
    with pytest.raises(ProviderError, match="rag_context_limit"):
        await answer_with_provider(settings, "Измени", [], design_context=data_project())
    assert not calls


@pytest.mark.asyncio
async def test_provider_patch_applies_only_to_captured_base(monkeypatch):
    async def model(settings, messages):
        return {"choices": [{"message": {"content": json.dumps({
            "answer": "Переименовал", "mode": "design", "design": None, "citations": [],
            "design_patch": {"operations": [edit(name="Новое имя")]},
        })}}]}
    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    base = data_project()
    prepared = prepare_request("Переименуй", [], design_context=base)
    base["systems"][0]["name"] = "Late mutation"
    response = await answer_with_provider(settings, "Переименуй", [], prepared=prepared)
    assert response["design"].systems[0].name == "Новое имя"
    assert response["design"].flows[0].destination_port == 514
    with pytest.raises(ProviderError, match="provider_invalid_design_patch"):
        await answer_with_provider(settings, "Переименуй", [], design_context=base,
                                   request_mode="reference")
