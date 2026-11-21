"""Saved calculated output matches inputs/exports and never invents unknown usage."""

import json
from xml.etree.ElementTree import fromstring

from test_sinoniks_contract19 import data_project, file_project

from partner_agent.requirements_result import result_artifacts19
from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_result_export import export_result


def test_result_snapshot_is_exact_and_inputs_remain_separate():
    request = Requirements19.model_validate(data_project())
    before = request.model_dump_json()
    result, artifacts = result_artifacts19(request, session_id="session-1")
    public = json.loads(artifacts["json"])
    expected = export_result(request, session_id="session-1")
    assert public == expected | {"accounting_export": "/api/v1/chats/session-1/accounting/export"}
    assert public["confirmed_rules_count"] == 1
    assert public["usage"]["actual_cost_usd"] is None
    assert public["timing"]["mean_turn_seconds"] is None
    assert json.loads(artifacts["requirements"])["schema_version"] == 3
    assert artifacts["txt"] == result["explanation"] + "\n"
    assert "Минимальное число правил Синоникса по заданным условиям: 1" in artifacts["txt"]
    assert "Цех" in artifacts["txt"] and "Офис" in artifacts["txt"]
    assert "UDP" in artifacts["txt"] and "514" in artifacts["txt"]
    scene = json.loads(artifacts["diagram"])
    xml = fromstring(artifacts["drawio"])
    rows = scene["rule_rows"]
    assert scene["profile"] == "calculated-sinoniks-19"
    assert [(row["number"], row["id"]) for row in rows] == [
        (rule["id"], rule["rule_id"]) for rule in public["rules"]
    ]
    assert json.loads(xml.attrib["data-rule-rows"]) == rows
    assert request.model_dump_json() == before


def test_unknown_placement_is_not_described_as_confirmed_architecture():
    document = data_project()
    document["networks"][0]["trust"] = None
    result, artifacts = result_artifacts19(
        Requirements19.model_validate(document), session_id="session-1"
    )
    assert result["synonyx"] is None
    assert result["confirmed_rules_count"] is None
    assert json.loads(artifacts["diagram"])["placement_pending"]
    assert "Предварительная топология" in artifacts["drawio"]
    assert "Размещение пока не определено" in artifacts["txt"]
    assert "Полный минимум пока не подтверждён" in artifacts["txt"]


def test_installed_firewall_is_named_once_at_the_existing_boundary():
    document = data_project()
    original = Requirements19.model_validate(document)
    explicit_null = Requirements19.model_validate({**document, "existing_firewall_name": None})
    assert explicit_null.model_dump_json() == original.model_dump_json()
    original_result, _ = result_artifacts19(original, session_id="session-1")
    request = Requirements19.model_validate({**document, "existing_firewall_name": "МЭ-01"})
    result, artifacts = result_artifacts19(request, session_id="session-1")
    identity = {"id": "firewall", "name": "МЭ-01", "existing": True}
    assert result["firewall"] == {"side": "open", **identity}
    assert result["synonyx"] == original_result["synonyx"]
    assert result["rules"] == original_result["rules"]
    assert json.loads(artifacts["requirements"])["existing_firewall_name"] == "МЭ-01"
    assert "используется установленный экран «МЭ-01»" in artifacts["txt"]
    scene = json.loads(artifacts["diagram"])
    assert scene["firewall"] == identity
    for page in scene["pages"]:
        firewall, = [box for box in page["boxes"] if box["kind"] == "firewall"]
        assert firewall["id"] == identity["id"]
        edges = [edge for edge in page["edges"] if edge["id"].endswith("-wall")]
        assert all({edge["source"], edge["target"]} == {"firewall", "node-A"} for edge in edges)
    xml = fromstring(artifacts["drawio"])
    assert json.loads(xml.attrib["data-firewall"]) == identity
    assert xml.find(".//mxCell[@value='МЭ-01']") is not None


def test_preview_displays_known_systems_before_any_rule_can_be_calculated():
    document = data_project()
    document["flows"][0]["receiver_ids"] = []
    request = Requirements19.model_validate(document)
    result, artifacts = result_artifacts19(request, session_id="early-preview")
    assert result["rules_count"] == 0 and result["confirmed_rules_count"] is None
    scene = json.loads(artifacts["diagram"])
    assert scene["rule_rows"] == []
    page, = scene["pages"]
    assert page["edges"] == []
    boxes = {box["id"]: box for box in page["boxes"]}
    assert "Офис" in boxes["open-zone"]["text"] and "Цех" in boxes["closed-zone"]["text"]
    assert "Связи пока не рассчитаны" in boxes["pending-rules"]["text"]
    assert {box["text"] for box in page["boxes"] if box["id"].startswith("endpoint-")} == {
        system.name for system in request.systems
    }
    xml = fromstring(artifacts["drawio"])
    assert xml.findall(".//mxCell[@edge='1']") == []

    document["systems"] = []
    document["flows"][0]["sender_ids"] = []
    empty = Requirements19.model_validate(document)
    _, no_topology = result_artifacts19(empty, session_id="early-preview")
    assert "diagram" not in no_topology and "drawio" not in no_topology


def test_automatic_bidirectional_files_keep_both_rules_and_pull_users():
    document = file_project()
    for side in document["flows"][0]["sides"]:
        side["automations"].append(side["automations"][0] | {"operation": "PUSH"})
    result, artifacts = result_artifacts19(
        Requirements19.model_validate(document), session_id="session-1"
    )
    assert result["confirmed_rules_count"] == 2 and result["file_users_count"] == 2
    assert len(result["rules"]) == 2
    assert "PULL" in artifacts["txt"] and "SMB" in artifacts["txt"]
    assert "Пользователей передачи файлов: 2" in artifacts["txt"]
