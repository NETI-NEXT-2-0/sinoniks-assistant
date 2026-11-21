import json

import pytest

from partner_agent.infra.settings import Settings
from partner_agent.runtime.provider import ProviderError
from partner_agent.runtime.rag_provider import (
    answer_with_provider,
    build_messages,
    prepare_request,
    validate_citations,
)


def test_build_messages_and_validate_allowed_citation():
    evidence = [{"evidence_id": "u1", "excerpt": "Python course"}]
    messages = build_messages("What course?", evidence)
    assert "[source_1]" in messages[1]["content"]
    result = validate_citations(
        {"answer": "Course", "citations": [{"evidence_id": "u1",
          "quote": "Python course"}]}, evidence
    )
    assert result["answer"] == "Course"


def test_reference_prompt_includes_only_readonly_project_context():
    messages = build_messages("Product question", [], design_context={"network_a": "Saved plant"},
                              request_mode="reference")
    assert "Saved plant" in str(messages) and "REFERENCE mode" in messages[0]["content"]
    assert "Read-only saved project conditions:" in messages[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,returned,proposal,allowed", [
    ("reference", "design", {}, False),
    ("reference", "conversation", None, True),
    ("design", "conversation", None, False),
    ("design", "insufficient", None, True),
    ("design", "design", {}, True),
])
async def test_explicit_request_mode_is_enforced(
    monkeypatch, requested, returned, proposal, allowed,
):
    async def model(settings, messages):
        return {"choices": [{"message": {"content": json.dumps({
            "answer": "Synthetic", "citations": [], "mode": returned, "design": proposal,
        })}}]}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    if allowed:
        result = await answer_with_provider(settings, "Question", [], request_mode=requested)
        assert result["answer_mode"] == returned
    else:
        with pytest.raises(ProviderError, match="provider_request_mode_violation"):
            await answer_with_provider(settings, "Question", [], request_mode=requested)

def test_validate_rejects_unauthorized_citation():
    with pytest.raises(ProviderError, match="provider_unauthorized_citation"):
        validate_citations(
            {"answer": "x", "citations": [{"evidence_id": "secret", "quote": "x"}]},
            [{"evidence_id": "u1", "excerpt": "x"}],
        )


@pytest.mark.parametrize("quote", ["", " \n ", "The price is 900.", "the price is 100."])
def test_paraphrased_citation_displays_real_source_not_generated_quote(quote):
    result = validate_citations(
        {"answer": "Price", "citations": [{"evidence_id": "u1", "quote": quote}]},
        [{"evidence_id": "u1", "excerpt": "The price is 100."}],
    )
    assert result["citations"] == [{"evidence_id": "u1", "quote": "The price is 100."}]


def test_model_can_select_a_source_without_retyping_its_text():
    result = validate_citations(
        {"answer": "Пересказ", "citations": [{"evidence_id": "u1"}]},
        [{"evidence_id": "u1", "excerpt": "Разрешённый фрагмент источника."}],
    )
    assert result["citations"][0]["quote"] == "Разрешённый фрагмент источника."


def test_citation_allows_layout_whitespace_without_changing_its_words():
    result = validate_citations(
        {"answer": "Course", "citations": [{"evidence_id": "u1", "quote": "Python course"}]},
        [{"evidence_id": "u1", "excerpt": "The Python\r\n  course costs 100."}],
    )
    assert result["citations"][0]["quote"] == "Python course"


@pytest.mark.asyncio
@pytest.mark.parametrize(("locator", "title", "location"), [
    ({"line": 8}, "Справочник", "Строка источника 8"),
    (
        {"positions": [{"page": 4}, {"page": 4}],
         "context": [{"positions": [{"page": 6}]}]},
        "Справочник", "Страницы фрагмента: 4, 6",
    ),
    (
        {"reviewed_copy": True, "start": 19, "end": 34, "positions": [{"page": 1}]},
        "Проверенная редакция · Справочник", "Символы проверенной редакции 20–34",
    ),
    (
        {"reviewed_copy": True, "section": "Правила обмена", "start": 19, "end": 34},
        "Проверенная редакция · Справочник", "Раздел проверенной редакции: Правила обмена",
    ),
    ({"line": True}, "Справочник", None),
])
async def test_answer_citation_uses_servers_title_and_location(
    monkeypatch, locator, title, location,
):
    async def complete(settings, messages):
        payload = {
            "answer": "Есть подтверждение", "mode": "grounded",
            "citations": [{
                "evidence_id": "ref", "quote": "Подтверждённый факт",
                "title": "Подставное название", "location": "Подставная позиция",
            }],
        }
        return {"choices": [{"message": {"content": json.dumps(payload)}}]}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", complete)
    evidence = [{
        "evidence_id": "ref", "excerpt": "Подтверждённый факт", "title": "Справочник",
        "locator": locator,
    }]
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    result = await answer_with_provider(settings, "Вопрос по документу", evidence)
    citation, = result["citations"]
    assert citation["title"] == title
    assert citation.get("location") == location


@pytest.mark.parametrize("result", [[], None, {"answer": "x" * 12001, "citations": []},
    {"answer": "ok", "citations": [{"evidence_id": "u1", "quote": "x"}] * 51},
    {"answer": "ok", "citations": [{"evidence_id": [], "quote": "x"}]}])
def test_invalid_or_oversized_answers_are_rejected(result):
    with pytest.raises(ProviderError):
        validate_citations(result, [{"evidence_id": "u1", "excerpt": "x"}])


def test_insufficient_answer_can_have_no_citations():
    assert validate_citations(
        {"answer": "Insufficient evidence.", "citations": []}, []
    )["citations"] == []


def test_history_preserves_pairs_and_current_question_exactly_once():
    history = [{"role": "user", "content": "Previous question"},
               {"role": "assistant", "content": "Previous answer"}]
    messages = build_messages("Current question", [], history)
    assert messages[1:3] == history
    assert messages[-1]["content"].count("Current question") == 1
    assert len(messages) == 4


def test_context_limit_removes_oldest_complete_exchanges():
    history = [{"role": role, "content": str(index) * 7000}
               for index in range(4) for role in ("user", "assistant")]
    messages = build_messages("Current", [], history)
    assert 4 <= len(messages) < len(history) + 2
    assert len(messages) % 2 == 0
    assert messages[-3:-1] == history[-2:]
    assert len(json.dumps(messages, ensure_ascii=False)) <= 32000
    assert history[0]["content"] == "0" * 7000
    with pytest.raises(ProviderError, match="rag_context_limit"):
        build_messages("x" * 32000, [], history)


def large_project():
    return {
        "schema_version": 2, "network_a": "Производство", "network_b": "Офис", "firewall": "B",
        "exchanges": [{
            "requirement_id": str(index).zfill(80), "sender_side": "A", "sender": "Д" * 120,
            "receiver": "М" * 120, "service": "С" * 120, "policy": "П" * 120,
            "mode": "diode", "protocol": "TCP", "destination_port": 1000 + index,
        } for index in range(24)],
    }


def test_large_project_preserves_conditions_and_whole_ranked_excerpts_before_history():
    project = large_project()
    evidence = [{"evidence_id": f"source-{i}", "excerpt": str(i).zfill(4) * 400}
                for i in range(20)]
    history = [{"role": role, "content": "OLD" * 2000} for role in ("user", "assistant")]
    prepared = prepare_request("Измени только контур B", evidence, history, design_context=project)
    assert len(json.dumps(prepared.messages, ensure_ascii=False)) <= 32000
    assert 0 < len(prepared.evidence) < len(evidence)
    assert prepared.evidence == evidence[:len(prepared.evidence)]
    assert len(prepared.messages) == 2
    content = prepared.messages[-1]["content"]
    assert json.loads(content.split("Saved project conditions:\n")[1]) == project
    for alias, evidence_id in prepared.citation_aliases.items():
        item = next(item for item in prepared.evidence if item["evidence_id"] == evidence_id)
        assert f"[{alias}] {item['excerpt']}" in content
    assert "[source-19]" not in content
    assert len(evidence) == 20 and len(history) == 2


@pytest.mark.asyncio
async def test_model_cannot_cite_a_retrieved_excerpt_omitted_from_the_actual_request(monkeypatch):
    evidence = [{"evidence_id": f"source-{i}", "excerpt": "x" * 1600} for i in range(20)]

    async def model(settings, messages):
        assert "[source-19]" not in messages[-1]["content"]
        return {"choices": [{"message": {"content": json.dumps({
            "answer": "Unsupported", "mode": "grounded",
            "citations": [{"evidence_id": "source-19", "quote": "x"}],
        })}}]}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    with pytest.raises(ProviderError, match="provider_unauthorized_citation"):
        await answer_with_provider(settings, "Question", evidence, design_context=large_project())


@pytest.mark.asyncio
async def test_prepared_request_cannot_override_explicit_reference_mode(monkeypatch):
    async def model(settings, messages):
        pytest.fail("No provider call is allowed for a mismatched prepared request")

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    prepared = prepare_request("Design", [], request_mode="design")
    with pytest.raises(ProviderError, match="provider_request_mode_violation"):
        await answer_with_provider(
            settings, "Design", [], prepared=prepared, request_mode="reference",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "snapshot", "proposal", "error"), [
    ("auto", {"revision": 1}, None, None),
    ("design", {"revision": 1}, None, None),
    ("reference", {"revision": 1}, None, None),
    ("reference", None, None, "provider_request_mode_violation"),
    ("reference", {"revision": 1}, {}, "provider_request_mode_violation"),
    ("auto", None, None, "provider_request_mode_violation"),
    ("design", {"pending_fields": ["x" * 32000]}, None, "provider_request_mode_violation"),
    ("auto", {"revision": 1}, {}, "provider_invalid_design_mode"),
])
async def test_project_explanation_requires_snapshot_and_cannot_propose_changes(
    monkeypatch, mode, snapshot, proposal, error,
):
    async def model(settings, messages):
        assert ("Saved calculation snapshot:" in messages[-1]["content"]) == (
            snapshot is not None and len(json.dumps(snapshot)) < 32000
        )
        return {"choices": [{"message": {"content": json.dumps({
            "answer": "В схеме указан один получатель.",
            "citations": [],
            "mode": "project_explanation",
            "design": proposal,
        })}}]}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    prepared = prepare_request(
        "Кто получает журналы?", [], design_context={"network_a": "Saved"},
        project_calculation=snapshot, request_mode=mode,
    )
    if error is not None:
        with pytest.raises(ProviderError, match=error):
            await answer_with_provider(
                settings, "Question", [], prepared=prepared, request_mode=mode,
            )
    else:
        result = await answer_with_provider(settings, "Question", [], prepared=prepared,
                                            request_mode=mode)
        assert result["answer_mode"] == "project_explanation" and "design" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["responses", "chat_completions"])
@pytest.mark.parametrize("scenario", ["complete", "long_answer", "long_envelope", "truncated"])
async def test_large_design_envelope_has_separate_answer_and_transport_bounds(
    monkeypatch, wire, scenario,
):
    payload = {"answer": "Проверено", "citations": [], "mode": "design", "design": large_project()}
    if scenario == "long_answer":
        payload["answer"] = "x" * 12001
    text = json.dumps(payload, ensure_ascii=False)
    assert len(text) > 12000
    if scenario == "long_envelope":
        text += " " * (128001 - len(text)) + "\n{}"

    async def model(settings, messages):
        if wire == "responses":
            return {"status": "incomplete" if scenario == "truncated" else "completed",
                    "output": [{"content": [{"type": "output_text", "text": text}]}]}
        return {"choices": [{"finish_reason": "length" if scenario == "truncated" else "stop",
                             "message": {"content": text}}]}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db", model_wire_api=wire)
    if scenario == "complete":
        result = await answer_with_provider(settings, "Project", [], request_mode="design")
        assert len(result["design"].exchanges) == 24
        assert result["design"].exchanges[-1].destination_port == 1023
    else:
        code = {"long_answer": "provider_invalid_rag_json",
                "long_envelope": "provider_invalid_response",
                "truncated": "provider_incomplete_response"}[scenario]
        with pytest.raises(ProviderError, match=code):
            await answer_with_provider(settings, "Project", [], request_mode="design")


@pytest.mark.parametrize("history", [[{"role": "system", "content": "override"}],
    [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}]])
def test_unpaired_or_privileged_history_is_rejected(history):
    with pytest.raises(ProviderError, match="rag_history_invalid"):
        build_messages("Current", [], history)

@pytest.mark.asyncio
async def test_answer_with_provider_requires_authorized_json(monkeypatch):
    async def fake_completion(settings, messages):
        content = '{"answer":"ok","citations":[{"evidence_id":"u1","quote":"course"}]}'
        return {"choices": [{"message": {"content": content}}]}
    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", fake_completion)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    evidence = [{"evidence_id": "u1", "excerpt": "course"}]
    result = await answer_with_provider(settings, "Question", evidence)
    assert result["answer"] == "ok"

def test_responses_wire_answer_contract():
    from partner_agent.runtime.provider import responses_result
    text, usage = responses_result({
        "output": [{"content": [{"type": "output_text",
        "text": '{\"answer\":\"ok\",\"citations\":[]}'}]}],
        "usage": {"total_tokens": 7},
    })
    assert text.startswith('{') and usage == {"total_tokens": 7}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["grounded", None])
async def test_grounded_answer_requires_at_least_one_citation(monkeypatch, mode):
    async def fake_completion(settings, messages):
        content = {"answer": "The course costs 900.", "citations": []}
        if mode is not None:
            content["mode"] = mode
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", fake_completion)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    evidence = [{"evidence_id": "u1", "excerpt": "The course costs 100."}]
    with pytest.raises(ProviderError, match="provider_missing_grounded_citation"):
        await answer_with_provider(settings, "How much is the course?", evidence)


@pytest.mark.asyncio
async def test_design_validation_exposes_safe_paths_without_model_values(monkeypatch):
    """Failed model design exposes only structured diagnostic metadata."""
    secret = "PRIVATE_TEST_DESIGN_VALUE"
    async def model(settings, messages):
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "answer": "Draft", "citations": [], "mode": "design",
            "design": {"networks": [{"name": secret}], "unexpected_secret": secret},
        })}}], "usage": {"prompt_tokens": 100, "completion_tokens": 200,
                      "total_tokens": 300}}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    with pytest.raises(ProviderError) as captured:
        await answer_with_provider(settings, "Design", [], request_mode="design")
    error = captured.value
    assert error.code == "provider_invalid_design"
    assert error.usage == {"prompt_tokens": 100, "completion_tokens": 200,
                           "total_tokens": 300}
    assert error.diagnostics["stage"] == "design_validation"
    assert error.diagnostics["issue_count"] >= 1
    assert error.diagnostics["issues"]
    assert secret not in json.dumps(error.diagnostics)


def test_developer_diagnostics_defaults_off_and_rejects_production():
    from pydantic import ValidationError

    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db")
    assert not settings.developer_diagnostics_enabled
    enabled = Settings(database_url="postgresql+psycopg://u:p@localhost/db",
                       developer_diagnostics_enabled=True)
    assert enabled.developer_diagnostics_enabled
    with pytest.raises(ValidationError, match="developer diagnostics cannot be enabled"):
        Settings(database_url="postgresql+psycopg://u:p@localhost/db",
                 environment="production", public_origin="https://example.com",
                 developer_diagnostics_enabled=True)

@pytest.mark.parametrize(("mutation", "expected_rule"), [
    (lambda d: d["systems"][0].update(id="office"), "duplicate_global_identifier"),
    (lambda d: d["flows"][0].update(receiver_ids=["missing-system"]), "unknown_reference"),
    (lambda d: d["flows"][0].update(receiver_network_id="plant"), "flow_not_crossing_boundary"),
])
def test_root_design_validation_includes_precise_safe_rule(mutation, expected_rule):
    from pydantic import ValidationError

    from partner_agent.runtime.rag_provider import safe_validation_diagnostics
    from partner_agent.sinoniks_contract19 import Requirements19

    document = {
        "schema_version": 3, "id": "project",
        "networks": [{"id": "office"}, {"id": "plant"}],
        "systems": [{"id": "sender", "network_id": "plant"},
                    {"id": "receiver", "network_id": "office"}],
        "flows": [{"id": "logs", "kind": "data", "sender_network_id": "plant",
                   "receiver_network_id": "office", "sender_ids": ["sender"],
                   "receiver_ids": ["receiver"], "purpose": "Logs", "mode": "one-way",
                   "protocol": "UDP", "destination_port": 514}],
    }
    mutation(document)
    with pytest.raises(ValidationError) as caught:
        Requirements19.model_validate(document)
    diagnostics = safe_validation_diagnostics(caught.value)
    assert diagnostics["issues"][0]["path"] == []
    assert diagnostics["issues"][0]["type"] == expected_rule
    assert diagnostics["issues"][0]["rule"] == expected_rule
    assert diagnostics["issues"][0]["validator"] == "Requirements19.references"
    assert diagnostics["issues"][0]["message"]
    assert "model_response" not in diagnostics

@pytest.mark.asyncio
async def test_full_developer_diagnostics_include_validation_reason_and_design(monkeypatch):
    raw = {"answer": "Draft", "citations": [], "mode": "design",
           "design": {"id": "draft", "networks": [{"id": "plant"}],
                      "systems": [{"id": "sender", "network_id": "unknown-network"}]}}
    async def model(settings, messages):
        return {"choices": [{"message": {"content": json.dumps(raw)}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 50, "total_tokens": 70}}
    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost/db",
                        developer_diagnostics_enabled=True)
    with pytest.raises(ProviderError) as caught:
        await answer_with_provider(settings, "Design", [], request_mode="design",
                                   design_context={"schema_version": 3, "id": "draft"})
    diagnostics = caught.value.diagnostics
    assert diagnostics["model_response"] == raw
    assert diagnostics["issues"][0]["rule"] == "unknown_reference"
    assert "unknown-network" in diagnostics["issues"][0]["message"]


@pytest.mark.asyncio
async def test_successful_insufficient_answer_has_opt_in_developer_response(monkeypatch):
    """A valid refusal in Project mode is still observable on the local debug stand."""
    response = {
        "answer": "Cannot provide a full updated project", "citations": [],
        "mode": "insufficient", "design": None,
    }

    async def model(settings, messages):
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(response),
        }}], "usage": {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16}}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    base = {"database_url": "postgresql+psycopg://u:p@localhost/db"}
    ordinary = await answer_with_provider(
        Settings(**base), "Update existing project", [], request_mode="design",
    )
    assert ordinary["answer_mode"] == "insufficient"
    assert "developer_model_response" not in ordinary
    local = await answer_with_provider(
        Settings(**base, developer_diagnostics_enabled=True),
        "Update existing project", [], request_mode="design",
    )
    assert local["answer_mode"] == "insufficient"
    assert "design" not in local
    assert local["developer_model_response"] == response
    assert local["usage"]["total_tokens"] == 16

@pytest.mark.parametrize("citation", [{"evidence_id": "source_1"}, "source_1", "[source_1]"])
def test_ephemeral_source_handles_resolve_only_to_selected_authorized_evidence(citation):
    prepared = prepare_request(
        "Вопрос", [{"evidence_id": "actual-id", "excerpt": "Подлинный текст"}],
    )
    assert "[source_1]" in prepared.messages[-1]["content"]
    assert "actual-id" not in prepared.messages[-1]["content"]
    result = validate_citations({"answer": "Пересказ", "citations": [citation]}, prepared.evidence,
                                prepared.citation_aliases)
    assert result["citations"] == [{"evidence_id": "actual-id", "quote": "Подлинный текст"}]
    with pytest.raises(ProviderError, match="provider_unauthorized_citation"):
        validate_citations({"answer": "Чужой", "citations": ["source_2"]}, prepared.evidence,
                           prepared.citation_aliases)
