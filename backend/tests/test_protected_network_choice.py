"""A project assumption must not relabel an actually open network as closed."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from partner_agent.project_answers import ProjectAnswer19, apply_answer, question_choices
from partner_agent.requirements_service import bind_requirements
from partner_agent.sinoniks_calculation import solve_project
from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_contract19_io import assess
from partner_agent.sinoniks_result_export import result_document


def conditions():
    return json.loads((Path(__file__).parent / "fixtures/network-f02.json").read_text(
        encoding="utf-8",
    ))


def open_pair():
    raw = conditions()
    for network in raw["networks"]:
        network["trust"] = "open"
    return raw


def test_open_networks_require_an_explicit_durable_choice():
    request = Requirements19.model_validate(open_pair())
    assert any(issue.path == "/protected_network_id" for issue in assess(request).issues)
    choices = question_choices(request)
    question = next(item for item in choices if item["field"] == "/protected_network_id")
    assert question["kind"] == "choice"
    assert {item["value"] for item in question["choices"]} == {"outer", "inner"}
    assert solve_project(request).data.placement is None
    chosen = apply_answer(request, ProjectAnswer19(
        base_revision_id=uuid4(), path="/protected_network_id", value="inner",
    ))
    result = solve_project(chosen)
    assert result.minimum == 3 and result.data.placement.assumed_protected_side
    assert result.data.placement.protected_network_id == "inner"
    assert all(network.trust == "open" for network in chosen.networks)
    restored = Requirements19.model_validate_json(chosen.model_dump_json())
    assert restored.protected_network_id == "inner"
    assert solve_project(restored, protected_network_id="outer").data.placement is None
    exported = result_document(restored, solve_project(restored), session_id="open-pair")
    assert exported["assumptions"][-1]["kind"] == "protected_network_choice"
    assert not any(issue.path == "/protected_network_id" for issue in assess(restored).issues)


def test_missing_new_field_preserves_the_exact_previous_serialization():
    request = Requirements19.model_validate(conditions())
    assert "protected_network_id" not in request.model_dump()
    assert hashlib.sha256(request.model_dump_json().encode()).hexdigest() == (
        "853e4b4fc5ed7f59951ddfe1406d8a337ff8102a93ed00ff446af80ebc6417cc"
    )


def test_choice_unknown_and_server_assigned_network_ids_survive_round_trip():
    chosen = Requirements19.model_validate({**open_pair(), "protected_network_id": "inner"})
    message_id = uuid4()
    assigned = bind_requirements(chosen, None, message_id=message_id, command_id=uuid4())
    protected = next(network for network in assigned.networks if network.name == "Закрытая станция")
    assert assigned.protected_network_id == protected.id and protected.id != "inner"
    unknown = apply_answer(assigned, ProjectAnswer19(
        base_revision_id=uuid4(), path="/protected_network_id", unknown=True,
    ))
    assert unknown.protected_network_id is None
    restored = Requirements19.model_validate_json(unknown.model_dump_json())
    assert any(note.path == "/protected_network_id" for note in restored.unknowns)
    assert solve_project(restored).data.placement is None


def test_dangling_protected_network_id_cannot_be_saved():
    with pytest.raises(ValidationError):
        Requirements19.model_validate({**open_pair(), "protected_network_id": "foreign"})
