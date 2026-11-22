"""Immutable project bases follow branch frontiers, including manual revisions."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_design_revisions import design_input
from test_response_execution import configure_execution
from test_response_execution import corpus as corpus

from partner_agent.design_service import latest_revision
from partner_agent.infra.db import session_factory
from partner_agent.infra.errors import APIError
from partner_agent.infra.models_designs import RunDesignContext
from partner_agent.runtime.runner import execute_run
from partner_agent.sinoniks_contract import DesignInput

pytestmark = pytest.mark.integration


async def latest(client, chat, branch=None):
    response = await client.get(
        f"/api/v1/chats/{chat['id']}/designs/latest",
        params={"branch_id": branch or chat["default_branch_id"]},
    )
    assert response.status_code == 200
    return response.json()["design"]


async def save(client, headers, chat, parent=None, branch=None, policy="base"):
    conditions = design_input()
    conditions["exchanges"][0].update(requirement_id="req-1", policy=policy)
    response = await client.post(
        f"/api/v1/chats/{chat['id']}/designs",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={
            "branch_id": branch or chat["default_branch_id"],
            "parent_id": parent,
            "input": conditions,
        },
    )
    assert response.status_code == 201
    return response.json()


def provider(monkeypatch, seen):
    async def answer(
        settings, question, evidence, *, history=None, design_context=None, prepared=None,
    ):
        seen.append(deepcopy(design_context))
        conditions = deepcopy(design_context or design_input())
        conditions["exchanges"][0]["policy"] = question
        return {
            "answer": "Server builds the result",
            "citations": [],
            "usage": {},
            "answer_mode": "design",
            "design": DesignInput.model_validate(conditions),
        }

    monkeypatch.setattr("partner_agent.runtime.runner.answer_with_provider", answer)


async def execute(client, headers, chat, text, branch=None):
    accepted = (await submit(client, headers, chat, text=text, branch=branch)).json()
    assert (await execute_run(UUID(accepted["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    response = await client.get(
        f"/api/v1/chats/{chat['id']}/snapshot",
        params={
            "branch_id": branch or chat["default_branch_id"],
        },
    )
    message = next(
        m
        for m in response.json()["messages"]
        if m["run_id"] == accepted["run_id"] and m["role"] == "assistant"
    )
    return accepted, message


async def fork(client, headers, chat, message, branch=None, mode="regenerate", key=None):
    return await client.post(
        f"/api/v1/chats/{chat['id']}/branches",
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
        json={
            "parent_branch_id": branch or chat["default_branch_id"],
            "message_id": message,
            "mode": mode,
            **({"revised_text": "edited-policy"} if mode == "edit" else {}),
        },
    )


@pytest.mark.parametrize("mode", ["regenerate", "edit"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_fork_uses_original_manual_base_and_preserves_parent(
    plane, monkeypatch, mode, legacy
):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    seen = []
    provider(monkeypatch, seen)
    chat = await new_chat(client, headers)
    base = await save(client, headers, chat)
    original, message = await execute(client, headers, chat, "original-policy")
    result = await latest(client, chat)
    future = await save(client, headers, chat, result["id"], policy="future-parent")
    if legacy:
        async with session_factory()() as db:
            await db.execute(
                delete(RunDesignContext).where(
                    RunDesignContext.run_id == UUID(original["run_id"]),
                )
            )
            await db.commit()
    key = str(uuid4())
    target = message["id"] if mode == "regenerate" else original["message_id"]
    first = await fork(client, headers, chat, target, mode=mode, key=key)
    assert first.status_code == 201
    child = first.json()
    assert (await fork(client, headers, chat, target, mode=mode, key=key)).json() == child
    inherited = await latest(client, chat, child["branch_id"])
    assert inherited["id"] == base["id"]
    assert inherited["branch_id"] == chat["default_branch_id"]
    assert (await execute_run(UUID(child["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    assert seen[-1]["exchanges"][0]["policy"] == "base"
    own = await latest(client, chat, child["branch_id"])
    assert own["parent_id"] == base["id"] and own["branch_id"] == child["branch_id"]
    assert own["revision"] == 2
    assert (await latest(client, chat))["id"] == future["id"]
    outsider, other_headers, _ = await plane("admin")
    assert (
        await outsider.get(
            f"/api/v1/chats/{chat['id']}/designs/latest",
            params={
                "branch_id": child["branch_id"],
            },
        )
    ).status_code == 404
    assert (
        await outsider.post(
            f"/api/v1/chats/{chat['id']}/designs/{base['id']}/download/json",
            headers=other_headers,
        )
    ).status_code == 404


async def test_empty_base_and_nested_forks_do_not_import_later_projects(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    seen = []
    provider(monkeypatch, seen)
    chat = await new_chat(client, headers)
    _, original = await execute(client, headers, chat, "first-project")
    parent_result = await latest(client, chat)
    child = (await fork(client, headers, chat, original["id"])).json()
    assert await latest(client, chat, child["branch_id"]) is None
    await save(client, headers, chat, parent_result["id"], policy="later-parent")
    assert (await execute_run(UUID(child["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    assert seen[-1] is None
    child_result = await latest(client, chat, child["branch_id"])
    assert child_result["parent_id"] is None and child_result["revision"] == 1
    response = await client.get(
        f"/api/v1/chats/{chat['id']}/snapshot",
        params={
            "branch_id": child["branch_id"],
        },
    )
    child_message = next(
        m
        for m in response.json()["messages"]
        if m["run_id"] == child["run_id"] and m["role"] == "assistant"
    )
    nested = (await fork(client, headers, chat, child_message["id"], child["branch_id"])).json()
    assert await latest(client, chat, nested["branch_id"]) is None
    await save(client, headers, chat, child_result["id"], child["branch_id"], "later-child")
    assert (await execute_run(UUID(nested["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    assert seen[-1] is None


async def test_manual_child_edit_conflicts_with_queued_regeneration(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    provider(monkeypatch, [])
    chat = await new_chat(client, headers)
    base = await save(client, headers, chat)
    _, message = await execute(client, headers, chat, "original-policy")
    child = (await fork(client, headers, chat, message["id"])).json()
    edited = await save(client, headers, chat, base["id"], child["branch_id"], "manual-child")
    assert (await execute_run(UUID(child["run_id"]), dispatch_generation=0))["status"] == "failed"
    outcome = (await client.get(f"/api/v1/runs/{child['run_id']}")).json()["outcome"]
    assert outcome["code"] == "design_revision_conflict"
    assert (await latest(client, chat, child["branch_id"]))["id"] == edited["id"]


async def test_claim_pins_context_before_history_and_retrieval(plane, monkeypatch):
    from partner_agent.runtime.context import answer_history

    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    seen = []
    provider(monkeypatch, seen)
    chat = await new_chat(client, headers)
    base = await save(client, headers, chat)

    async def change_after_claim(db, actor, run_id):
        history = await answer_history(db, actor, run_id)
        await save(client, headers, chat, base["id"], policy="changed-after-claim")
        return history

    monkeypatch.setattr("partner_agent.runtime.runner.answer_history", change_after_claim)
    admitted = (await submit(client, headers, chat, text="model-policy")).json()
    assert (await execute_run(UUID(admitted["run_id"]), dispatch_generation=0))[
        "status"
    ] == "failed"
    assert seen[0]["exchanges"][0]["policy"] == "base"
    assert (await latest(client, chat))["input"]["exchanges"][0]["policy"] == "changed-after-claim"


async def test_legacy_fork_recovers_only_a_visible_completed_project(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    seen = []
    provider(monkeypatch, seen)
    chat = await new_chat(client, headers)
    await execute(client, headers, chat, "visible-project")
    visible = await latest(client, chat)

    async def ordinary(
        settings, question, evidence, *, history=None, design_context=None, prepared=None,
    ):
        return {
            "answer": "Ordinary legacy answer",
            "citations": [],
            "usage": {},
            "answer_mode": "conversation",
        }

    monkeypatch.setattr("partner_agent.runtime.runner.answer_with_provider", ordinary)
    original, message = await execute(client, headers, chat, "hidden-suffix")
    await save(client, headers, chat, visible["id"], policy="future-manual")
    async with session_factory()() as db:
        await db.execute(
            delete(RunDesignContext).where(
                RunDesignContext.run_id == UUID(original["run_id"]),
            )
        )
        await db.commit()
    child = (await fork(client, headers, chat, message["id"])).json()
    assert (await latest(client, chat, child["branch_id"]))["id"] == visible["id"]
    provider(monkeypatch, seen)
    assert (await execute_run(UUID(child["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    assert seen[-1]["exchanges"][0]["policy"] == "visible-project"


async def test_corrupt_cross_chat_context_fails_before_provider(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    seen = []
    provider(monkeypatch, seen)
    first, second = await new_chat(client, headers), await new_chat(client, headers)
    foreign = await save(client, headers, first)
    admitted = (await submit(client, headers, second, text="must-not-see-foreign")).json()
    async with session_factory()() as db:
        db.add(
            RunDesignContext(run_id=UUID(admitted["run_id"]), base_revision_id=UUID(foreign["id"]))
        )
        await db.commit()
    with pytest.raises(APIError) as error:
        await execute_run(UUID(admitted["run_id"]), dispatch_generation=0)
    assert error.value.code == "design_context_invalid" and not seen


@pytest.mark.parametrize("revocation", ["none", "before", "during"])
async def test_inherited_project_revalidates_derived_sources(corpus, monkeypatch, revocation):
    curator, curator_headers, client, headers, user_id, document_id, _, unit_id = corpus
    configure_execution(monkeypatch)
    calls = []

    async def revoke():
        response = await curator.put(
            f"/api/v1/knowledge/documents/{document_id}/grants/{user_id}",
            headers=curator_headers,
            json={"expected_revision": 1, "allow_answer": False, "allow_original": False},
        )
        assert response.status_code == 200

    async def model(
        settings, question, evidence, *, history=None, design_context=None, prepared=None,
    ):
        calls.append(design_context)
        if len(calls) == 1:
            assert any(item["evidence_id"] == str(unit_id) for item in evidence)
        if len(calls) == 3 and revocation == "during":
            await revoke()
        return {
            "answer": "Source-derived project",
            "citations": [],
            "usage": {},
            "answer_mode": "design",
            "design": DesignInput.model_validate(design_input()),
        }

    monkeypatch.setattr("partner_agent.runtime.runner.answer_with_provider", model)
    chat = await new_chat(client, headers)
    await execute(client, headers, chat, "Synthetic course")
    base = await latest(client, chat)
    _, target = await execute(client, headers, chat, "Update the project")
    child = (await fork(client, headers, chat, target["id"])).json()
    if revocation == "before":
        await revoke()
    result = await execute_run(UUID(child["run_id"]), dispatch_generation=0)
    assert result["status"] == ("succeeded" if revocation == "none" else "failed")
    assert len(calls) == (2 if revocation == "before" else 3)
    if revocation != "none":
        denied = await client.get(
            f"/api/v1/chats/{chat['id']}/designs/latest",
            params={"branch_id": child["branch_id"]},
        )
        assert denied.status_code == 409 and denied.json()["code"] == "source_context_changed"
        async with session_factory()() as db:
            inherited = await latest_revision(db, UUID(chat["id"]), UUID(child["branch_id"]))
            assert str(inherited.id) == base["id"]
        outcome = (await client.get(f"/api/v1/runs/{child['run_id']}")).json()["outcome"]
        assert outcome["code"] == "source_context_changed"
    else:
        assert await latest(client, chat, child["branch_id"])


async def test_short_reply_keeps_previous_uncited_clarification(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    seen = []

    async def clarify(settings, question, evidence, *, history=None, **kwargs):
        seen.append(history)
        if len(seen) == 1:
            return {"answer": "В какую сеть добавить компьютер 3?", "citations": [],
                    "usage": {}, "answer_mode": "insufficient"}
        assert history[-2:] == [
            {"role": "user", "content": "Добавь компьютер 3 в контур справа"},
            {"role": "assistant", "content": "В какую сеть добавить компьютер 3?"},
        ]
        return {"answer": "Уточнение получено", "citations": [], "usage": {},
                "answer_mode": "conversation"}

    monkeypatch.setattr("partner_agent.runtime.runner.answer_with_provider", clarify)
    chat = await new_chat(client, headers)
    await execute(client, headers, chat, "Добавь компьютер 3 в контур справа")
    await execute(client, headers, chat, "Сеть цеха")
    assert len(seen) == 2
