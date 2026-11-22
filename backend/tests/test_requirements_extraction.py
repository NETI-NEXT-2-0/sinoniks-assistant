"""Provider regressions for group identifiers, envelope fields and unknown notes."""

import copy
import json

import pytest
from pydantic import ValidationError

from partner_agent.infra.settings import Settings
from partner_agent.runtime.provider import ProviderError
from partner_agent.runtime.rag_provider import answer_with_provider, prepare_request
from partner_agent.sinoniks_contract19 import Requirements19


def grouped_documents():
    """Synthetic six-group-occurrence case; no private provider report is published."""
    return {
        "schema_version": 3, "id": "project-test",
        "networks": [{"id": "office", "trust": "open"},
                     {"id": "plant", "trust": "closed"}],
        "systems": [{"id": "docs-office", "network_id": "office", "kind": "storage"},
                    {"id": "docs-plant", "network_id": "plant", "kind": "storage"}],
        "flows": [{
            "id": f"files-{index}", "kind": "file", "direction": "forward",
            "sender_network_id": "office", "receiver_network_id": "plant",
            "sender_ids": ["docs-office"], "receiver_ids": ["docs-plant"],
            "sides": [{
                "network_id": network, "manual": False, "usb": False, "checks": [],
                "access_group": group if index < 2 or network == "plant" else None,
                "received_files_shared": True,
                "automations": [{"operation": operation, "protocol": "SMB",
                                 "storage_id": storage}],
            } for network, storage, operation in (
                ("office", "docs-office", "PULL"), ("plant", "docs-plant", "PUSH"),
            )],
        } for index, group in enumerate((
            "shared_documents", "shared_documents", "laboratory_group", "service_group",
        ))],
        "unknowns": [],
        "assumptions": [{"path": "/systems", "target_id": None, "basis": "user_minimum",
                         "explanation": "IP-адреса нужно назначить перед внедрением."}],
    }


def test_v3_instruction_explains_constraints_not_expressible_as_arbitrary_strings():
    request = prepare_request(
        "Сохрани проект и выгрузи draw.io", [],
        design_context={"schema_version": 3, "id": "draft"}, new_project=True,
    )
    instruction = request.messages[0]["content"]
    group_schema = Requirements19.model_json_schema()["$defs"]["FileSide19"]["properties"][
        "access_group"
    ]["anyOf"][0]
    assert group_schema["pattern"] == "^[a-zA-Z0-9_-]{1,80}$"
    assert group_schema["pattern"] in instruction
    assert "preserve existing group IDs" in instruction
    assert "NEVER inside design" in instruction
    assert "Saving a project, calculating rules" in instruction
    assert "IP addresses and ICAP service URLs are not fields" in instruction
    assert len(json.dumps(request.messages, ensure_ascii=False)) <= 32000


def test_seven_structural_errors_do_not_mask_the_following_unknown_conflict():
    proposal = grouped_documents()
    for flow in proposal["flows"]:
        for side in flow["sides"]:
            if side["access_group"] is not None:
                side["access_group"] = "группа документов"
    proposal["vendor_wishes"] = []
    proposal["unknowns"] = [{"path": "/systems", "target_id": None, "reason": "missing"}]
    with pytest.raises(ValidationError) as invalid:
        Requirements19.model_validate(proposal)
    assert invalid.value.error_count() == 7
    assert [issue["type"] for issue in invalid.value.errors()] == (
        ["string_pattern_mismatch"] * 6 + ["extra_forbidden"]
    )
    # This is an explicit test correction, never a production coercion of model output.
    proposal["flows"] = grouped_documents()["flows"]
    proposal.pop("vendor_wishes")
    with pytest.raises(ValidationError, match="unknown note cannot hide a supplied value"):
        Requirements19.model_validate(proposal)
    proposal["unknowns"] = []
    assert Requirements19.model_validate(proposal).assumptions[0].path == "/systems"


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", [None, "group", "nested_wishes", "unknown", "reference",
                                   "stale_missing"])
async def test_provider_accepts_separated_envelope_without_repairing_invalid_design(
    monkeypatch, defect,
):
    proposal = grouped_documents()
    if defect == "group":
        proposal["flows"][0]["sides"][0]["access_group"] = "общие документы"
    elif defect == "nested_wishes":
        proposal["vendor_wishes"] = []
    elif defect == "unknown":
        proposal["unknowns"] = [{"path": "/systems", "target_id": None,
                                 "reason": "user_unknown"}]
    elif defect == "stale_missing":
        proposal["unknowns"] = [{"path": "/systems", "target_id": None, "reason": "missing"}]
    elif defect == "reference":
        proposal["flows"][0]["receiver_ids"] = ["nonexistent"]
    original = copy.deepcopy(proposal)
    wishes = [{"kind": "free_text", "text": "Другое: особый профиль"}]
    calls = []

    async def complete(settings, messages):
        calls.append(messages)
        return {"choices": [{"message": {"content": json.dumps({
            "answer": "Условия проекта", "mode": "design", "citations": [],
            "design": proposal, "vendor_wishes": wishes,
        })}}], "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", complete)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    options = {"design_context": {"schema_version": 3, "id": "draft"},
               "request_mode": "design"}
    if defect and defect != "stale_missing":
        with pytest.raises(ProviderError, match="provider_invalid_design") as invalid:
            await answer_with_provider(settings, "Проект", [], **options)
        assert invalid.value.usage["total_tokens"] == 30
        assert invalid.value.diagnostics["stage"] == "design_validation"
    else:
        result = await answer_with_provider(settings, "Проект", [], **options)
        assert result["vendor_wishes"] == wishes
        expected = copy.deepcopy(proposal)
        if defect == "stale_missing":
            expected["unknowns"] = []
        assert result["design"] == Requirements19.model_validate(expected)
        assert result["design"].requests_for_vendor == []
    assert len(calls) == 1
    assert proposal == original
