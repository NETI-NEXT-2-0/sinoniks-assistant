from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_sinoniks_contract19 import data_project

from partner_agent.requirements_input import normalize_note_entity_paths
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
