"""Saved F02 keeps calculation and both directions in scene/native XML without a model call."""

import json
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
