"""Saved geometry, ownership and archive immutability on real PostgreSQL."""

import hashlib
import uuid

import pytest
from test_chat_data_plane import new_chat
from test_chat_data_plane import plane as plane
from test_design_revisions import design_input

from partner_agent.infra.db import session_factory
from partner_agent.infra.models_designs import DesignRevision

pytestmark = pytest.mark.integration


async def test_scene_is_saved_once_with_xml_and_owner_only_across_revisions(plane):
    client, headers, _ = await plane()
    admin, _, _ = await plane("admin")
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    command = {"branch_id": chat["default_branch_id"], "input": design_input()}
    saved = await client.post(base, headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
                              json=command)
    assert saved.status_code == 201, saved.text
    first = saved.json()
    url = base + f"/{first['id']}/diagram"
    response = await client.get(url)
    assert response.status_code == 200
    original = response.json()
    assert original["revision_id"] == first["id"]
    assert original["sha256"] == first["artifact_sha256"]["diagram"]
    assert (await admin.get(url)).status_code == 404
    other = await new_chat(client, headers)
    assert (await client.get(f"/api/v1/chats/{other['id']}/designs/{first['id']}/diagram"
                             )).status_code == 404
    command.update(parent_id=first["id"])
    command["input"]["network_b"] = "Новая сеть"
    newer = await client.post(base, headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
                              json=command)
    assert newer.status_code == 201
    assert (await client.get(url)).json() == original
    file = await client.post(base + f"/{first['id']}/download/drawio", headers=headers)
    assert hashlib.sha256(file.content).hexdigest() == first["artifact_sha256"]["drawio"]


async def test_legacy_scene_is_not_regenerated_or_artifacts_mutated_by_get(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    saved = (await client.post(base, headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
                              json={"branch_id": chat["default_branch_id"],
                                    "input": design_input()})).json()
    identifier = uuid.UUID(saved["id"])
    async with session_factory()() as db:
        row = await db.get(DesignRevision, identifier)
        original = {key: value for key, value in row.artifacts.items() if key != "diagram"}
        row.artifacts = original
        await db.commit()
    response = await client.get(base + f"/{identifier}/diagram")
    assert response.status_code == 409 and response.json()["code"] == "diagram_not_available"
    async with session_factory()() as db:
        assert (await db.get(DesignRevision, identifier)).artifacts == original
    file = await client.post(base + f"/{identifier}/download/drawio", headers=headers)
    assert file.text == original["drawio"]
