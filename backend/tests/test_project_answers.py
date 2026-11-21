"""Only explicit revision-bound fields can be changed without a model."""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_sinoniks_contract19 import data_project, file_project

from partner_agent.infra.errors import APIError
from partner_agent.project_answers import ProjectAnswer19, apply_answer, question_choices
from partner_agent.sinoniks_contract19 import Requirements19


def command(**changes):
    return ProjectAnswer19.model_validate(
        {
            "base_revision_id": str(uuid4()),
            "path": "/flows/0/protocol",
            "target_id": "logs",
            "value": "UDP",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "path",
    [
        "/id",
        "/legacy/firewall",
        "/flows/0/id",
        "/flows/0/sources",
        "/flows/00/protocol",
        "/flows/-1/mode",
        "/__proto__/polluted",
        "/systems/0/attachment",
        "/networks",
    ],
)
def test_answer_is_not_an_arbitrary_json_patch(path):
    with pytest.raises(ValidationError):
        command(path=path)


def test_answer_updates_one_field_and_rejects_a_different_entity():
    document = data_project()
    document["flows"][0]["protocol"] = None
    request = Requirements19.model_validate(document)
    before = request.model_dump_json()
    result = apply_answer(request, command())
    assert result.flows[0].protocol == "UDP"
    assert result.flows[0].id == request.flows[0].id
    assert result.systems == request.systems and result.networks == request.networks
    assert request.model_dump_json() == before
    with pytest.raises(APIError) as caught:
        apply_answer(request, command(target_id="different-flow"))
    assert caught.value.code == "question_context_changed"


def test_unknown_is_saved_and_replaced_only_when_that_field_is_answered():
    document = data_project()
    document["flows"][0]["protocol"] = None
    request = Requirements19.model_validate(document)
    unknown = apply_answer(request, command(unknown=True, value=None))
    assert unknown.flows[0].protocol is None
    assert unknown.unknowns[0].reason == "user_unknown"
    assert unknown.unknowns[0].target_id == "logs"
    assert apply_answer(unknown, command(unknown=True, value=None)) == unknown
    answered = apply_answer(unknown, command())
    assert not answered.unknowns and answered.flows[0].protocol == "UDP"
    with pytest.raises(ValidationError):
        command(unknown=True, value="TCP")


@pytest.mark.parametrize("value", [True, "514", 65536, -1, {"sources": []}])
def test_field_type_and_constraints_are_enforced_after_application(value):
    request = Requirements19.model_validate(data_project())
    with pytest.raises(APIError) as caught:
        apply_answer(request, command(path="/flows/0/destination_port", value=value))
    assert caught.value.code == "invalid_project_answer"


def test_file_side_identity_must_match_its_saved_network():
    request = Requirements19.model_validate(file_project())
    answer = command(
        path="/flows/0/sides/0/received_files_shared",
        target_id="files",
        side_network_id="plant",
        value=False,
    )
    result = apply_answer(request, answer)
    assert result.flows[0].sides[0].received_files_shared is False
    with pytest.raises(APIError) as caught:
        apply_answer(request, answer.model_copy(update={"side_network_id": "office"}))
    assert caught.value.code == "question_context_changed"


@pytest.mark.parametrize("field", ["manual", "usb", "checks", "received_files_shared"])
def test_unknown_cannot_clear_known_file_side_fields(field):
    request = Requirements19.model_validate(file_project())
    with pytest.raises(APIError) as caught:
        apply_answer(request, command(
            path=f"/flows/0/sides/0/{field}", target_id="files", side_network_id="plant",
            unknown=True, value=None,
        ))
    assert caught.value.code == "question_context_changed"


@pytest.mark.parametrize("usb", [True, False])
def test_manual_file_questions_keep_the_known_sender_and_identify_the_receiving_side(usb):
    document = file_project()
    document["actors"] = [{"id": "person", "name": "Оператор"}]
    document["manual_users"] = {"lower": 1, "upper": 1}
    flow = document["flows"][0]
    flow.update(
        direction="forward", sender_network_id="office", receiver_network_id="plant",
        sender_ids=[], receiver_ids=["sender"],
    )
    source, destination = reversed(flow["sides"])
    source.update(
        manual=True, usb=usb, actor_ids=["person"], automations=[],
        checks=[{"kind": "signature"}], manual_protocol=None if usb else "SFTP",
    )
    destination.update(checks=None, received_files_shared=None, access_group="operations")
    request = Requirements19.model_validate(document)
    questions = {question["field"]: question for question in question_choices(request)}
    assert set(questions) == {
        "/flows/0/sides/0/checks", "/flows/0/sides/0/received_files_shared",
    }
    assert all("Узел В" in question["text"] for question in questions.values())
    assert "operations" in questions["/flows/0/sides/0/received_files_shared"]["text"]
    for question in questions.values():
        request = apply_answer(request, command(
            path=question["field"], target_id=question["target_id"],
            side_network_id=question["side_network_id"], unknown=True, value=None,
        ))
    assert request.flows[0].sides[1].model_dump(exclude={"sources"}) == (
        Requirements19.model_validate(document).flows[0].sides[1].model_dump(exclude={"sources"})
    )
    assert not question_choices(request)


def test_choices_expose_values_and_human_labels_without_provenance():
    document = data_project()
    document["networks"][0]["trust"] = None
    document["flows"][0]["protocol"] = None
    request = Requirements19.model_validate(document)
    questions = question_choices(request)
    trust = next(item for item in questions if item["field"] == "/networks/0/trust")
    assert trust["target_id"] == "office" and trust["kind"] == "choice"
    assert {item["value"] for item in trust["choices"]} == {"open", "closed"}
    assert "sources" not in str(questions)


@pytest.mark.parametrize(
    "protocol,application",
    [
        ("MQTT", "generic"),
        ("Modbus TCP", "generic"),
        ("МЭК 104", "generic"),
        (None, "ntp"),
    ],
)
def test_product_defined_modes_do_not_generate_redundant_questions(protocol, application):
    document = data_project()
    document["flows"][0].update(mode=None, protocol=protocol, application=application)
    if application == "ntp":
        document["flows"][0].update(
            sender_network_id="office",
            receiver_network_id="plant",
            sender_ids=["receiver"],
            receiver_ids=["sender"],
        )
    request = Requirements19.model_validate(document)
    fields = {question["field"] for question in question_choices(request)}
    assert "/flows/0/mode" not in fields
    if application == "ntp":
        assert "/flows/0/protocol" not in fields
    assert request.flows[0].mode is None
