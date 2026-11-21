"""Follow-up retrieval preserves topic; new explicit codes keep exact matching."""

import pytest

from partner_agent.retrieval.service import search_answer_evidence


@pytest.mark.asyncio
async def test_anaphoric_question_uses_prior_topic_before_incidental_generic_hits(monkeypatch):
    calls = []

    async def search(db, actor, query, limit):
        calls.append(query)
        return [{"evidence_id": "product" if "Синоникс" in query else "unrelated"}]

    monkeypatch.setattr("partner_agent.retrieval.service.search_knowledge", search)
    history = [
        {"role": "user", "content": "Что такое Синоникс?"},
        {"role": "assistant", "content": "Система обмена"},
    ]
    result = await search_answer_evidence(None, None, "По какому принципу оно работает?", history)
    assert result == [{"evidence_id": "product"}]
    assert calls == ["Что такое Синоникс?\nПо какому принципу оно работает?"]


@pytest.mark.asyncio
async def test_new_explicit_identifier_does_not_inherit_previous_topic(monkeypatch):
    calls = []

    async def search(db, actor, query, limit):
        calls.append(query)
        return []

    monkeypatch.setattr("partner_agent.retrieval.service.search_knowledge", search)
    result = await search_answer_evidence(
        None, None, "Это ICAP-999?", [{"role": "user", "content": "Что такое ICAP-123?"}]
    )
    assert result == [] and calls == ["Это ICAP-999?"]
