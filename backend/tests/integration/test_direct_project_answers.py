"""Queued field answers are real user turns with zero model attempts."""

from uuid import UUID, uuid4

import pytest
from test_chat_data_plane import new_chat
from test_chat_data_plane import plane as plane
from test_design_branches import fork
from test_requirements_revisions import data_project, save
from test_response_execution import configure_execution
from test_response_execution import corpus as corpus

from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


async def send_answer(client, headers, chat, row, *, key=None, **changes):
    answer = {
        "base_revision_id": row["id"],
        "path": "/flows/0/protocol",
        "target_id": row["input"]["flows"][0]["id"],
        "value": "UDP",
        **changes,
    }
    return await client.post(
        f"/api/v1/chats/{chat['id']}/runs",
        headers={
            **headers,
            "Idempotency-Key": key or str(uuid4()),
        },
        json={
            "branch_id": chat["default_branch_id"],
            "message": "Уточнение: UDP",
            "request_mode": "design",
            "project_answer": answer,
        },
    )


def no_model(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("A direct answer must not call a provider")

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", forbidden)


async def test_direct_answer_is_idempotent_auditable_and_zero_provider_calls(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    no_model(monkeypatch)
    chat = await new_chat(client, headers)
    branch, base = chat["default_branch_id"], f"/api/v1/chats/{chat['id']}/designs"
    document = data_project()
    document["flows"][0]["protocol"] = None
    first = (await save(client, headers, base, branch, document)).json()
    key = str(uuid4())
    admitted = await send_answer(client, headers, chat, first, key=key)
    assert admitted.status_code == 202, admitted.text
    assert (await send_answer(client, headers, chat, first, key=key)).json() == admitted.json()
    result = await execute_run(UUID(admitted.json()["run_id"]), dispatch_generation=0)
    assert result["status"] == "succeeded"
    current = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert current["revision"] == 2 and current["input"]["flows"][0]["protocol"] == "UDP"
    assert current["input"]["flows"][0]["sources"][-1]["reference"] == admitted.json()["message_id"]
    run = (await client.get(f"/api/v1/runs/{admitted.json()['run_id']}")).json()
    assert run["outcome"]["provider_calls"] == 0
    assert run["outcome"]["execution"] == "local_project_answer"
    ledger = (await client.get(f"/api/v1/chats/{chat['id']}/accounting")).json()
    assert ledger["summary"]["calls_count"] == 0 and not ledger["calls"]
    assert len(ledger["turns"]) == 1
    # Replaying the completed command returns the same run, not another revision.
    assert (await send_answer(client, headers, chat, first, key=key)).json() == admitted.json()


async def test_batch_applies_typed_answers_and_preserves_other_text(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    no_model(monkeypatch)
    chat = await new_chat(client, headers)
    chat_url = f"/api/v1/chats/{chat['id']}"
    branch = chat["default_branch_id"]
    design_url = chat_url + "/designs"
    document = data_project()
    document["flows"][0].update(protocol=None, destination_port=None, mode=None)
    first = (await save(client, headers, design_url, branch, document)).json()
    target = first["input"]["flows"][0]["id"]
    fields = [
        {"path": "/flows/0/protocol", "target_id": target, "value": "UDP"},
        {"path": "/flows/0/destination_port", "target_id": target, "unknown": True},
    ]
    remark = {"path": "/flows/0/mode", "target_id": target,
              "text": " Уточню режим.\nТребования к нему ещё согласуются. "}
    command_headers = {**headers, "Idempotency-Key": str(uuid4())}
    payload = {
        "branch_id": branch,
        "message": "Применить уточнения проекта",
        "request_mode": "design",
        "project_answer": {
            "base_revision_id": first["id"], "answers": fields, "remarks": [remark],
        },
    }
    fields[1]["value"] = 70000
    fields[1]["unknown"] = False
    invalid = await client.post(chat_url + "/runs", headers=command_headers, json=payload)
    assert invalid.status_code == 422 and invalid.json()["code"] == "invalid_project_answer"
    latest = await client.get(design_url + "/latest", params={"branch_id": branch})
    assert latest.json()["design"]["id"] == first["id"]
    assert (await client.get(chat_url + "/vendor-wishes/export")).json()["wishes"] == []

    del fields[1]["value"]
    fields[1]["unknown"] = True
    remark["target_id"] = "invalid-target"
    rejected = await client.post(chat_url + "/runs", headers=command_headers, json=payload)
    assert rejected.status_code == 409 and rejected.json()["code"] == "question_context_changed"
    remark["target_id"] = target
    admitted = await client.post(chat_url + "/runs", headers=command_headers, json=payload)
    assert admitted.status_code == 202, admitted.text
    receipt, = admitted.json()["project_answer_remarks"]
    assert receipt["text"] == remark["text"] and receipt["base_revision_id"] == first["id"]
    run_id = admitted.json()["run_id"]
    assert (await execute_run(UUID(run_id), dispatch_generation=0))["status"] == "succeeded"
    replay = await client.post(chat_url + "/runs", headers=command_headers, json=payload)
    assert replay.json() == admitted.json()
    latest = await client.get(design_url + "/latest", params={"branch_id": branch})
    current = latest.json()["design"]
    assert current["revision"] == 2 and current["parent_id"] == first["id"]
    assert current["input"]["flows"][0]["protocol"] == "UDP"
    assert current["input"]["flows"][0]["destination_port"] is None
    assert current["input"]["flows"][0]["mode"] is None
    assert [note["path"] for note in current["input"]["unknowns"]] == ["/flows/0/destination_port"]
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert run["outcome"]["provider_calls"] == 0
    assert len(run["outcome"]["project_answer"]["answers"]) == 2
    exported, = (await client.get(chat_url + "/vendor-wishes/export")).json()["wishes"]
    assert exported["origin"] == "user_submission"
    assert exported["path"] == remark["path"] and exported["text"] == remark["text"]


async def test_questions_are_exact_owner_branch_scoped_and_stale_selection_rejected(plane):
    client, headers, _ = await plane()
    admin, _, _ = await plane("admin")
    chat = await new_chat(client, headers)
    branch, base = chat["default_branch_id"], f"/api/v1/chats/{chat['id']}/designs"
    document = data_project()
    document["flows"][0]["protocol"] = None
    first = (await save(client, headers, base, branch, document)).json()
    url = base + f"/{first['id']}/questions"
    questions = await client.get(url, params={"branch_id": branch})
    assert questions.status_code == 200 and questions.json()["current"]
    protocol = next(
        item for item in questions.json()["questions"] if item["field"] == "/flows/0/protocol"
    )
    assert protocol["target_id"] == first["input"]["flows"][0]["id"]
    assert (await admin.get(url, params={"branch_id": branch})).status_code == 404
    other = await new_chat(client, headers)
    assert (
        await client.get(url, params={"branch_id": other["default_branch_id"]})
    ).status_code == 404
    assert (await send_answer(client, {}, chat, first)).status_code == 403
    wrong = await send_answer(client, headers, chat, first, target_id="wrong-entity")
    assert wrong.status_code == 409 and wrong.json()["code"] == "question_context_changed"
    changed = (await save(client, headers, base, branch, document, parent=first["id"])).json()
    assert not (await client.get(url, params={"branch_id": branch})).json()["current"]
    stale = await send_answer(client, headers, chat, first)
    assert stale.status_code == 409 and stale.json()["code"] == "design_revision_conflict"
    assert changed["revision"] == 2


@pytest.mark.parametrize("change", ["edit", "cancel"])
async def test_queued_direct_answer_cannot_overwrite_or_publish_after_cancel(
    plane, monkeypatch, change
):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    no_model(monkeypatch)
    chat = await new_chat(client, headers)
    branch, base = chat["default_branch_id"], f"/api/v1/chats/{chat['id']}/designs"
    first = (await save(client, headers, base, branch, data_project())).json()
    run = (await send_answer(client, headers, chat, first)).json()
    if change == "edit":
        newer = (
            await save(client, headers, base, branch, data_project(), parent=first["id"])
        ).json()
    else:
        canceled = await client.post(f"/api/v1/runs/{run['run_id']}/cancel", headers=headers)
        assert canceled.status_code == 200
    result = await execute_run(UUID(run["run_id"]), dispatch_generation=0)
    assert result["status"] == ("failed" if change == "edit" else "duplicate_or_ineligible")
    current = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert current["id"] == (newer["id"] if change == "edit" else first["id"])


async def test_regenerate_reapplies_original_structured_command_without_a_provider(
    plane, monkeypatch
):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    no_model(monkeypatch)
    chat = await new_chat(client, headers)
    branch, base = chat["default_branch_id"], f"/api/v1/chats/{chat['id']}/designs"
    document = data_project()
    document["flows"][0]["protocol"] = None
    first = (await save(client, headers, base, branch, document)).json()
    run = (await send_answer(client, headers, chat, first, value=None, unknown=True)).json()
    assert (await execute_run(UUID(run["run_id"]), dispatch_generation=0))["status"] == "succeeded"
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    message = next(item for item in snapshot["messages"] if item["role"] == "assistant")
    child = (await fork(client, headers, chat, message["id"])).json()
    assert (await execute_run(UUID(child["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    current = (await client.get(base + "/latest", params={"branch_id": child["branch_id"]})).json()[
        "design"
    ]
    assert current["parent_id"] == first["id"]
    assert current["input"]["flows"][0]["protocol"] is None
    assert current["input"]["unknowns"][0]["reason"] == "user_unknown"


async def test_direct_answer_rechecks_revoked_sources_without_a_provider(corpus, monkeypatch):
    from partner_agent.sinoniks_contract19 import Requirements19

    curator, curator_headers, client, headers, user_id, document_id, _, unit_id = corpus
    configure_execution(monkeypatch)
    calls = []

    async def model(settings, question, evidence, **kwargs):
        calls.append(question)
        assert any(item["evidence_id"] == str(unit_id) for item in evidence)
        return {
            "answer": "Derived",
            "citations": [],
            "usage": {},
            "answer_mode": "design",
            "design": Requirements19.model_validate(data_project()),
        }

    monkeypatch.setattr("partner_agent.runtime.runner.answer_with_provider", model)
    chat = await new_chat(client, headers)
    from test_chat_data_plane import submit

    admitted = (await submit(client, headers, chat, text="Synthetic course")).json()
    assert (await execute_run(UUID(admitted["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    base, branch = f"/api/v1/chats/{chat['id']}/designs", chat["default_branch_id"]
    first = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    direct = (await send_answer(client, headers, chat, first)).json()
    revoked = await curator.put(
        f"/api/v1/knowledge/documents/{document_id}/grants/{user_id}",
        headers=curator_headers,
        json={"expected_revision": 1, "allow_answer": False, "allow_original": False},
    )
    assert revoked.status_code == 200
    assert (await execute_run(UUID(direct["run_id"]), dispatch_generation=0))["status"] == "failed"
    inaccessible = await client.get(base + "/latest", params={"branch_id": branch})
    assert inaccessible.status_code == 409
    assert inaccessible.json()["code"] == "source_context_changed"
    from sqlalchemy import select

    from partner_agent.infra.db import session_factory
    from partner_agent.infra.models_designs import DesignRevision

    async with session_factory()() as db:
        stored = list((await db.scalars(select(DesignRevision).where(
            DesignRevision.chat_id == UUID(chat["id"]),
        ))).all())
        assert len(stored) == 1 and str(stored[0].id) == first["id"]
    assert len(calls) == 1
    outcome = (await client.get(f"/api/v1/runs/{direct['run_id']}")).json()["outcome"]
    assert outcome["code"] == "source_context_changed" and outcome["provider_calls"] == 0


async def test_cancel_after_claim_keeps_zero_calls_and_cannot_publish(plane, monkeypatch):
    from partner_agent.runtime import project_answer as local

    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    no_model(monkeypatch)
    chat = await new_chat(client, headers)
    branch, base = chat["default_branch_id"], f"/api/v1/chats/{chat['id']}/designs"
    first = (await save(client, headers, base, branch, data_project())).json()
    run = (await send_answer(client, headers, chat, first)).json()
    original = local.project_evidence

    async def cancel_before_finish(db, actor, chat_id, project):
        cancelled = await client.post(f"/api/v1/runs/{run['run_id']}/cancel", headers=headers)
        assert cancelled.status_code == 200
        return await original(db, actor, chat_id, project)

    monkeypatch.setattr(local, "project_evidence", cancel_before_finish)
    assert (await execute_run(UUID(run["run_id"]), dispatch_generation=0))[
        "status"
    ] == "ownership_lost"
    current = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert current["id"] == first["id"]
    ledger = (await client.get(f"/api/v1/chats/{chat['id']}/accounting")).json()
    assert ledger["summary"]["calls_count"] == 0
    assert ledger["legacy_untracked_turns"] == 0
