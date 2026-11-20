import asyncio
import hashlib
from uuid import uuid4
from xml.etree.ElementTree import fromstring

import pytest
from test_chat_data_plane import new_chat
from test_chat_data_plane import plane as plane

pytestmark = pytest.mark.integration


def design_input():
    return {
        "network_a": "Производство", "network_b": "Офис", "firewall": "B",
        "exchanges": [{
            "sender_side": "A", "sender": "Датчик", "receiver": "Мониторинг",
            "mode": "diode", "service": "Телеметрия", "policy": "Только мониторинг",
        }],
    }


async def test_design_draft_completion_and_immutable_downloads(plane):
    client, headers, _ = await plane()
    outsider, other_headers, _ = await plane(role="admin")
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    branch = chat["default_branch_id"]
    first_key = str(uuid4())
    command_headers = {**headers, "Idempotency-Key": first_key}
    body = {"branch_id": branch, "input": {}}
    draft = await client.post(base, json=body, headers=command_headers)
    assert draft.status_code == 201
    first = draft.json()
    assert first["revision"] == 1 and first["status"] == "needs_clarification"
    assert first["questions"] and not first["artifact_sha256"]
    assert (await client.post(
        f"{base}/{first['id']}/download/drawio", headers=headers,
    )).status_code == 409
    replay = await client.post(base, json=body, headers=command_headers)
    assert replay.json()["id"] == first["id"]
    changed = await client.post(
        base, json={**body, "input": design_input()}, headers=command_headers,
    )
    assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
    body = {"branch_id": branch, "parent_id": first["id"], "input": design_input()}
    completed = await client.post(
        base, json=body, headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert completed.status_code == 201
    second = completed.json()
    assert second["revision"] == 2 and second["status"] == "conceptual"
    assert second["parent_id"] == first["id"] and second["result"]["logical_rule_count"] == 1
    url = f"{base}/{second['id']}/download/drawio"
    downloaded = await client.post(url, headers=headers)
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == second["artifact_sha256"]["drawio"]
    assert fromstring(downloaded.content).find("diagram") is not None
    latest = await client.get(f"{base}/latest", params={"branch_id": branch})
    assert latest.json()["design"]["id"] == second["id"]
    assert latest.headers["cache-control"] == "private, no-store"
    assert (await outsider.get(f"{base}/{second['id']}")).status_code == 404
    assert (await outsider.post(url, headers=other_headers)).status_code == 404
    assert (await client.post(url)).status_code == 403
    third_body = {
        **body, "parent_id": second["id"], "input": {**design_input(), "network_b": "DMZ"},
    }
    third = await client.post(
        base, json=third_body, headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert third.status_code == 201 and third.json()["revision"] == 3
    assert (await client.post(url, headers=headers)).content == downloaded.content
    assert (await client.get(f"{base}/{first['id']}")).json()["result"] is None
    deleted = await client.delete(f"/api/v1/chats/{chat['id']}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.post(url, headers=headers)).status_code == 404


async def test_concurrent_design_edits_serialize_and_unknown_retry_reuses_revision(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    body = {"branch_id": chat["default_branch_id"], "input": design_input()}
    key = str(uuid4())
    duplicate = await asyncio.gather(*(
        client.post(base, json=body, headers={**headers, "Idempotency-Key": key}) for _ in range(2)
    ))
    assert [reply.status_code for reply in duplicate] == [201, 201]
    first = duplicate[0].json()
    assert first["id"] == duplicate[1].json()["id"]
    edits = await asyncio.gather(*(
        client.post(base, json={**body, "parent_id": first["id"]},
                    headers={**headers, "Idempotency-Key": str(uuid4())}) for _ in range(2)
    ))
    assert sorted(reply.status_code for reply in edits) == [201, 409]
    latest = (await client.get(f"{base}/latest", params={"branch_id": body["branch_id"]})).json()
    assert latest["design"]["revision"] == 2


async def test_design_cannot_use_a_branch_of_another_chat(plane):
    client, headers, _ = await plane()
    first = await new_chat(client, headers)
    second = await new_chat(client, headers)
    response = await client.post(
        f"/api/v1/chats/{first['id']}/designs",
        json={"branch_id": second["default_branch_id"], "input": design_input()},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 404
