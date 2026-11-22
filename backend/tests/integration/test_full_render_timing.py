"""Complete render observations never overwrite historical card-only timings."""

import uuid

import pytest
from test_chat_data_plane import complete, new_chat, submit
from test_chat_data_plane import plane as plane

pytestmark = pytest.mark.integration


async def test_card_and_full_result_are_separate_immutable_observations(plane):
    client, headers, _ = await plane()
    outsider, other_headers, _ = await plane("admin")
    chat = await new_chat(client, headers)
    run_id = (await submit(client, headers, chat)).json()["run_id"]
    await complete(run_id)
    base = f"/api/v1/chats/{chat['id']}/accounting"
    ledger = (await client.get(base)).json()
    floor = ledger["turns"][0]["server_elapsed_ms"]
    url = base + f"/turns/{run_id}/rendered"
    first = await client.post(url, headers=headers, json={"elapsed_ms": floor + 100})
    assert first.status_code == 200
    partial = (await client.get(base)).json()
    assert partial["mean_elapsed_ms"] == floor + 100
    assert partial["mean_full_elapsed_ms"] is None
    assert partial["turns"][0]["timing_scope"] == "answer_and_card"
    payload = {"elapsed_ms": floor + 300, "scope": "full_result"}
    assert (await outsider.post(url, headers=other_headers, json=payload)).status_code == 404
    assert (await client.post(url, json=payload)).status_code == 403
    assert (await client.post(url, headers=headers, json=payload)).status_code == 200
    repeated = await client.post(url, headers=headers,
                                 json={"elapsed_ms": 0, "scope": "full_result"})
    assert repeated.json()["elapsed_ms"] == floor + 300
    final = (await client.get(base)).json()
    assert final["mean_full_elapsed_ms"] == floor + 300
    assert final["full_timing_complete"] is True
    assert final["turns"][0]["card_elapsed_ms"] == floor + 100
    assert final["turns"][0]["timing_scope"] == "full_result"
    assert (await client.post(base + f"/turns/{uuid.uuid4()}/rendered", headers=headers,
                              json=payload)).status_code == 404


async def test_full_result_rejects_pending_turn_and_unknown_scope(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    run_id = (await submit(client, headers, chat)).json()["run_id"]
    path = f"/api/v1/chats/{chat['id']}/accounting/turns/{run_id}/rendered"
    assert (await client.post(path, headers=headers,
                             json={"elapsed_ms": 5, "scope": "full_result"})).status_code == 409
    assert (await client.post(path, headers=headers,
                             json={"elapsed_ms": 5, "scope": "fabricated"})).status_code == 422
