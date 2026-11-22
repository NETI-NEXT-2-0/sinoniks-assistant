from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_sinoniks_contract19 import data_project, file_project

from partner_agent.requirements_input import (
    normalize_file_side_actor_ids,
    normalize_note_entity_paths,
)
from partner_agent.requirements_patch import apply_patch
from partner_agent.sinoniks_contract19 import Requirements19


def test_entity_note_address_is_equivalent_to_indexed_note():
    document = data_project()
    flow = document["flows"][0]
    document["assumptions"] = [{"path": f"/flows/{flow['id']}/protocol",
                                "target_id": flow["id"], "explanation": "Deployment note",
                                "basis": "product_fact"}]
    expected = deepcopy(document)
    expected["assumptions"][0]["path"] = "/flows/0/protocol"
    normalize_note_entity_paths(document)
    assert document == expected
    Requirements19.model_validate(document)
    normalize_note_entity_paths(document)
    assert document == expected


@pytest.mark.parametrize("target,path", [
    ("wrong-id", "/flows/logs/protocol"),
    ("missing", "/flows/missing/protocol"),
    ("logs", "/flows/logs/not_a_field"),
])
def test_address_normalization_does_not_bypass_validation(target, path):
    document = data_project()
    document["assumptions"] = [{"path": path, "target_id": target,
                                "explanation": "Deployment note", "basis": "product_fact"}]
    normalize_note_entity_paths(document)
    with pytest.raises(ValidationError):
        Requirements19.model_validate(document)


def test_unknown_cannot_hide_a_supplied_value_after_address_translation():
    document = data_project()
    flow = document["flows"][0]
    document["unknowns"] = [{"path": f"/flows/{flow['id']}/protocol",
                             "target_id": flow["id"], "reason": "missing"}]
    normalize_note_entity_paths(document)
    with pytest.raises(ValidationError):
        Requirements19.model_validate(document)


def test_file_side_alias_is_applied_atomically_in_addressed_patch():
    document = file_project()
    sides = deepcopy(document["flows"][0]["sides"])
    for side in sides:
        side["actors"] = []
    result = apply_patch(document, {"operations": [{
        "op": "update", "collection": "flows", "id": "files", "values": {"sides": sides},
    }]})
    assert all(side.actor_ids == [] for side in result.flows[0].sides)
    assert "actors" in sides[0] and "actor_ids" not in sides[0]


@pytest.mark.parametrize("aliases", [["missing"], [{"id": "new-person"}], [12]])
def test_actor_alias_does_not_invent_people_or_accept_invalid_references(aliases):
    document = file_project()
    document["flows"][0]["sides"][0]["actors"] = aliases
    normalize_file_side_actor_ids(document)
    with pytest.raises(ValidationError):
        Requirements19.model_validate(document)


def test_actor_alias_conflict_is_not_resolved_silently():
    document = file_project()
    document["flows"][0]["sides"][0].update(actors=[], actor_ids=[])
    normalize_file_side_actor_ids(document)
    with pytest.raises(ValidationError):
        Requirements19.model_validate(document)
