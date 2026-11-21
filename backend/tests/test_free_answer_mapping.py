from uuid import uuid4

import pytest
from test_sinoniks_contract19 import data_project, file_project

from partner_agent.infra.errors import APIError
from partner_agent.project_answers import (
    ProjectAnswerBatch19,
    clarification_message,
    reapply_structured_answers,
)
from partner_agent.sinoniks_contract19 import Requirements19


def batch(answers=None, remarks=None):
    return ProjectAnswerBatch19.model_validate({
        "base_revision_id": str(uuid4()), "answers": answers or [], "remarks": remarks or [],
    })


def test_free_answers_include_questions_and_values_in_durable_message():
    request = Requirements19(id='empty')
    command = batch(remarks=[{'path': '/networks', 'text': 'Офис открыт, цех закрыт'},
                             {'path': '/flows', 'text': 'Журналы идут в офис'}])
    message = clarification_message(request, command)
    assert 'Укажите две сети' in message and 'Офис открыт, цех закрыт' in message
    assert 'Опишите хотя бы один поток' in message and 'Журналы идут в офис' in message


def test_typed_selection_overrides_model_guess_after_reordering():
    raw = data_project()
    command = batch(answers=[{'path': '/networks/0/name', 'target_id': 'office',
                              'value': 'Явное имя'}])
    raw['networks'].reverse()
    result = reapply_structured_answers(Requirements19.model_validate(raw), command)
    assert result.networks[1].id == 'office' and result.networks[1].name == 'Явное имя'
    assert result.networks[0].name == 'Цех'


def test_explicit_unknown_does_not_turn_into_model_invented_port():
    command = batch(answers=[{'path': '/flows/0/destination_port', 'target_id': 'logs',
                              'unknown': True}])
    result = reapply_structured_answers(Requirements19.model_validate(data_project()), command)
    assert result.flows[0].destination_port is None
    assert result.unknowns[0].reason == 'user_unknown'


def test_deleted_or_reassigned_target_rejects_whole_proposal():
    raw = data_project()
    raw['systems'] = []
    raw['flows'] = []
    command = batch(answers=[{'path': '/systems/0/name', 'target_id': 'sender', 'value': 'Имя'}])
    with pytest.raises(APIError, match='removed'):
        reapply_structured_answers(Requirements19.model_validate(raw), command)


def test_file_side_answers_remain_bound_after_side_reordering():
    raw = file_project()
    raw['flows'][0]['sides'].reverse()
    command = batch(answers=[{'path': '/flows/0/sides/0/received_files_shared',
                              'target_id': 'files', 'side_network_id': 'plant', 'value': False}])
    result = reapply_structured_answers(Requirements19.model_validate(raw), command)
    sides = result.flows[0].sides
    assert next(side for side in sides if side.network_id == 'plant').received_files_shared is False
    assert next(side for side in sides if side.network_id == 'office').received_files_shared is True
