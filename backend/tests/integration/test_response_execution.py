import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane

from partner_agent.infra.db import session_factory
from partner_agent.infra.models import Group, GroupMember, Membership, ModelUsageDaily, Workspace
from partner_agent.infra.models_documents import (
    DisclosureAnswerDeny,
    DisclosureGroupDeny,
    DisclosureUnit,
    Document,
    DocumentGrant,
    DocumentGroupGrant,
    DocumentVersion,
)
from partner_agent.infra.models_stage2 import ContextManifest
from partner_agent.infra.settings import get_settings
from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def corpus(plane):
    curator, curator_headers, owner_id = await plane("curator")
    client, headers, user_id = await plane()
    document_id, version_id, unit_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with session_factory()() as db:
        db.add(Document(id=document_id, owner_id=owner_id, scope="knowledge", title="Synthetic"))
        await db.flush()
        db.add(DocumentVersion(
            id=version_id, document_id=document_id, generation=1, source_sha256="a" * 64,
            media_type="text/plain", size_bytes=10, original_key=f"{version_id}/original",
            canonical_key=f"{version_id}/canonical", canonical_sha256="b" * 64,
            approved_canonical_sha256="b" * 64, pipeline_version="canonical-v1",
            status="ready_for_review",
        ))
        await db.flush()
        document = await db.get(Document, document_id)
        document.active_version_id = version_id
        db.add(DisclosureUnit(
            id=unit_id, version_id=version_id, block_id=uuid.uuid4(),
            text="Synthetic course costs 100.", text_sha256="c" * 64,
            locator={"line": 1}, policy_revision=1,
        ))
        db.add(DocumentGrant(document_id=document_id, user_id=user_id,
                             allow_answer=True, allow_original=False))
        await db.commit()
    yield curator, curator_headers, client, headers, user_id, document_id, version_id, unit_id
    async with session_factory()() as db:
        document = await db.get(Document, document_id)
        document.active_version_id = None
        document.last_published_version_id = None
        await db.flush()
        await db.execute(delete(DocumentGrant).where(DocumentGrant.document_id == document_id))
        await db.execute(
            delete(DisclosureAnswerDeny).where(DisclosureAnswerDeny.unit_id == unit_id)
        )
        await db.execute(delete(DisclosureUnit).where(DisclosureUnit.version_id == version_id))
        await db.execute(delete(DocumentVersion).where(DocumentVersion.id == version_id))
        await db.execute(delete(Document).where(Document.id == document_id))
        await db.commit()


def configure_execution(monkeypatch):
    for key, value in {
        "PARTNER_RUNTIME_PROFILE": "openai_compatible",
        "PARTNER_MODEL_ENDPOINT": "https://test.invalid/v1",
        "PARTNER_MODEL_NAME": "synthetic",
        "PARTNER_MODEL_API_KEY": "synthetic",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


async def test_corpus_summary_distinguishes_publication_from_search_eligibility(corpus):
    curator, _, _, _, _, _, version_id, unit_id = corpus
    path = "/api/v1/knowledge/summary"
    baseline = (await curator.get(path)).json()
    assert baseline["published_documents"] == baseline["searchable_excerpts"] == 1
    async with session_factory()() as db:
        version = await db.get(DocumentVersion, version_id)
        version.status = "processing"
        await db.commit()
    rebuilding = (await curator.get(path)).json()
    assert rebuilding["published_documents"] == 1
    assert rebuilding["searchable_documents"] == rebuilding["searchable_excerpts"] == 0
    async with session_factory()() as db:
        version = await db.get(DocumentVersion, version_id)
        version.status = "ready_for_review"
        document = await db.get(Document, version.document_id)
        db.add(DisclosureAnswerDeny(unit_id=unit_id, user_id=document.owner_id))
        await db.commit()
    assert (await curator.get(path)).json() == rebuilding


@pytest.mark.parametrize(
    "scenario", ["new_topic", "new_identifier", "unknown_identifier", "followup"]
)
async def test_current_question_controls_search_identifiers(corpus, monkeypatch, scenario):
    _, _, client, headers, _, _, version_id, unit_id = corpus
    other_id = uuid.uuid4()
    async with session_factory()() as db:
        db.add(DisclosureUnit(
            id=other_id, version_id=version_id, block_id=uuid.uuid4(),
            text=("Synthetic advanced course costs 200."
                  if scenario in ("new_identifier", "unknown_identifier")
                  else "Submit the enrollment application using the form."),
            text_sha256="d" * 64, locator={"line": 2}, policy_revision=1,
        ))
        await db.commit()
    configure_execution(monkeypatch)
    observed = []

    async def local_completion(settings, question, evidence, *, history=None, prepared=None):
        observed.append({item["evidence_id"] for item in evidence})
        return {"answer": "Synthetic test answer.", "citations": [], "usage": {}}

    monkeypatch.setattr("partner_agent.runtime.runner.answer_with_provider", local_completion)
    chat = await new_chat(client, headers)
    first = (await submit(client, headers, chat, text="Synthetic 100")).json()
    assert (await execute_run(uuid.UUID(first["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    question, expected = {
        "new_topic": ("How to submit an enrollment application?", {str(other_id)}),
        "new_identifier": ("Synthetic 200", {str(other_id)}),
        "unknown_identifier": ("Synthetic 999", set()),
        "followup": ("А какова стоимость?", {str(unit_id)}),
    }[scenario]
    second = (await submit(client, headers, chat, text=question)).json()
    assert (await execute_run(uuid.UUID(second["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    assert observed == [{str(unit_id)}, expected]


@pytest.mark.parametrize("change", ["none", "grant", "deny", "unpublish", "version", "text"])
async def test_final_answer_rechecks_all_source_context(corpus, monkeypatch, change):
    curator, curator_headers, client, headers, user_id, document_id, version_id, unit_id = corpus
    chat = await new_chat(client, headers)
    admitted = (await submit(client, headers, chat, text="Synthetic course")).json()
    configure_execution(monkeypatch)

    async def local_completion(settings, question, evidence, *, history=None, prepared=None):
        assert any(item["evidence_id"] == str(unit_id) for item in evidence)
        if change == "grant":
            response = await curator.put(
                f"/api/v1/knowledge/documents/{document_id}/grants/{user_id}",
                headers=curator_headers,
                json={"expected_revision": 1, "allow_answer": False, "allow_original": False},
            )
            assert response.status_code == 200
        else:
            async with session_factory()() as db:
                document = await db.get(Document, document_id)
                if change == "deny":
                    db.add(DisclosureAnswerDeny(unit_id=unit_id, user_id=user_id))
                elif change == "unpublish":
                    document.active_version_id = None
                elif change == "version":
                    version = await db.get(DocumentVersion, version_id)
                    version.status = "processing"
                elif change == "text":
                    unit = await db.get(DisclosureUnit, unit_id)
                    unit.text = "Changed source."
                await db.commit()
        return {"answer": "Synthetic course costs 100.", "citations": [], "usage": {}}

    monkeypatch.setattr("partner_agent.runtime.runner.answer_with_provider", local_completion)
    result = await execute_run(uuid.UUID(admitted["run_id"]), dispatch_generation=0)
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    if change == "none":
        assert result["status"] == "succeeded"
        assert "Synthetic course costs 100." in str(snapshot)
        async with session_factory()() as db:
            manifest = await db.scalar(select(ContextManifest).where(
                ContextManifest.run_id == uuid.UUID(admitted["run_id"])
            ))
            assert manifest.evidence_ids == [str(unit_id)]
    else:
        assert result["status"] == "failed"
        assert "source_context_changed" in str(snapshot)
        assert "Synthetic course costs 100." not in str(snapshot)


@pytest.mark.parametrize("revocation", ["none", "before", "during", "legacy"])
async def test_history_uses_completed_authorized_turns_only(corpus, monkeypatch, revocation):
    curator, curator_headers, client, headers, user_id, document_id, _, _ = corpus
    chat = await new_chat(client, headers)
    configure_execution(monkeypatch)
    calls = []

    async def revoke():
        response = await curator.put(
            f"/api/v1/knowledge/documents/{document_id}/grants/{user_id}",
            headers=curator_headers,
            json={"expected_revision": 1, "allow_answer": False, "allow_original": False},
        )
        assert response.status_code == 200

    async def local_completion(settings, question, evidence, *, history=None, prepared=None):
        calls.append((question, history))
        if len(calls) == 2:
            assert "Queued secret" not in str(history)
            if revocation in ("before", "legacy"):
                assert history == []
            else:
                assert [item["role"] for item in history] == ["user", "assistant"]
                assert history[0]["content"] == "Synthetic course"
                assert history[1]["content"] == "Synthetic course costs 100."
                assert evidence
            if revocation == "during":
                await revoke()
        return {"answer": "Synthetic course costs 100." if evidence else "Insufficient evidence.",
                "citations": [], "usage": {}}

    monkeypatch.setattr("partner_agent.runtime.runner.answer_with_provider", local_completion)
    first = (await submit(client, headers, chat, text="Synthetic course")).json()
    assert (await execute_run(uuid.UUID(first["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    if revocation == "before":
        await revoke()
    elif revocation == "legacy":
        async with session_factory()() as db:
            manifest = await db.scalar(select(ContextManifest).where(
                ContextManifest.run_id == uuid.UUID(first["run_id"])
            ))
            manifest.evidence_ids = []
            await db.commit()
    second = (await submit(client, headers, chat, text="А какова стоимость?")).json()
    await submit(client, headers, chat, text="Queued secret")
    result = await execute_run(uuid.UUID(second["run_id"]), dispatch_generation=0)
    assert result["status"] == ("failed" if revocation == "during" else "succeeded")
    assert len(calls) == 2


@pytest.mark.parametrize("usage,expected", [({}, 20000),
    ({"prompt_tokens": 1000, "completion_tokens": 500}, 4000)])
async def test_missing_usage_keeps_reservation_instead_of_refunding(corpus, monkeypatch,
                                                                  usage, expected):
    _, _, client, headers, _, _, _, _ = corpus
    configure_execution(monkeypatch)
    name = "usage-" + uuid.uuid4().hex
    for key, value in {"PARTNER_MODEL_DAILY_CAP_USD": "1",
                       "PARTNER_MODEL_INPUT_USD_PER_MILLION": "2",
                       "PARTNER_MODEL_OUTPUT_USD_PER_MILLION": "4",
                       "PARTNER_MODEL_NAME": name}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    async def local_completion(settings, question, evidence, *, history=None, prepared=None):
        return {"answer": "Synthetic course costs 100.", "citations": [], "usage": usage}

    monkeypatch.setattr("partner_agent.runtime.runner.answer_with_provider", local_completion)
    try:
        chat = await new_chat(client, headers)
        admitted = (await submit(client, headers, chat, text="Synthetic course")).json()
        result = await execute_run(uuid.UUID(admitted["run_id"]), dispatch_generation=0)
        assert result["status"] == "succeeded"
        async with session_factory()() as db:
            row = await db.scalar(select(ModelUsageDaily).where(ModelUsageDaily.model == name))
            assert row.spent_usd_micros == expected
        outcome = (await client.get(f"/api/v1/runs/{admitted['run_id']}")).json()["outcome"]
        assert outcome["usage_complete"] == bool(usage)
        assert outcome["estimated_cost_usd"] == (0.004 if usage else None)
    finally:
        async with session_factory()() as db:
            await db.execute(delete(ModelUsageDaily).where(ModelUsageDaily.model == name))
            await db.commit()


@pytest.mark.parametrize(
    ("scenario", "cap", "calls_expected", "code", "spent"),
    [
        ("estimate", "0.01", 0, "provider_request_cap_exceeded", None),
        ("actual", "0.03", 1, "provider_request_cap_exceeded", 42000),
        ("citation", "0.03", 1, "provider_invalid_citation_quote", 4000),
        ("json", "0.03", 1, "provider_invalid_rag_json", 4000),
        ("midnight", "0.03", 1, "provider_invalid_rag_json", 4000),
    ],
)
async def test_model_limit_and_rejected_usage(corpus, monkeypatch, scenario, cap,
                                              calls_expected, code, spent):
    _, _, client, headers, _, _, _, unit_id = corpus
    configure_execution(monkeypatch)
    name = "budget-" + uuid.uuid4().hex
    for key, value in {
        "PARTNER_MODEL_DAILY_CAP_USD": "1",
        "PARTNER_MODEL_REQUEST_CAP_USD": cap,
        "PARTNER_MODEL_INPUT_USD_PER_MILLION": "2",
        "PARTNER_MODEL_OUTPUT_USD_PER_MILLION": "4",
        "PARTNER_MODEL_NAME": name,
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    usage = {"prompt_tokens": 1000, "completion_tokens": 10000 if scenario == "actual" else 500}
    calls = 0
    before_midnight = datetime(2026, 11, 16, 23, 59, 59, tzinfo=UTC)

    class LedgerClock:
        @staticmethod
        def now(tz):
            at = datetime(2026, 11, 17, tzinfo=UTC) if calls else before_midnight
            return at.astimezone(tz)

    if scenario == "midnight":
        monkeypatch.setattr("partner_agent.runtime.ledger.datetime", LedgerClock)

    async def local_completion(settings, messages):
        nonlocal calls
        calls += 1
        payload = {"answer": "Synthetic course costs 100.", "mode": "grounded", "citations": [
            {"evidence_id": str(unit_id),
             "quote": "The course costs 900." if scenario == "citation" else
             "Synthetic course costs 100."},
        ]}
        content = "{bad JSON" if scenario in ("json", "midnight") else json.dumps(payload)
        return {"choices": [{"message": {"content": content}}], "usage": usage}

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", local_completion)
    try:
        chat = await new_chat(client, headers)
        admitted = await submit(client, headers, chat, text="Synthetic course")
        assert admitted.status_code == 202
        result = await execute_run(uuid.UUID(admitted.json()["run_id"]), dispatch_generation=0)
        assert result["status"] == "failed" and calls == calls_expected
        snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
        outcome = snapshot["runs"][0]["outcome"]
        assert outcome["code"] == code
        assert not any(message["role"] == "assistant" for message in snapshot["messages"])
        async with session_factory()() as db:
            row = await db.scalar(select(ModelUsageDaily).where(ModelUsageDaily.model == name))
            if spent is None:
                assert row is None and "usage" not in outcome
            else:
                assert row.spent_usd_micros == spent and row.request_count == 1
                assert outcome["usage"] == usage and outcome["usage_complete"] is True
                assert outcome["estimated_cost_usd"] == spent / 1_000_000
                if scenario == "midnight":
                    assert row.usage_day.date() == before_midnight.date()
    finally:
        async with session_factory()() as db:
            await db.execute(delete(ModelUsageDaily).where(ModelUsageDaily.model == name))
            await db.commit()


@pytest.mark.parametrize("change", ["grant", "member", "deny"])
async def test_group_changes_during_execution_block_final_answer(corpus, plane, monkeypatch,
                                                                 change):
    curator, curator_headers, client, headers, user_id, document_id, version_id, unit_id = corpus
    admin, admin_headers, _ = await plane("admin")
    workspace_id, group_id = uuid.uuid4(), uuid.uuid4()
    async with session_factory()() as db:
        document = await db.get(Document, document_id)
        owner_id = document.owner_id
        db.add(Workspace(id=workspace_id, name="Synthetic"))
        await db.flush()
        document.workspace_id = workspace_id
        db.add_all([Membership(workspace_id=workspace_id, user_id=identifier)
                    for identifier in (owner_id, user_id)])
        db.add(Group(id=group_id, workspace_id=workspace_id, name="Synthetic"))
        await db.flush()
        await db.execute(delete(DocumentGrant).where(DocumentGrant.document_id == document_id))
        db.add(DocumentGroupGrant(
            document_id=document_id, group_id=group_id, workspace_id=workspace_id,
            allow_answer=True, allow_original=False,
        ))
        db.add(GroupMember(group_id=group_id, workspace_id=workspace_id, user_id=user_id))
        await db.commit()
    configure_execution(monkeypatch)

    async def local_completion(settings, question, evidence, *, history=None, prepared=None):
        assert evidence
        if change == "grant":
            response = await curator.put(
                f"/api/v1/knowledge/documents/{document_id}/group-grants/{group_id}",
                headers=curator_headers,
                json={"expected_revision": 1, "allow_answer": False, "allow_original": False},
            )
        elif change == "member":
            response = await admin.delete(
                f"/api/v1/admin/workspaces/{workspace_id}/groups/{group_id}/members/{user_id}",
                headers=admin_headers,
            )
        else:
            response = await curator.put(
                f"/api/v1/knowledge/versions/{version_id}/units/{unit_id}/group-denies/{group_id}",
                headers=curator_headers, json={"expected_revision": 1, "denied": True},
            )
        assert response.status_code == 200
        return {"answer": "Synthetic course costs 100.", "citations": [], "usage": {}}

    monkeypatch.setattr("partner_agent.runtime.runner.answer_with_provider", local_completion)
    try:
        chat = await new_chat(client, headers)
        admitted = (await submit(client, headers, chat, text="Synthetic course")).json()
        result = await execute_run(uuid.UUID(admitted["run_id"]), dispatch_generation=0)
        assert result["status"] == "failed"
        snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
        assert "source_context_changed" in str(snapshot)
        assert "Synthetic course costs 100." not in str(snapshot)
    finally:
        async with session_factory()() as db:
            await db.execute(
                delete(DisclosureGroupDeny).where(DisclosureGroupDeny.group_id == group_id)
            )
            await db.execute(
                delete(DocumentGroupGrant).where(DocumentGroupGrant.group_id == group_id)
            )
            await db.execute(delete(GroupMember).where(GroupMember.group_id == group_id))
            document = await db.get(Document, document_id)
            document.workspace_id = None
            await db.flush()
            await db.execute(delete(Group).where(Group.id == group_id))
            await db.execute(delete(Membership).where(Membership.workspace_id == workspace_id))
            await db.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await db.commit()


async def test_revocation_waits_until_final_publication_commits(corpus, monkeypatch):
    import partner_agent.runtime.lifecycle as lifecycle

    curator, curator_headers, client, headers, user_id, document_id, _, _ = corpus
    configure_execution(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original_finish = lifecycle.finish_run

    async def hold_finalization(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_finish(*args, **kwargs)

    async def local_completion(settings, question, evidence, *, history=None, prepared=None):
        return {"answer": "Synthetic course costs 100.", "citations": [], "usage": {}}

    monkeypatch.setattr(lifecycle, "finish_run", hold_finalization)
    monkeypatch.setattr("partner_agent.runtime.runner.answer_with_provider", local_completion)
    chat = await new_chat(client, headers)
    admitted = (await submit(client, headers, chat, text="Synthetic course")).json()
    execution = asyncio.create_task(execute_run(uuid.UUID(admitted["run_id"]),
                                               dispatch_generation=0))
    revocation = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        revocation = asyncio.create_task(curator.put(
            f"/api/v1/knowledge/documents/{document_id}/grants/{user_id}",
            headers=curator_headers,
            json={"expected_revision": 1, "allow_answer": False, "allow_original": False},
        ))
        await asyncio.sleep(0.05)
        assert not revocation.done()
        release.set()
        assert (await asyncio.wait_for(execution, 5))["status"] == "succeeded"
        assert (await asyncio.wait_for(revocation, 5)).status_code == 200
    finally:
        release.set()
        await asyncio.gather(execution, *([revocation] if revocation else []))
