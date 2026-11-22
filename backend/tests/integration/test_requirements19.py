"""Requirements 1.9 validation has no saved-revision or provider side effects."""

from uuid import uuid4

import pytest
from test_chat_data_plane import new_chat
from test_chat_data_plane import plane as plane

pytestmark = pytest.mark.integration


def conditions():
    return {"schema_version": 3, "id": "project-1", "networks": [], "flows": []}


async def test_requirements_validation_is_owner_branch_csrf_scoped_and_does_not_save(plane):
    client, headers, _ = await plane()
    admin, admin_headers, _ = await plane("admin")
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    body = {"branch_id": chat["default_branch_id"], "input": conditions()}
    original = (await client.get(base + "/latest", params={
        "branch_id": chat["default_branch_id"],
    })).json()
    response = await client.post(base + "/requirements/validate", headers=headers, json=body)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["saved"] is False and value["assessment"]["rules_count"] is None
    assert value["assessment"]["status"] == "needs_clarification"
    assert value["input"]["networks"] == [] and not value["input"]["assumptions"]
    assert response.headers["Cache-Control"] == "private, no-store"
    assert (await client.get(base + "/latest", params={
        "branch_id": chat["default_branch_id"],
    })).json() == original
    assert (await admin.post(base + "/requirements/validate", headers=admin_headers,
                             json=body)).status_code == 404
    assert (await client.post(base + "/requirements/validate", json=body)).status_code == 403
    other = await new_chat(client, headers)
    body["branch_id"] = other["default_branch_id"]
    assert (await client.post(base + "/requirements/validate", headers=headers,
                             json=body)).status_code == 404


async def test_invalid_contract_is_not_reinterpreted_as_legacy_or_saved(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    body = {"branch_id": chat["default_branch_id"],
            "input": conditions() | {"exchanges": [], "schema_version": 3.0}}
    response = await client.post(base + "/requirements/validate", headers=headers, json=body)
    assert response.status_code == 422
    # Requirements now persist as drafts, never as a solved conceptual design.
    response = await client.post(base, headers={**headers,
        "Idempotency-Key": str(uuid4())},
        json={"branch_id": chat["default_branch_id"], "input": conditions()})
    assert response.status_code == 201
    assert response.json()["result"]["status"] == "needs_clarification"
    assert response.json()["status"] == "needs_clarification"
