"""Real model execution path, with a substitute paid transport and real PostgreSQL."""

import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_design_revisions import design_input
from test_response_execution import configure_execution

from partner_agent.runtime.runner import execute_run

pytestmark = pytest.mark.integration


async def test_large_project_round_trip_and_local_overflow_do_not_spend_or_change_revision(
    plane, monkeypatch,
):
    from sqlalchemy import delete, select

    from partner_agent.infra.db import session_factory
    from partner_agent.infra.models import ModelUsageDaily
    from partner_agent.infra.settings import get_settings

    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    name = "large-project-" + uuid4().hex
    monkeypatch.setenv("PARTNER_MODEL_NAME", name)
    monkeypatch.setenv("PARTNER_MODEL_DAILY_CAP_USD", "1")
    get_settings.cache_clear()
    conditions = {
        **design_input(),
        "exchanges": [{
            "requirement_id": str(index).zfill(80), "sender_side": "A", "sender": "Д" * 120,
            "receiver": "М" * 120, "service": "С" * 120, "policy": "П" * 120,
            "mode": "diode", "protocol": "TCP", "destination_port": 1000 + index,
        } for index in range(24)],
    }
    calls = []

    async def model(settings, messages):
        calls.append(messages)
        result = reply(conditions)
        assert len(result["choices"][0]["message"]["content"]) > 12000
        return result

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    try:
        admitted = (await submit(client, headers, chat, text="Спроектируй 24 потока")).json()
        assert (await execute_run(UUID(admitted["run_id"]), dispatch_generation=0))[
            "status"
        ] == "succeeded"
        saved = (await client.get(base + "/latest", params={
            "branch_id": chat["default_branch_id"],
        })).json()["design"]
        assert len(saved["input"]["exchanges"]) == 24
        async with session_factory()() as db:
            before = await db.scalar(select(ModelUsageDaily).where(ModelUsageDaily.model == name))
            assert before is not None
            budget_before = (before.spent_usd_micros, before.request_count)
        overflow_response = await submit(client, headers, chat, text="X" * 32000)
        assert overflow_response.status_code == 202, overflow_response.text
        overflow = overflow_response.json()
        assert (await execute_run(UUID(overflow["run_id"]), dispatch_generation=0))[
            "status"
        ] == "succeeded"
        outcome = (await client.get(f"/api/v1/runs/{overflow['run_id']}")).json()["outcome"]
        assert outcome["response_status"] == "needs_clarification"
        assert outcome["task_completed"] is False
        assert outcome["code"] == "rag_context_limit" and len(calls) == 1
        async with session_factory()() as db:
            after = await db.scalar(select(ModelUsageDaily).where(ModelUsageDaily.model == name))
            assert (after.spent_usd_micros, after.request_count) == budget_before
        latest = (await client.get(base + "/latest", params={
            "branch_id": chat["default_branch_id"],
        })).json()["design"]
        assert latest["id"] == saved["id"]
        artifact = await client.post(base + f"/{saved['id']}/download/json", headers=headers)
        assert artifact.status_code == 200
        assert len(artifact.json()["rules"]) == 24
    finally:
        async with session_factory()() as db:
            await db.execute(delete(ModelUsageDaily).where(ModelUsageDaily.model == name))
            await db.commit()


def reply(conditions):
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "answer": "Untrusted model counts: 999.",
                            "citations": [],
                            "mode": "design",
                            "design": conditions,
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
    }


@pytest.mark.parametrize("response_kind", ["insufficient", "truncated"])
async def test_unapplied_chat_edit_keeps_the_saved_revision(plane, monkeypatch, response_kind):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    branch = chat["default_branch_id"]
    chat_url = f"/api/v1/chats/{chat['id']}"
    design_url = chat_url + "/designs"
    saved = await client.post(
        design_url, headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"branch_id": branch, "input": design_input()},
    )
    assert saved.status_code == 201
    first = saved.json()
    initial_file = await client.post(
        design_url + f"/{first['id']}/download/json", headers=headers,
    )

    async def model(settings, messages):
        serialized = messages[-1]["content"].split("Saved project conditions:\n")[1]
        assert json.loads(serialized)["exchanges"][0]["policy"] == "Только мониторинг"
        response = reply(None)
        message = {"answer": "Нужно уточнить режим.", "mode": "insufficient",
                   "citations": [], "design": None}
        response["choices"][0].update(
            message={"content": json.dumps(message)},
            finish_reason="length" if response_kind == "truncated" else "stop",
        )
        if response_kind == "truncated":
            response["usage"]["completion_tokens"] = 7000
            response["usage"]["total_tokens"] = 7200
        return response

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    admitted = await client.post(
        chat_url + "/runs", headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"branch_id": branch, "request_mode": "design", "message": "Измени правило."},
    )
    assert admitted.status_code == 202
    run_id = admitted.json()["run_id"]
    result = await execute_run(UUID(run_id), dispatch_generation=0)
    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    if response_kind == "insufficient":
        assert result["status"] == "succeeded"
        assert run["outcome"]["answer_mode"] == "insufficient"
        snapshot = (await client.get(chat_url + "/snapshot")).json()
        assistant, = [item for item in snapshot["messages"] if item["role"] == "assistant"]
        assert assistant["content"].startswith("Изменения по этому запросу не сохранены.")
    else:
        assert result["status"] == "succeeded"
        assert run["outcome"]["response_status"] == "needs_clarification"
        assert run["outcome"]["task_completed"] is False
        assert run["outcome"]["code"] == "provider_incomplete_response"
    assert "design_id" not in run["outcome"]
    latest = (await client.get(design_url + "/latest", params={"branch_id": branch})).json()
    assert latest["design"]["id"] == first["id"]
    archived = await client.post(design_url + f"/{first['id']}/download/json", headers=headers)
    assert archived.content == initial_file.content


async def test_chat_design_clarification_revision_sources_and_download(plane, monkeypatch):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    turns = []

    async def model(settings, messages):
        turns.append(messages)
        if len(turns) == 1:
            return reply({"network_a": "Производство"})
        if len(turns) == 2:
            assert "Saved project conditions:" in messages[-1]["content"]
            assert "Производство" in messages[-1]["content"]
            return reply(design_input())
        payload = json.loads(messages[-1]["content"].split("Saved project conditions:\n")[1])
        assert "sources" not in payload["exchanges"][0]
        payload["exchanges"][0]["policy"] = "Только аварии"
        return reply(payload)

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    first = (
        await submit(client, headers, chat, text="Контур A — Производство. Сделай проект.")
    ).json()
    assert (await execute_run(UUID(first["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    draft = (
        await client.get(base + "/latest", params={"branch_id": chat["default_branch_id"]})
    ).json()["design"]
    assert draft["revision"] == 1 and draft["questions"] and not draft["result"]
    second = (
        await submit(client, headers, chat, text="Офис, МЭ B, датчик → мониторинг, diode.")
    ).json()
    assert (await execute_run(UUID(second["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    completed = (
        await client.get(base + "/latest", params={"branch_id": chat["default_branch_id"]})
    ).json()["design"]
    assert completed["revision"] == 2 and completed["parent_id"] == draft["id"]
    assert completed["result"]["counts"]["logical_exchanges"] == 1
    source = completed["input"]["exchanges"][0]["sources"]
    assert source == [{"kind": "message", "reference": second["message_id"], "revision": 1}]
    outcome = (await client.get(f"/api/v1/runs/{second['run_id']}")).json()["outcome"]
    assert outcome["design_id"] == completed["id"] and outcome["answer_mode"] == "design"
    snapshot = (await client.get(f"/api/v1/chats/{chat['id']}/snapshot")).json()
    assistant = next(
        m
        for m in snapshot["messages"]
        if m["run_id"] == second["run_id"] and m["role"] == "assistant"
    )
    assert "999" not in assistant["content"] and assistant["origin"] == "openai_compatible"
    third = (await submit(client, headers, chat, text="Теперь только аварии.")).json()
    assert (await execute_run(UUID(third["run_id"]), dispatch_generation=0))[
        "status"
    ] == "succeeded"
    latest = (
        await client.get(base + "/latest", params={"branch_id": chat["default_branch_id"]})
    ).json()["design"]
    flow = latest["input"]["exchanges"][0]
    assert (
        latest["revision"] == 3
        and flow["requirement_id"] == completed["input"]["exchanges"][0]["requirement_id"]
    )
    assert [s["reference"] for s in flow["sources"]] == [second["message_id"], third["message_id"]]
    old_file = await client.post(f"{base}/{completed['id']}/download/json", headers=headers)
    assert old_file.json()["rules"][0]["policy"] == "Только мониторинг"
    assert (await client.get("/api/v1/auth/me")).json()["runtime_profile"] == "openai_compatible"


@pytest.mark.parametrize("change", ["cancel", "edit", "invalid_provenance", "invalid_shape"])
async def test_model_design_cannot_publish_after_cancel_edit_or_forged_source(
    plane, monkeypatch, change
):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    admitted = (await submit(client, headers, chat, text="Спроектируй Синоникс.")).json()

    async def model(settings, messages):
        conditions = deepcopy(design_input())
        if change == "cancel":
            response = await client.post(
                f"/api/v1/runs/{admitted['run_id']}/cancel", headers=headers
            )
            assert response.status_code == 200
        elif change == "edit":
            response = await client.post(
                base,
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json={"branch_id": chat["default_branch_id"], "input": {}},
            )
            assert response.status_code == 201
        elif change == "invalid_shape":
            conditions["firewall"] = "invented-side"
        else:
            conditions["exchanges"][0]["sources"] = [{"kind": "message", "reference": "foreign"}]
        return reply(conditions)

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    await execute_run(UUID(admitted["run_id"]), dispatch_generation=0)
    current = (
        await client.get(base + "/latest", params={"branch_id": chat["default_branch_id"]})
    ).json()["design"]
    if change == "edit":
        assert current["revision"] == 1 and current["input"]["exchanges"] == []
        run = (await client.get(f"/api/v1/runs/{admitted['run_id']}")).json()
        assert run["status"] == "failed" and run["outcome"]["code"] == "design_revision_conflict"
    else:
        assert current is None
