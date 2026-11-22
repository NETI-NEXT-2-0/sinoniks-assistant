import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from partner_agent.infra.settings import Settings
from partner_agent.runtime.accounting import tariff_snapshot
from partner_agent.runtime.output_schema import (
    current_output_schema,
    new_project_output_schema,
    output_schema_scope,
    output_schema_sha256,
)
from partner_agent.runtime.provider import chat_completion


def test_schema_keeps_protected_side_at_project_level_and_no_model_provenance():
    schema = new_project_output_schema(['source_1', 'source_2'])
    definitions = schema['$defs']
    root = definitions['Requirements19']['properties']
    assert 'id' not in root and 'protected_network_id' in root
    assert 'protected_network_id' not in definitions['FileFlow19']['properties']
    assert definitions['FileFlow19']['properties']['sides']['minItems'] == 2
    assert definitions['FileFlow19']['properties']['sides']['maxItems'] == 2
    assert 'sources' not in json.dumps(schema) and 'legacy' not in definitions

    def check(value):
        if isinstance(value, dict):
            assert not {'oneOf', 'discriminator', 'default', 'const'} & value.keys()
            if value.get('type') == 'object':
                assert value['additionalProperties'] is False
                assert set(value['required']) == set(value['properties'])
            if '$ref' in value:
                assert value['$ref'].removeprefix('#/$defs/') in definitions
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(schema)
    empty = new_project_output_schema([])
    assert empty['properties']['citations']['maxItems'] == 0
    assert schema['properties']['citations']['items']['properties']['evidence_id']['enum'] == [
        'source_1', 'source_2']  # No shared mutable request state.


async def test_schema_context_is_private_per_task_and_restored_on_exception():
    async def capture(name):
        schema = new_project_output_schema([name])
        with output_schema_scope(schema):
            await asyncio.sleep(0)
            assert current_output_schema() is schema
            return output_schema_sha256()
    first, second = await asyncio.gather(capture('one'), capture('two'))
    assert first != second and current_output_schema() is None
    with pytest.raises(RuntimeError), output_schema_scope({'type': 'object'}):
        raise RuntimeError('test')
    assert output_schema_sha256() is None


@pytest.mark.parametrize('wire,endpoint', [
    ('chat_completions', 'https://test.invalid/v1'),
    ('responses', 'https://test.invalid/v1'),
    ('chat_completions', 'https://polza.ai/api/v1'),
])
async def test_schema_wire_format_and_ledger_hash(wire, endpoint):
    schema = new_project_output_schema(['source_1'])
    calls = []
    def receive(request):
        body = json.loads(request.content)
        calls.append(body)
        output = (body['response_format']['json_schema'] if wire == 'chat_completions'
                  else body['text']['format'])
        assert output['strict'] is True and output['schema'] == schema
        assert output['name'] == 'sinoniks_new_project'
        if endpoint == 'https://polza.ai/api/v1':
            assert body['provider'] == {'only': ['openai'], 'allow_fallbacks': False}
        return httpx.Response(200, json={'choices': []})
    settings = Settings(_env_file=None, database_url='postgresql+psycopg://localhost/test',
                        runtime_profile='openai_compatible', model_endpoint=endpoint,
                        model_name='synthetic', model_api_key='synthetic', model_wire_api=wire,
                        model_json_output=True, model_structured_design_output=True,
                        model_provider_variant='openai')
    with output_schema_scope(schema):
        await chat_completion(settings, [], client_factory=lambda **kw: httpx.AsyncClient(
            transport=httpx.MockTransport(receive), **kw))
        assert tariff_snapshot(settings)['output_schema_sha256'] == output_schema_sha256()
    assert len(calls) == 1 and current_output_schema() is None
    with pytest.raises(ValidationError, match='requires JSON output'):
        Settings(_env_file=None, database_url='postgresql+psycopg://localhost/test',
                 model_structured_design_output=True)


@pytest.mark.parametrize('new_project', [True, False])
async def test_only_new_projects_use_the_full_schema_and_existing_edits_stay_addressed(
    monkeypatch, new_project,
):
    from test_sinoniks_contract19 import data_project

    from partner_agent.runtime.rag_provider import answer_with_provider, prepare_request

    base = data_project()
    observed = []
    async def model(settings, messages):
        observed.append(current_output_schema())
        content = {'answer': 'Условия проекта', 'citations': [], 'mode': 'design'}
        if new_project:
            content['design'] = {k: v for k, v in base.items() if k != 'id'}
        else:
            content['design_patch'] = {'operations': [{'op': 'update', 'collection': 'systems',
                'id': 'sender', 'values': {'name': 'Новое имя'}}]}
        return {'choices': [{'message': {'content': json.dumps(content)}}]}
    monkeypatch.setattr('partner_agent.runtime.rag_provider.chat_completion', model)
    settings = Settings(_env_file=None, database_url='postgresql+psycopg://localhost/test',
                        model_context_window_tokens=1050000, model_json_output=True,
                        model_structured_design_output=True)
    prepared = prepare_request('Измени условия', [], design_context=base, new_project=new_project,
                               context_window_tokens=1050000)
    result = await answer_with_provider(settings, 'Измени условия', [], prepared=prepared)
    assert result['answer_mode'] == 'design' and len(observed) == 1
    assert result['design'].id == base['id']
    assert (observed[0] is not None) == new_project
    assert current_output_schema() is None


async def test_schema_size_is_checked_before_any_paid_request(monkeypatch):
    from test_sinoniks_contract19 import data_project

    from partner_agent.runtime.provider import ProviderError
    from partner_agent.runtime.rag_provider import answer_with_provider, prepare_request

    async def forbidden(*args):
        raise AssertionError('Schema overflow must not reach the provider')
    monkeypatch.setattr('partner_agent.runtime.rag_provider.chat_completion', forbidden)
    request = prepare_request('Создай', [], design_context=data_project(), new_project=True,
                              context_window_tokens=1050000, max_output_tokens=6000)
    size = sum(len(item['content'].encode()) + 64 for item in request.messages)
    settings = Settings(_env_file=None, database_url='postgresql+psycopg://localhost/test',
                        model_context_window_tokens=size+6000+64, model_max_output_tokens=6000,
                        model_json_output=True, model_structured_design_output=True)
    with pytest.raises(ProviderError, match='rag_context_limit'):
        await answer_with_provider(settings, 'Создай', [], prepared=request)


async def test_structured_output_still_rejects_semantically_invalid_network_references(monkeypatch):
    from test_sinoniks_contract19 import data_project

    from partner_agent.runtime.provider import ProviderError
    from partner_agent.runtime.rag_provider import answer_with_provider, prepare_request

    base = data_project()
    invalid = deepcopy(base)
    invalid.pop('id')
    invalid['flows'][0]['receiver_network_id'] = 'undeclared-network'
    async def model(*args):
        return {'choices': [{'message': {'content': json.dumps({
            'answer': 'Условия', 'mode': 'design', 'citations': [], 'design': invalid,
        })}}]}
    monkeypatch.setattr('partner_agent.runtime.rag_provider.chat_completion', model)
    settings = Settings(_env_file=None, database_url='postgresql+psycopg://localhost/test',
                        model_context_window_tokens=1050000, model_json_output=True,
                        model_structured_design_output=True)
    request = prepare_request('Создай', [], design_context=base, new_project=True,
                              context_window_tokens=1050000)
    with pytest.raises(ProviderError, match='provider_invalid_design'):
        await answer_with_provider(settings, 'Создай', [], prepared=request)
    assert current_output_schema() is None
