from xml.etree import ElementTree

from partner_agent.sinoniks_calculation import calculate_rules, solve_project
from partner_agent.sinoniks_contract19 import Requirements19
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


def test_covers_mixed_usb_and_icap_with_push_and_one_human():
    request = plan([
        files(
            "updates", side("office", usb=True, actor_ids=["operator"],
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
