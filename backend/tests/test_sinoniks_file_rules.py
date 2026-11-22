import json
from uuid import uuid4
from xml.etree import ElementTree

import pytest

from partner_agent.project_answers import ProjectAnswer19, apply_answer, question_choices
from partner_agent.requirements_input import bind_shared_file_checks
from partner_agent.requirements_result import result_artifacts19
from partner_agent.sinoniks_calculation import calculate_rules, solve_project
from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_contract19_io import assess
from partner_agent.sinoniks_export import scene_drawio
from partner_agent.sinoniks_file_rules import calculate_file_rules
from partner_agent.sinoniks_layout19 import layout19
from partner_agent.sinoniks_result_export import export_result


def plan(flows, **extra):
    return Requirements19.model_validate({
        "id": "project", "networks": [
            {"id": "office", "name": "Офис", "trust": "open"},
            {"id": "plant", "name": "Цех", "trust": "closed"},
        ],
        "systems": [
            {"id": "office-store", "network_id": "office", "kind": "storage"},
            {"id": "plant-store", "network_id": "plant", "kind": "storage"},
            {"id": "media", "kind": "usb", "attachment": "node-A"},
        ],
        "actors": [{"id": "operator"}], "flows": flows, **extra,
    })


def side(network, **extra):
    return {"network_id": network, "manual": False, "usb": False, "checks": [],
            "received_files_shared": True, **extra}


def files(name, source, destination, **extra):
    return {"id": name, "kind": "file", "direction": "forward",
            "sender_network_id": source["network_id"],
            "receiver_network_id": destination["network_id"],
            "sides": [source, destination], **extra}


def automation(operation, network, protocol="SFTP"):
    return {"operation": operation, "protocol": protocol, "storage_id": f"{network}-store"}


def test_two_pulls_and_pushes_on_both_nodes_become_two_rules_and_two_users():
    request = plan([files(
        "sync", side("office", automations=[automation(op, "office") for op in ("PULL", "PUSH")]),
        side("plant", automations=[automation(op, "plant") for op in ("PULL", "PUSH")]),
        direction="both",
    )])
    result = calculate_file_rules(request, outside_network_id="office")
    assert result.minimum == 2 and result.file_users_count == 2
    assert [rule.direction for rule in result.rules] == ["forward", "forward"]
    expert = export_result(request, session_id="test-u01")
    assert expert["file_users_count"] == 2 and expert["file_users_range"] is None
    details = expert["file_users_breakdown"]
    assert details["manual"] == {"min": 0, "max": 0, "actors": []}
    assert details["technical_pull_count"] == 2
    assert {operation["storage_id"] for operation in details["technical_pulls"]} == {
        "office-store", "plant-store",
    }
    assert [(rule["from"], rule["to"], rule["protocol"]) for rule in expert["rules"]] == [
        ("office", "plant", {"office": "SFTP, PULL", "plant": "SFTP, PUSH"}),
        ("plant", "office", {"office": "SFTP, PUSH", "plant": "SFTP, PULL"}),
    ]


@pytest.mark.parametrize("gap", [{"checks": None}, {"received_files_shared": None}])
def test_unknown_checks_or_access_do_not_invent_extra_push_users(gap):
    request = plan([files(
        "manual-push", side("office", manual=True, manual_protocol="SFTP",
                            actor_ids=["operator"]),
        side("plant", automations=[automation("PUSH", "plant")], **gap),
    )])
    result = calculate_file_rules(request, outside_network_id="office")
    assert result.minimum is None and result.issues
    assert result.declared_pulls == 0 and result.file_users_count == 1
    assert result.user_details.upper_unknown_reasons == ()


def test_unknown_checks_do_not_remove_proven_two_pull_lower_bound():
    request = plan([files(
        "sync",
        side("office", checks=None, automations=[
            automation(op, "office") for op in ("PULL", "PUSH")
        ]),
        side("plant", checks=None, automations=[
            automation(op, "plant") for op in ("PULL", "PUSH")
        ]),
        direction="both",
    )])
    result = calculate_file_rules(request, outside_network_id="office")
    assert len(result.rules) == result.lower_bound == result.declared_pulls == 2
    assert result.minimum is None and result.file_users_count is None
    assert result.user_details.manual_upper == 0


@pytest.mark.parametrize("shared", [False, True])
def test_u01_only_groups_explicitly_shared_unknown_checks(shared):
    raw = plan([
        files(
            f"flow-{source}",
            side(source, checks=None, automations=[automation("PULL", source)]),
            side(target, checks=None, automations=[automation("PUSH", target)]),
        )
        for source, target in (("office", "plant"), ("plant", "office"))
    ]).model_dump(mode="json")
    question = "Проверки одинаковые, разделять доступ не нужно." if shared else "Уточните обмен."
    bind_shared_file_checks(raw, question)
    request = Requirements19.model_validate(raw)
    choices = [item for item in question_choices(request) if item["field"].endswith("/checks")]
    assert len(choices) == (1 if shared else 4)
    first = choices[0]
    answer = ProjectAnswer19(
        base_revision_id=uuid4(), path=first["field"], target_id=first["target_id"],
        side_network_id=first["side_network_id"], value=[{"kind": "signature"}],
    )
    updated = apply_answer(request, answer)
    file_sides = [item for flow in updated.flows for item in flow.sides]
    assert sum(item.checks is not None for item in file_sides) == (4 if shared else 1)
    assert all(item.checks is None or item.checks[0].kind == "signature" for item in file_sides)
    partial = calculate_file_rules(request, outside_network_id="office")
    assert partial.lower_bound == partial.declared_pulls == 2
    assert partial.user_details.manual_upper == 0 and partial.minimum is None


def test_isolated_departments_require_two_rules_without_counting_each_as_person():
    request = plan([
        files(f"flow-{department}",
              side("office", manual=True, manual_protocol="SFTP", actor_ids=[department],
                   checks=None),
              side("plant", checks=None, received_files_shared=False, access_group=department))
        for department in ("engineering", "quality")
    ], actors=[{"id": "engineering", "name": "Отдел 1"}, {"id": "quality", "name": "Группа 2"}])
    result = calculate_file_rules(request, outside_network_id="office")
    assert len(result.rules) == result.lower_bound == 2
    assert result.minimum is None
    assert result.file_users_count is None and result.file_users_range is None
    assert result.user_details.actor_ids == () and result.user_details.manual_upper is None
    expert, artifacts = result_artifacts19(request, session_id="isolated-groups")
    assert "Ручные пользователи: неизвестно." in artifacts["txt"]
    assert [rule["directions"][0]["received_files_shared"] for rule in expert["rules"]] == [
        False, False,
    ]
    groups = {rule["directions"][0]["access_groups"]["plant"] for rule in expert["rules"]}
    assert groups == {"engineering", "quality"}
    assert all(f"Раздельный доступ (Цех): {group}" in artifacts["txt"] for group in groups)
    native = ElementTree.fromstring(artifacts["drawio"])
    access = [row["file_access_directions"][0]
              for row in json.loads(native.attrib["data-rule-rows"])]
    assert {lane["access_group"] for lane in access} == groups
    assert all(lane["received_files_shared"] is False for lane in access)


def test_f04_manual_sftp_and_smb_push_need_one_known_user():
    request = plan([files(
        "programs",
        side("office", manual=True, manual_protocol="SFTP", actor_ids=["operator"],
             received_files_shared=None, checks=[
                 {"kind": "name_mask"}, {"kind": "size"},
                 {"kind": "icap", "icap_service": "antivirus"},
             ]),
        side("plant", automations=[automation("PUSH", "plant", "SMB")]),
        sender_ids=["office-store"], receiver_ids=["plant-store"],
    )])
    assessment = assess(request)
    assert assessment.status == "needs_clarification"
    assert [issue.path for issue in assessment.issues] == [
        "/flows/0/sides/0/checks/0/details", "/flows/0/sides/0/checks/1/details",
    ]
    expert = export_result(request, session_id="test-f04")
    assert expert["status"] == "completed"
    assert expert["rules_count"] == expert["file_users_count"] == 1
    rule, = expert["rules"]
    assert (rule["from"], rule["to"], rule["mode"]) == ("office", "plant", "one-way")
    assert rule["protocol"] == {"office": "SFTP, вручную", "plant": "SMB, PUSH"}
    assert {check["kind"] for check in rule["checks_by_node"]["A"]} == {
        "name_mask", "size", "icap",
    }
    assert rule["checks_by_node"]["B"] == []


def test_f04_known_check_parameters_do_not_invent_extra_scope():
    request = plan([files(
        "programs", side("office", checks=None, manual=True, manual_protocol="SFTP",
                         actor_ids=["operator"]),
        side("plant", automations=[automation("PUSH", "plant", "SMB")], checks=[
            {"kind": "name_mask", "details": "*.nc"},
            {"kind": "size", "details": "10 МБ"},
            {"kind": "icap", "icap_service": "antivirus"},
        ]),
        sender_ids=["office-store"], receiver_ids=["plant-store"],
    )])
    assert [issue.path for issue in assess(request).issues] == ["/flows/0/sides/0/checks"]
    result = calculate_file_rules(request, outside_network_id="office")
    assert len(result.rules) == result.lower_bound == 1
    assert result.minimum is None
    assert [issue.reason for issue in result.issues] == ["file_check_scope_unconfirmed"]


def test_covers_mixed_usb_and_icap_with_push_and_one_human():
    request = plan([
        files(
            "updates", side("office", manual=True, usb=True, actor_ids=["operator"],
                            checks=[{"kind": "icap", "icap_service": "antivirus"}]),
            side("plant", automations=[automation("PUSH", "plant", "SMB")]),
            sender_ids=["media"], receiver_ids=["plant-store"],
        ),
        {
            "id": "telemetry", "kind": "data", "sender_network_id": "plant",
            "receiver_network_id": "office", "sender_ids": ["plant-store"],
            "receiver_ids": ["office-store"], "protocol": "MQTT",
        },
        {
            "id": "clock", "kind": "data", "sender_network_id": "office",
            "receiver_network_id": "plant", "sender_ids": ["office-store"],
            "protocol": "UDP", "application": "ntp", "destination_port": 123,
        },
    ])
    result = calculate_rules(request)
    assert result["rules_count"] == result["rules_lower_bound"] == 3
    assert result["file_users_count"] == 1
    assert result["declared_pull_users"] == 0
    expert = export_result(request, session_id="test-f07")
    assert expert["schema_version"] == "1.1" and expert["session_id"] == "test-f07"
    assert expert["status"] == "completed" and expert["rules_count"] == 3
    assert expert["file_users_count"] == 1
    assert expert["file_users_breakdown"] == {
        "manual": {"min": 1, "max": 1, "actors": [{"id": "operator", "name": None}]},
        "technical_pull_count": 0,
        "technical_pulls": [],
        "upper_unknown_reasons": [],
    }
    mqtt = next(rule for rule in expert["rules"] if rule["protocol"] == "MQTT")
    assert mqtt["port"] is None and mqtt["mode"] == "two-way"
    file = next(rule for rule in expert["rules"] if rule["type"] == "file")
    assert file["protocol"] == {"office": "USB-носитель на Узле А", "plant": "SMB, PUSH"}
    assert file["checks"] == ["ICAP: внешний антивирус"]
    assert file["checks_by_node"]["A"][0]["icap_service"] == "antivirus"
    assert expert["usage"]["status"] == "not_measured"
    scene = layout19(request, solve_project(request))
    assert [(row["number"], row["id"]) for row in scene["rule_rows"]] == [
        (rule["id"], rule["rule_id"]) for rule in expert["rules"]
    ]
    page = scene["pages"][0]
    usb_edges = [edge for edge in page["edges"] if "usb" in edge["id"]]
    assert [(edge["source"], edge["target"]) for edge in usb_edges] == [("usb-1", "node-A")]
    for edge in page["edges"]:
        assert all(a[0] == b[0] or a[1] == b[1] for a, b in zip(
            edge["points"], edge["points"][1:],
        ))
    xml = ElementTree.fromstring(scene_drawio(scene))
    assert any(cell.get("source") == "usb-1" and cell.get("target") == "node-A"
               for cell in xml.findall(".//mxCell"))
    assert xml.find(".//mxCell[@id='node-A'][@vertex='1']") is not None


@pytest.mark.parametrize("manual_protocol", [None, "SFTP"])
def test_usb_only_shows_sftp_when_it_is_explicitly_specified(manual_protocol):
    request = plan([files(
        "updates",
        side("office", manual=True, usb=True, manual_protocol=manual_protocol,
             actor_ids=["operator"]),
        side("plant", automations=[automation("PUSH", "plant", "SMB")]),
        receiver_ids=["plant-store"],
    )])
    result, artifacts = result_artifacts19(request, session_id="usb-export")
    rule, = result["rules"]
    diagram = ElementTree.fromstring(artifacts["drawio"])
    row, = json.loads(diagram.attrib["data-rule-rows"])
    assert row["id"] == rule["rule_id"] and row["number"] == rule["id"]
    assert (row["sender_side"], row["two_way"]) == ("A", False)
    for medium in (artifacts["txt"], rule["protocol"]["office"], row["protocol"]):
        assert "USB" in medium and ("SFTP" in medium) == (manual_protocol is not None)
    assert "SMB PUSH" in row["protocol"]


def test_same_profile_merges_both_directions_and_deduplicates_person():
    office = side("office", manual=True, manual_protocol="SFTP", actor_ids=["operator"])
    plant = side("plant", manual=True, manual_protocol="SFTP", actor_ids=["operator"])
    result = calculate_file_rules(plan([
        files("one", office, plant), files("two", plant, office),
    ]), outside_network_id="office")
    assert result.minimum == 1 and result.rules[0].direction == "both"
    assert result.file_users_count == 1


def test_size_and_external_icap_apply_on_their_own_nodes_and_groups_stay_isolated():
    office = side("office", manual=True, checks=[{"kind": "size"}])
    plant = side("plant", manual=True, received_files_shared=False,
                 checks=[{"kind": "icap", "icap_service": "antivirus"}])
    result = calculate_file_rules(plan([
        files("group-1", office, {**plant, "access_group": "one"}),
        files("group-2", office, {**plant, "access_group": "two"}),
        files("group-1-again", office, {**plant, "access_group": "one"}),
    ], manual_users={"lower": 2, "upper": 5}), outside_network_id="office")
    assert result.minimum == 2 and result.file_users_count is None
    assert result.file_users_range is not None
    assert (result.file_users_range.lower, result.file_users_range.upper) == (2, 5)


def test_u02_isolated_groups_are_separate_even_when_checks_are_identical():
    office = side("office", manual=True)
    plant = side("plant", manual=True, received_files_shared=False)
    request = plan([
        files("engineering", office, {**plant, "access_group": "engineering"}),
        files("quality", office, {**plant, "access_group": "quality"}),
    ])
    expert = export_result(request, session_id="test-u02-isolated")
    assert expert["rules_count"] == 2
    assert expert["file_users_count"] is None and expert["file_users_range"] is None
    assert {rule["directions"][0]["access_groups"]["plant"] for rule in expert["rules"]} == {
        "engineering", "quality",
    }


def test_u02_common_access_keeps_independent_node_checks_in_one_rule():
    request = plan([files(
        "common", side("office", manual=True, checks=[{"kind": "size"}]),
        side("plant", manual=True, checks=[{"kind": "icap", "icap_service": "antivirus"}]),
    )], manual_users={"lower": 2, "upper": 5})
    expert = export_result(request, session_id="test-u02-common")
    assert expert["rules_count"] == 1
    assert expert["file_users_count"] is None
    assert expert["file_users_range"] == {"min": 2, "max": 5}
    checks = expert["rules"][0]["checks_by_node"]
    assert [check["kind"] for check in checks["A"]] == ["size"]
    assert [check["kind"] for check in checks["B"]] == ["icap"]


def test_unknown_methods_and_checks_never_become_a_certified_rule_count():
    request = plan([files(
        "pending", side("office", manual=None, usb=None, checks=None),
        side("plant", manual=None, usb=None, checks=None, received_files_shared=None),
        direction="both",
    )])
    result = calculate_rules(request)
    assert result["rules_count"] is None
    assert result["rules_lower_bound"] == 1 and result["file_users_count"] is None
    assert result["file_issues"]
    expert = export_result(request, session_id="test-n01")
    assert expert["status"] == "preliminary"
    assert expert["rules_count"] == len(expert["rules"])
    assert expert["confirmed_rules_count"] is None and expert["rules_lower_bound"] == 1
    assert expert["rules"][0]["protocol"] == {"office": None, "plant": None}
    assert expert["rules_lower_bound_breakdown"] == {"data": 0, "file": 1}
    assert {issue["reason"] for issue in expert["rules"][0]["provisional_reasons"]} == {
        "file_checks_unknown", "file_visibility_unknown", "file_method_unknown_or_unavailable",
    }
    assert expert["file_users_breakdown"]["upper_unknown_reasons"] == [
        "manual_upper_unknown", "file_rules_incomplete",
    ]


def test_expert_export_preserves_declared_initiator_and_known_user_range():
    request = plan([files(
        "files-both", side("plant", manual=True), side("office", manual=True), direction="both",
    )], manual_users={"lower": 4, "upper": 9})
    expert = export_result(request, session_id="test-both")
    assert expert["rules"][0]["from"] == "plant"
    assert expert["rules"][0]["to"] == "office"
    assert expert["rules"][0]["mode"] == "two-way"
    assert expert["file_users_count"] is None
    assert expert["file_users_range"] == {"min": 4, "max": 9}


def test_expert_export_keeps_unsupported_only_wish_without_rules():
    request = plan([], requests_for_vendor=[
        {"id": "wish", "text": "встроенный антивирус", "kind": "unsupported"},
    ])
    expert = export_result(request, session_id="test-unsupported")
    assert expert["status"] == "unsupported"
    assert expert["rules"] == [] and expert["rules_count"] == 0
    assert expert["requests_for_vendor"] == ["встроенный антивирус"]


def test_ambiguous_multiple_automations_are_kept_without_inventing_pairings():
    result = calculate_file_rules(plan([files(
        "pairing", side("office", automations=[
            automation("PULL", "office"), automation("PULL", "office", "SMB"),
        ]), side("plant", automations=[automation("PUSH", "plant")]),
    )]), outside_network_id="office")
    assert result.minimum is None
    assert result.declared_pulls == 2 and len(result.rules) == 3
    assert "file_automation_pairing_unknown" in {issue.reason for issue in result.issues}


def test_only_disjoint_automations_can_share_a_bidirectional_rule():
    result = calculate_file_rules(plan([
        files("incoming", side("office", automations=[automation("PULL", "office")]),
              side("plant", manual=True)),
        files("outgoing", side("plant", automations=[automation("PULL", "plant")]),
              side("office", manual=True)),
        files("outgoing-other", side("plant", manual=True),
              side("office", received_files_shared=False, access_group="restricted",
                   automations=[automation("PUSH", "office")])),
    ]), outside_network_id="office")
    assert result.minimum == 3 and result.declared_pulls == 2
