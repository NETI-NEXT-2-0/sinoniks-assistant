"""New extraction version configuration and unsaved scaffold are explicit."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from partner_agent.infra.settings import Settings
from partner_agent.requirements_service import new_project_context
from partner_agent.runtime.rag_provider import prepare_request


def test_new_default_and_environment_version_are_explicit(monkeypatch):
    monkeypatch.delenv("PARTNER_DESIGN_INPUT_VERSION", raising=False)
    assert (
        Settings(database_url="postgresql+psycopg://test/test", _env_file=None).design_input_version
        == 3
    )
    for value in ("2", "3"):
        monkeypatch.setenv("PARTNER_DESIGN_INPUT_VERSION", value)
        assert Settings(
            database_url="postgresql+psycopg://test/test", _env_file=None
        ).design_input_version == int(value)
    monkeypatch.setenv("PARTNER_DESIGN_INPUT_VERSION", "4")
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+psycopg://test/test", _env_file=None)


def test_new_scaffold_is_empty_explicit_and_not_present_in_reference():
    context = new_project_context(UUID("4c40193a-b84f-40b0-958c-5be26c6c8c85"))
    assert len(context["networks"]) == 2
    assert all(
        network["trust"] is None and network["name"] is None for network in context["networks"]
    )
    assert context["flows"] == [] and context["systems"] == []
    assert "sources" not in str(context)
    prepared = prepare_request("Опиши инфраструктуру", [], design_context=context, new_project=True)
    assert prepared.input_version == 3 and not prepared.can_explain_project
    assert "New draft requirements (not yet saved):" in prepared.messages[-1]["content"]
    assert "Saved project conditions:" not in prepared.messages[-1]["content"]
    reference = prepare_request(
        "Справочный вопрос", [], design_context=context, new_project=True, request_mode="reference"
    )
    assert "UNSAVED NEW PROJECT" not in str(reference.messages)
    assert str(context) not in str(reference.messages)
    assert "New draft requirements (not yet saved):" not in str(reference.messages)
