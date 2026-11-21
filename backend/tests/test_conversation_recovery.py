"""Conversation formatting and saved-diagram context stay strict and recoverable."""

import pytest

from partner_agent.design_service import project_readout
from partner_agent.infra.models_designs import DesignRevision
from partner_agent.runtime.provider import ProviderError
from partner_agent.runtime.rag_provider import (
    decode_model_json,
    prepare_request,
    validate_citations,
)


def test_one_complete_json_fence_is_unwrapped_without_relaxing_validation():
    assert (
        decode_model_json('```json\n{"answer":"Уточните сеть","citations":[]}\n```')["citations"]
        == []
    )
    for raw in [
        '```json\n{"answer":1,"answer":2}\n```',
        'Explanation: {"answer":"x"}',
        "```json\n{}\n```\n{}",
    ]:
        with pytest.raises(ProviderError, match="provider_invalid_rag_json"):
            decode_model_json(raw)
    with pytest.raises(ProviderError, match="provider_unauthorized_citation"):
        validate_citations(
            {
                "answer": "not trusted",
                "citations": [
                    {"evidence_id": "saved-project", "quote": "anything"},
                ],
            },
            [],
        )


def test_saved_diagram_side_mapping_uses_placement_not_network_list_order():
    revision = DesignRevision(
        revision=2,
        questions=[],
        input={
            "schema_version": 3,
            "networks": [{"id": "closed", "name": "Цех"}, {"id": "open", "name": "Офис"}],
        },
        result={
            "status": "completed",
            "synonyx": {"node_a_network_id": "open", "node_b_network_id": "closed"},
            "rules": [],
        },
    )
    summary = project_readout(revision)
    assert summary["displayed_networks"] == {
        "left": {"id": "open", "name": "Офис"},
        "right": {"id": "closed", "name": "Цех"},
    }
    prepared = prepare_request(
        "Добавь компьютер в контур справа",
        [],
        design_context=revision.input,
        project_calculation=summary,
    )
    assert '"right":' in prepared.messages[-1]["content"]
    assert "latest pending request" in prepared.messages[0]["content"]
    reference = prepare_request(
        "Справка",
        [],
        design_context=revision.input,
        project_calculation=summary,
        request_mode="reference",
    )
    assert '"right":' in reference.messages[-1]["content"]
    assert "Read-only saved project conditions:" in reference.messages[-1]["content"]


def test_latest_clarification_survives_a_full_evidence_window():
    history = [
        {"role": "user", "content": "Добавь компьютер 3 справа"},
        {"role": "assistant", "content": "В какую сеть добавить компьютер 3?"},
    ]
    request = prepare_request(
        "Сеть цеха",
        [{"evidence_id": str(i), "excerpt": "Справочный материал. " * 70} for i in range(20)],
        history,
    )
    assert request.messages[1:3] == history
    assert len(__import__("json").dumps(request.messages, ensure_ascii=False)) <= 32000
    assert {e["evidence_id"] for e in request.evidence} <= {str(i) for i in range(20)}
