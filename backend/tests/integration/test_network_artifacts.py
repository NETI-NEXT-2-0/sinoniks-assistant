"""Saved F02 keeps calculation and both directions in scene/native XML without a model call."""

import json
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from xml.etree.ElementTree import fromstring

import pytest
from test_chat_data_plane import new_chat
from test_chat_data_plane import plane as plane

pytestmark = pytest.mark.integration


async def test_saved_f02_has_three_rules_and_two_way_native_artifact(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    conditions = json.loads((Path(__file__).parents[1] / "fixtures/network-f02.json").read_text(
        encoding="utf-8",
    ))
    command = {"branch_id": chat["default_branch_id"], "input": conditions}
    keyed = {**headers, "Idempotency-Key": str(uuid4())}
    response = await client.post(base, headers=keyed, json=command)
    assert response.status_code == 201
    revision = response.json()
    assert revision["result"]["confirmed_rules_count"] == 3
    assert revision["result"]["file_users_count"] == 0
    scene_url = base + f"/{revision['id']}/diagram"
    xml_url = base + f"/{revision['id']}/download/drawio"
    scene = (await client.get(scene_url)).json()["scene"]
    native = await client.post(xml_url, headers=headers)
    assert native.status_code == 200
    xml = fromstring(native.content)
    both = [edge for page in scene["pages"] for edge in page["edges"]
            if edge["start_arrow"] == "open"]
    assert len(both) == 2
    for edge in both:
        cell = xml.find(f".//mxCell[@id='{edge['id']}']")
        style = dict(part.split("=", 1) for part in cell.attrib["style"].split(";") if "=" in part)
        assert style["startArrow"] == "open" and style["endArrow"] == "block"
    assert (await client.post(base, headers=keyed, json=command)).json() == revision
    assert (await client.post(xml_url, headers=headers)).content == native.content
    assert (await client.get(scene_url)).json()["scene"] == scene


async def test_archived_f02_and_partial_revision_keep_matching_artifact_rules(plane):
    client, headers, _ = await plane()
    chat = await new_chat(client, headers)
    base = f"/api/v1/chats/{chat['id']}/designs"
    conditions = json.loads((Path(__file__).parents[1] / "fixtures/network-f02.json").read_bytes())
    command = {"branch_id": chat["default_branch_id"], "input": conditions}

    async def save():
        response = await client.post(base, json=command,
                                     headers={**headers, "Idempotency-Key": str(uuid4())})
        assert response.status_code == 201
        return response.json()

    async def artifacts(revision):
        path = base + f"/{revision['id']}"
        scene = (await client.get(path + "/diagram")).json()["scene"]
        native = await client.post(path + "/download/drawio", headers=headers)
        archived = await client.post(path + "/download/json", headers=headers)
        expert = await client.post(path + "/download/json?include_accounting=true", headers=headers)
        assert native.status_code == archived.status_code == expert.status_code == 200
        assert expert.json()["configuration_sha256"] == sha256(archived.content).hexdigest()
        assert expert.json()["revision_id"] == revision["id"]
        assert expert.json()["rules"] == revision["result"]["rules"]
        rows = scene["rule_rows"]
        assert json.loads(fromstring(native.content).attrib["data-rule-rows"]) == rows
        assert [(row["number"], row["id"]) for row in rows] == [
            (rule["id"], rule["rule_id"]) for rule in expert.json()["rules"]
        ]
        return expert.json(), archived.content, native.content

    first = await save()
    initial = await artifacts(first)
    assert initial[0]["status"] == "completed" and initial[0]["rules_count"] == 3
    command["parent_id"] = first["id"]
    logs = next(flow for flow in conditions["flows"] if flow["id"] == "logs")
    logs["mode"] = logs["destination_port"] = None
    partial = await save()
    latest = (await client.get(base + "/latest", params={"branch_id": command["branch_id"]})).json()
    assert latest["design"]["id"] == partial["id"]
    current = (await artifacts(partial))[0]
    assert current["status"] == "preliminary" and current["unknowns"]
    assert current["confirmed_rules_count"] is None
    assert current["session_id"] == initial[0]["session_id"] == chat["id"]
    assert await artifacts(first) == initial
