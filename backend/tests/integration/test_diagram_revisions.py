"""Saved geometry, ownership and archive immutability on real PostgreSQL."""

import hashlib
import json
import uuid
from copy import deepcopy
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest
from test_chat_data_plane import new_chat, submit
from test_chat_data_plane import plane as plane
from test_design_execution import reply
from test_design_revisions import design_input
from test_response_execution import configure_execution

from partner_agent.infra.db import session_factory
from partner_agent.infra.models_designs import DesignRevision
from partner_agent.infra.settings import get_settings
from partner_agent.runtime.runner import execute_run

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


async def test_mixed_project_keeps_all_flow_links_through_extraction_and_exports(
    plane, monkeypatch,
):
    client, headers, _ = await plane()
    configure_execution(monkeypatch)
    monkeypatch.setenv("PARTNER_DESIGN_INPUT_VERSION", "3")
    get_settings.cache_clear()
    fixture = Path(__file__).parents[1] / "fixtures/network-f07.json"
    conditions = json.loads(fixture.read_text(encoding="utf-8"))
    conditions["schema_version"] = 3
    generic, _, file_flow = conditions["flows"]
    for index in range(6):
        conditions["flows"].append({
            **generic, "id": f"logs-{index}", "protocol": "UDP", "mode": "one-way",
            "purpose": f"Журнал {index}", "destination_port": 1514 + index,
        })
    file_flow["sides"][1]["checks"] = None
    for index in range(5):
        item = deepcopy(file_flow)
        item.update(id=f"file-{index}", purpose=f"Документы {index}", sender_ids=[])
        item["sides"][0].update(usb=False, manual=True, manual_protocol="SFTP")
        item["sides"][1].update(
            checks=[], received_files_shared=False, access_group=f"group-{index}",
        )
        conditions["flows"].append(item)
    calls = []

    async def model(settings, messages):
        calls.append(messages)
        return reply(conditions)

    monkeypatch.setattr("partner_agent.runtime.rag_provider.chat_completion", model)
    chat = await new_chat(client, headers)
    branch = chat["default_branch_id"]
    chat_url = f"/api/v1/chats/{chat['id']}"
    base = chat_url + "/designs"
    admitted = await submit(client, headers, chat, text="Рассчитай обмен по двум сетям.")
    assert admitted.status_code == 202
    run_id = uuid.UUID(admitted.json()["run_id"])
    assert (await execute_run(run_id, dispatch_generation=0))["status"] == "succeeded"
    first = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    question_url = base + f"/{first['id']}/questions"
    questions = (await client.get(question_url, params={"branch_id": branch})).json()["questions"]
    question, = [item for item in questions if item["field"].endswith("/checks")]
    payload = {
        "branch_id": branch, "message": "Без дополнительных проверок", "request_mode": "design",
        "project_answer": {"base_revision_id": first["id"], "answers": [{
            "path": question["field"], "target_id": question["target_id"],
            "side_network_id": question["side_network_id"], "value": [],
        }]},
    }
    applied = await client.post(
        chat_url + "/runs", headers={**headers, "Idempotency-Key": str(uuid.uuid4())}, json=payload,
    )
    assert applied.status_code == 202, applied.text
    advice_run = uuid.UUID(applied.json()["run_id"])
    assert (await execute_run(advice_run, dispatch_generation=0))["status"] == "succeeded"
    current = (await client.get(base + "/latest", params={"branch_id": branch})).json()["design"]
    assert current["parent_id"] == first["id"] and len(calls) == 1

    design_url = base + f"/{current['id']}"
    artifacts = {}
    for extension in ("requirements", "json", "txt", "drawio"):
        response = await client.post(design_url + "/download/" + extension, headers=headers)
        assert response.status_code == 200, response.text
        assert hashlib.sha256(response.content).hexdigest() == current["artifact_sha256"][extension]
        artifacts[extension] = response.text
    requirements = json.loads(artifacts["requirements"])
    result = json.loads(artifacts["json"])
    xml = fromstring(artifacts["drawio"])
    diagram = (await client.get(design_url + "/diagram")).json()["scene"]
    rows = json.loads(xml.attrib["data-rule-rows"])
    assert rows == diagram["rule_rows"]
    assert len(rows) == result["confirmed_rules_count"] == 14
    flows = {flow["id"]: flow for flow in requirements["flows"]}
    assert {flow_id for row in rows for flow_id in row["flow_ids"]} == set(flows)
    assert sum(rule["type"] == "file" for rule in result["rules"]) == 6
    for row, rule in zip(rows, result["rules"], strict=True):
        assert (row["number"], row["id"]) == (rule["id"], rule["rule_id"])
        assert row["flow_ids"] == rule["flow_ids"]
        assert (row["from_network_id"], row["to_network_id"]) == (rule["from"], rule["to"])
        assert row["two_way"] == (rule["mode"] == "two-way")
        assert row["destination_port"] == rule["port"]
        line = next(line for line in artifacts["txt"].splitlines()
                    if line.startswith(f"{rule['id']}. "))
        assert rule["description"] in line
    usb, = [row for row in rows if "USB" in (row["protocol"] or "")]
    assert usb["protocol"] == "SMB PUSH, USB"
    assert any(cell.get("source") == "usb-1" and cell.get("target") == "node-A"
               for cell in xml.iter("mxCell"))
