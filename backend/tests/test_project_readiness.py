import json
from copy import deepcopy

from test_sinoniks_contract19 import data_project, file_project

from partner_agent.project_readiness import readiness_report
from partner_agent.requirements_result import result_artifacts19
from partner_agent.sinoniks_contract19 import Requirements19


def build(raw):
    request = Requirements19.model_validate(raw)
    result, artifacts = result_artifacts19(request, session_id="review-test")
    return request, result, artifacts


def test_complete_project_is_checked_without_deployment_guarantee_or_format_change():
    request, result, artifacts = build(data_project())
    report = result["readiness"]
    assert report["status"] == "checked"
    assert not report["missing"] and not report["conflicts"]
    assert "инженер" in report["summary"]
    assert all(check["status"] == "passed" for check in report["checks"])
    assert "readiness" not in json.loads(artifacts["json"])
    assert "Расчёт проверен по заданным условиям" in artifacts["drawio"]
    assert "readiness" in json.loads(artifacts["diagram"])["pages"][0]["boxes"][-1]["id"]
    assert readiness_report(request, result, artifacts)["status"] == "checked"


def test_unknown_direction_is_a_gap_not_a_conflict():
    raw = data_project()
    raw["flows"][0]["mode"] = None
    _, result, _ = build(raw)
    report = result["readiness"]
    assert report["status"] == "needs_clarification"
    assert report["missing"] and not report["conflicts"]


def test_explicit_unknown_and_optional_port_remain_preliminary_not_a_fake_failure():
    for field in ("mode", "destination_port"):
        raw = data_project()
        raw["flows"][0][field] = None
        raw["unknowns"] = [{"path": "/flows/0/" + field, "target_id": "logs",
                            "reason": "user_unknown"}]
        _, result, _ = build(raw)
        report = result["readiness"]
        assert report["status"] == "preliminary"
        assert any(item["answered_unknown"] for item in report["missing"])
        assert not report["conflicts"]


def test_contradictory_trust_is_distinct_from_missing_data():
    raw = data_project()
    raw["networks"][0]["trust"] = "closed"
    _, result, _ = build(raw)
    assert result["readiness"]["status"] == "conflict"
    assert result["readiness"]["conflicts"]


def test_recommendations_do_not_mutate_conditions_or_make_firewall_optional():
    raw = file_project()
    raw["systems"].append({"id": "spare", "name": "Запасной сервер", "network_id": "office"})
    before = deepcopy(raw)
    _, result, _ = build(raw)
    report = result["readiness"]
    assert {item["code"] for item in report["recommendations"]} == {"file_checks", "unused_system"}
    assert all("МЭ" not in item["message"] for item in report["recommendations"])
    assert raw == before


def test_missing_firewall_is_an_error_not_an_optional_tip():
    request, result, artifacts = build(data_project())
    result["firewall"] = None
    report = readiness_report(request, result, artifacts)
    assert report["status"] == "conflict"
    assert any(item["code"] == "firewall_missing" for item in report["conflicts"])


def test_tampered_native_objects_or_expert_rules_cannot_get_checked_status():
    request, result, artifacts = build(data_project())
    for key in ("json", "drawio"):
        altered = dict(artifacts)
        if key == "json":
            exported = json.loads(altered[key])
            exported["rules"][0]["port"] = 999
            altered[key] = json.dumps(exported)
        else:
            altered[key] = altered[key].replace('value="Узел А"', 'value="Другой узел"')
        report = readiness_report(request, result, altered)
        assert report["status"] == "conflict"
        assert any(item["code"] == "artifact_mismatch" for item in report["conflicts"])


def test_known_incompatible_ntp_is_not_treated_as_just_missing_information():
    raw = data_project()
    raw["flows"][0].update(application="ntp", protocol="TCP")
    _, result, _ = build(raw)
    assert result["readiness"]["status"] == "conflict"
    assert any(item["code"].startswith("ntp_") for item in result["readiness"]["conflicts"])


def test_missing_diagram_cannot_get_checked_status():
    request, result, artifacts = build(data_project())
    artifacts.pop("diagram")
    artifacts.pop("drawio")
    assert readiness_report(request, result, artifacts)["status"] == "conflict"


def test_solver_protected_side_assumption_is_visible_without_relabelling_open_networks():
    raw = data_project()
    raw["networks"][1]["trust"] = "open"
    raw["protected_network_id"] = "plant"
    _, result, _ = build(raw)
    assert result["readiness"]["status"] == "preliminary"
    assert result["readiness"]["assumptions"]
    assert all(network["trust"] == "open" for network in result["networks"])


def test_out_of_scope_request_is_a_limitation_not_a_completed_solution():
    raw = data_project()
    raw["flows"] = []
    raw["requests_for_vendor"] = [{"id": "routing", "kind": "unsupported", "flow_id": None,
                                  "text": "Прямая маршрутизация сетей"}]
    _, result, _ = build(raw)
    report = result["readiness"]
    assert report["status"] == "unsupported"
    assert report["limitations"] and not report["conflicts"]
    assert report["checks"][1]["status"] == "pending"
