"""Durable intent and a server-enforced boundary between lookup and project writes."""

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_chat_data_plane import new_chat
from test_chat_data_plane import plane as plane
from test_design_revisions import design_input
from test_response_execution import configure_execution
from test_response_execution import corpus as corpus

from partner_agent.infra.db import session_factory
from partner_agent.infra.models_designs import DesignRevision
from partner_agent.infra.models_stage2 import Admission
from partner_agent.runs.service import body_hash
from partner_agent.runtime.runner import execute_run
from partner_agent.sinoniks_contract import DesignInput

pytestmark = pytest.mark.integration


async def send(client, headers, chat, mode="auto", key=None, message="Describe networks"):
    return await client.post(
        f"/api/v1/chats/{chat['id']}/runs",
        headers={
            **headers,
            "Idempotency-Key": key or str(uuid4()),
        },
        json={
            "branch_id": chat["default_branch_id"],
            "message": message,
            "attachment_ids": [],
            "web_mode": "off",
            "request_mode": mode,
        },
    )


def response(mode="insufficient", design=None):
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "answer": "Synthetic answer",
                            "citations": [],
                            "mode": mode,
                            "design": design,
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


@pytest.mark.parametrize("mode", ["auto", "design", "reference"])
async def test_request_mode_is_durable_idempotent_and_inherited(plane, monkeypatch, mode):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)

    async def model(settings, messages):
        return response()

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    chat = await new_chat(client, headers)
    key = str(uuid4())
    first = await send(client, headers, chat, mode, key)
    assert first.status_code == 202 and first.json()["request_mode"] == mode
    assert (await send(client, headers, chat, mode, key)).json() == first.json()
    conflict = await send(
        client, headers, chat, "reference" if mode != "reference" else "design", key
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    if mode == "auto":
        async with session_factory()() as db:
            admission = await db.scalar(select(Admission).where(Admission.key == key))
            assert admission.body_hash == body_hash(
                {
                    "branch_id": chat["default_branch_id"],
                    "message": "Describe networks",
                    "attachment_ids": [],
                    "web_mode": "off",
                }
            )
        omitted = await client.post(
            f"/api/v1/chats/{chat['id']}/runs",
            headers={
                **headers,
                "Idempotency-Key": key,
            },
            json={"branch_id": chat["default_branch_id"], "message": "Describe networks"},
        )
        assert omitted.json() == first.json()
    run_id = first.json()["run_id"]
    assert (await execute_run(UUID(run_id), dispatch_generation=0))["status"] == "succeeded"
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert run["request_mode"] == mode
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    answer = next(
        m for m in snapshot["messages"] if m["run_id"] == run_id and m["role"] == "assistant"
    )
    for kind, target in (("edit", first.json()["message_id"]), ("regenerate", answer["id"])):
        child = await client.post(
            f"/api/v1/chats/{chat['id']}/branches",
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
            },
            json={
                "parent_branch_id": chat["default_branch_id"],
                "message_id": target,
                "mode": kind,
                **({"revised_text": "Edited"} if kind == "edit" else {}),
            },
        )
        assert child.status_code == 201 and child.json()["request_mode"] == mode


@pytest.mark.parametrize("mode", ["unknown", None, 42])
async def test_invalid_request_mode_does_not_admit_a_run(plane, mode):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    assert (await send(client, headers, chat, mode)).status_code == 422
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    assert snapshot["runs"] == [] and snapshot["messages"] == []


async def test_queued_requests_keep_their_independent_purposes(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    calls = []

    async def model(settings, messages):
        calls.append(messages)
        if len(calls) == 1:
            assert "PROJECT mode" in messages[0]["content"]
            return response("design", design_input())
        assert "REFERENCE mode" in messages[0]["content"]
        assert "Saved project conditions:" not in str(messages)
        return response("conversation")

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    chat = await new_chat(client, headers)
    first = (await send(client, headers, chat, "design")).json()
    second = (await send(client, headers, chat, "reference")).json()
    assert (await execute_run(UUID(first["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    before = (
        await client.get(
            f"/api/v1/chats/{chat['id']}/designs/latest",
            params={
                "branch_id": chat["default_branch_id"],
            },
        )
    ).json()["design"]
    assert (await execute_run(UUID(second["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    after = (
        await client.get(
            f"/api/v1/chats/{chat['id']}/designs/latest",
            params={
                "branch_id": chat["default_branch_id"],
            },
        )
    ).json()["design"]
    assert len(calls) == 2 and before["id"] == after["id"]


async def test_explain_shared_receiver_without_a_revision_or_reference_history_leak(
    plane, monkeypatch,
):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    branch = chat["default_branch_id"]
    conditions = design_input()
    conditions["exchanges"] = [
        dict(conditions["exchanges"][0], sender=f"ПК-{number}", receiver="Журналы цеха",
             protocol="UDP", destination_port=514)
        for number in range(1, 4)
    ]
    saved = await client.post(
        base, headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"branch_id": branch, "input": conditions},
    )
    assert saved.status_code == 201
    before = saved.json()

    async def model(settings, messages):
        if "REFERENCE mode" in messages[0]["content"]:
            assert "Журналы цеха" not in str(messages)
            assert "Почему правило одно" not in str(messages)
            return response("conversation")
        question = messages[-1]["content"]
        summary = json.loads(question.split("Saved calculation snapshot:\n", 1)[1].splitlines()[0])
        assert summary["counts"]["logical_exchanges"] == 1
        assert summary["rule_membership_in_display_order"] == [
            ["requirement-1", "requirement-2", "requirement-3"],
        ]
        assert "Журналы цеха" in question and '"destination_port": 514' in question
        reply = response("project_explanation")
        reply["choices"][0]["message"]["content"] = json.dumps({
            "answer": "ПК используют общий получатель «Журналы цеха», UDP 514.",
            "citations": [], "mode": "project_explanation", "design": None,
        })
        return reply

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    for mode in ("auto", "reference"):
        message = "Почему правило одно?" if mode == "auto" else "Что такое UDP?"
        admitted = (await send(client, headers, chat, mode, message=message)).json()
        assert (await execute_run(UUID(admitted["run_id"]), dispatch_generation=0))[
            "status"
        ] == "succeeded"
        run = (await client.get(f"/api/v1/runs/{admitted['run_id']}")).json()
        if mode == "auto":
            assert run["outcome"]["answer_mode"] == "project_explanation"
            assert "design_id" not in run["outcome"]
            view = (await client.get(
                f"/api/v1/chats/{chat['id']}/snapshot", params={"branch_id": branch},
            )).json()
            assert next(
                item["content"] for item in view["messages"]
                if item["run_id"] == admitted["run_id"] and item["role"] == "assistant"
            ).startswith("По сохранённой ревизии №1.\n\nПК используют")
    after = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert after["id"] == before["id"]
    assert after["artifact_sha256"] == before["artifact_sha256"]


async def test_vendor_wishes_export_preserves_user_words_and_is_private(plane, monkeypatch):
    client, headers, _ = await plane()
    outsider, _, _ = await plane("admin")
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    other_chat = await new_chat(client, headers)
    original = "нужен встроенный антивирус"
    other = "требуется собственный формат отчёта"

    async def model(settings, messages):
        current = messages[-1]["content"]
        reply = response()
        supported = design_input() if "Передавайте обновления" in current else None
        reply["choices"][0]["message"]["content"] = json.dumps({
            "answer": "Проверка ответа", "citations": [], "design": supported,
            "mode": "design" if supported else "insufficient",
            "vendor_wishes": [
                {"kind": "unsupported", "text": original},
                {"kind": "free_text", "text": other},
                {"kind": "free_text", "text": "Фраза из другого источника"},
            ],
        })
        return reply

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    turns = []
    for mode, message in (
        ("design", f"Передавайте обновления, {original}."),
        ("auto", f"Ещё раз: {original}."),
        ("auto", f"Другое: {other}."),
        ("reference", f"Справочный ход: {other}."),
    ):
        admitted = (await send(client, headers, chat, mode, message=message)).json()
        turns.append(admitted)
        assert (await execute_run(UUID(admitted["run_id"]), dispatch_generation=0))[
            "status"
        ] == "succeeded"
    base = f"/api/v1/chats/{chat['id']}"
    revision = (await client.get(
        base + "/designs/latest", params={"branch_id": chat["default_branch_id"]},
    )).json()["design"]
    assert revision["revision"] == 1 and revision["result"]["logical_rule_count"] == 1
    assert original not in json.dumps(revision["input"], ensure_ascii=False)
    reference = (await client.get(f"/api/v1/runs/{turns[3]['run_id']}")).json()
    assert reference["outcome"]["vendor_wishes"] == [{"kind": "free_text", "text": other}]

    export = base + "/vendor-wishes/export"
    exported = await client.get(export)
    assert exported.status_code == 200 and exported.headers["Cache-Control"] == "private, no-store"
    assert exported.json()["session_id"] == chat["id"]
    wishes = exported.json()["wishes"]
    assert [(item["kind"], item["text"], item["message_id"]) for item in wishes] == [
        ("unsupported", original, turns[0]["message_id"]),
        ("free_text", other, turns[2]["message_id"]),
    ]
    assert (await outsider.get(export)).status_code == 404
    assert (await client.get(
        f"/api/v1/chats/{other_chat['id']}/vendor-wishes/export",
    )).json()["wishes"] == []


@pytest.mark.parametrize("guard", ["provider", "completion"])
async def test_reference_mode_cannot_save_an_adapter_proposal(plane, monkeypatch, guard):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    original = (
        await client.post(
            base,
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"branch_id": chat["default_branch_id"], "input": design_input()},
        )
    ).json()
    if guard == "provider":

        async def model(settings, messages):
            assert "Saved project conditions:" not in messages[-1]["content"]
            return response("design", design_input())

        monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    else:

        async def adapter(
            settings, question, evidence, *, history=None, request_mode="auto", **kwargs
        ):
            assert request_mode == "reference" and "design_context" not in kwargs
            return {
                "answer": "Forbidden write",
                "citations": [],
                "usage": {},
                "answer_mode": "design",
                "design": DesignInput.model_validate(design_input()),
            }

        monkeypatch.setattr("partner_agent.runtime.runner.answer_with_provider", adapter)
    admitted = (await send(client, headers, chat, "reference")).json()
    assert (await execute_run(UUID(admitted["run_id"]), dispatch_generation=0))[
        "status"
    ] == "failed"
    run = (await client.get(f"/api/v1/runs/{admitted['run_id']}")).json()
    assert run["outcome"]["code"] == (
        "provider_request_mode_violation" if guard == "provider" else "request_mode_violation"
    )
    assert (
        await client.get(base + "/latest", params={"branch_id": chat["default_branch_id"]})
    ).json()["design"]["id"] == original["id"]


async def test_reference_question_excludes_project_even_after_its_source_is_revoked(
    corpus, monkeypatch
):
    curator, curator_headers, client, headers, user_id, document_id, _, _ = corpus
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    calls = []

    async def model(settings, messages):
        calls.append(messages)
        if len(calls) == 1:
            proposal = design_input()
            proposal["network_a"] = "SECRET_TOPOLOGY"
            return response("design", proposal)
        assert len(messages) == 2
        assert "SECRET_TOPOLOGY" not in str(messages) and "Saved project conditions:" not in str(
            messages
        )
        assert "REFERENCE mode" in messages[0]["content"]
        return response("conversation")

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    first = (await send(client, headers, chat, "design", message="Synthetic course")).json()
    assert (await execute_run(UUID(first["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    base = f"/api/v1/chats/{chat['id']}/designs/latest"
    original = (await client.get(base, params={"branch_id": chat["default_branch_id"]})).json()[
        "design"
    ]
    revoked = await curator.put(
        f"/api/v1/knowledge/documents/{document_id}/grants/{user_id}",
        headers=curator_headers,
        json={"expected_revision": 1, "allow_answer": False, "allow_original": False},
    )
    assert revoked.status_code == 200
    second = (await send(client, headers, chat, "reference", message="Привет")).json()
    assert (await execute_run(UUID(second["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    assert len(calls) == 2
    denied = await client.get(base, params={"branch_id": chat["default_branch_id"]})
    assert denied.status_code == 409 and denied.json()["code"] == "source_context_changed"
    async with session_factory()() as db:
        revisions = list((await db.scalars(select(DesignRevision).where(
            DesignRevision.chat_id == UUID(chat["id"]),
        ))).all())
        assert len(revisions) == 1 and str(revisions[0].id) == original["id"]
