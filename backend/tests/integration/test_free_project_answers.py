"""Free form answers are project input, not an unchanged zero-call revision."""

import json
from uuid import UUID, uuid4

import pytest
from test_chat_data_plane import new_chat
from test_chat_data_plane import plane as plane
from test_requirements_revisions import data_project, save
from test_response_execution import configure_execution

from partner_agent.infra.settings import get_settings
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


async def arrange(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}"
    first = (await save(client, headers, base + '/designs', chat['default_branch_id'],
                        {"schema_version": 3, "id": "empty"})).json()
    payload = {"branch_id": chat['default_branch_id'], "message": "Применить уточнения проекта",
               "request_mode": "design", "project_answer": {
                   "base_revision_id": first['id'], "answers": [], "remarks": [
                       {"path": "/networks", "text": "Открытая офисная сеть и закрытая сеть цеха"},
                       {"path": "/flows", "text": "Компьютер цеха передаёт журналы серверу офиса "
                        "по UDP 514 без ответов"},
                   ]}}
    return client, headers, chat, base, first, payload


async def test_empty_project_free_fields_reach_model_and_save_a_real_project(plane, monkeypatch):
    client, headers, chat, base, first, payload = await arrange(plane, monkeypatch)
    calls = []
    async def model(settings, messages):
        calls.append(messages)
        question = messages[-1]['content']
        assert "Укажите две сети" in question and "Открытая офисная сеть" in question
        assert "Опишите хотя бы один поток" in question and "UDP 514 без ответов" in question
        conditions = data_project()
        conditions['id'] = 'empty'
        return {"choices": [{"message": {"content": json.dumps({
            "mode": "design", "answer": "Готово", "citations": [], "design": conditions,
        })}}], "usage": {"prompt_tokens": 100, "completion_tokens": 100}}
    monkeypatch.setattr('partner_agent.runtime.rag_provider.chat_completion', model)
    key = {**headers, 'Idempotency-Key': str(uuid4())}
    response = await client.post(base + '/runs', headers=key, json=payload)
    assert response.status_code == 202, response.text
    run_id = response.json()['run_id']
    assert (await execute_run(UUID(run_id), dispatch_generation=0))['status'] == 'succeeded'
    snapshot = (await client.get(base + '/snapshot')).json()
    user = next(m for m in snapshot['messages'] if m['role'] == 'user')
    assert "Открытая офисная сеть" in user['content'] and "UDP 514" in user['content']
    latest = (await client.get(base + '/designs/latest', params={
        'branch_id': chat['default_branch_id'],
    })).json()['design']
    assert latest['revision'] == 2 and latest['parent_id'] == first['id']
    assert len(latest['input']['networks']) == 2 and len(latest['input']['flows']) == 1
    assert latest['result']['confirmed_rules_count'] == 1
    assert len(calls) == 1
    assert (await client.post(base + '/runs', headers=key, json=payload)).json() == response.json()
    repeat = await execute_run(UUID(run_id), dispatch_generation=0)
    assert repeat['status'] == 'duplicate_or_ineligible'
    assert len(calls) == 1
    assert (await client.get(base + '/vendor-wishes/export')).json()['wishes'] == []


@pytest.mark.parametrize('reply', ['clarify', 'invalid', 'unchanged'])
async def test_unresolved_text_keeps_saved_revision(plane, monkeypatch, reply):
    client, headers, chat, base, first, payload = await arrange(plane, monkeypatch)
    async def model(settings, messages):
        body = {"mode": "insufficient", "answer": "Укажите, какая сеть защищаемая?",
                "citations": [], "design": None}
        if reply == 'unchanged':
            body.update(mode='design', design={"schema_version": 3, "id": "empty"})
        return {"choices": [{"message": {"content": 'bad json' if reply == 'invalid'
                                         else json.dumps(body)}}]}
    monkeypatch.setattr('partner_agent.runtime.rag_provider.chat_completion', model)
    response = await client.post(base + '/runs',
                                 headers={**headers, 'Idempotency-Key': str(uuid4())},
                                 json=payload)
    run_id = response.json()['run_id']
    await execute_run(UUID(run_id), dispatch_generation=0)
    latest = (await client.get(base + '/designs/latest', params={
        'branch_id': chat['default_branch_id'],
    })).json()['design']
    assert latest['id'] == first['id']
    snapshot = (await client.get(base + '/snapshot')).json()
    assert any('Открытая офисная сеть' in m['content'] for m in snapshot['messages'])


async def test_offline_profile_preserves_text_without_fake_project_revision(plane, monkeypatch):
    client, headers, chat, base, first, payload = await arrange(plane, monkeypatch)
    monkeypatch.setenv('PARTNER_RUNTIME_PROFILE', 'offline_fixture')
    get_settings.cache_clear()
    response = await client.post(base + '/runs',
                                 headers={**headers, 'Idempotency-Key': str(uuid4())},
                                 json=payload)
    await execute_run(UUID(response.json()['run_id']), dispatch_generation=0)
    latest = (await client.get(base + '/designs/latest', params={
        'branch_id': chat['default_branch_id'],
    })).json()['design']
    assert latest['id'] == first['id']
    run = (await client.get('/api/v1/runs/' + response.json()['run_id'])).json()
    assert run['outcome']['provider_calls'] == 0 and run['outcome']['task_completed'] is False
