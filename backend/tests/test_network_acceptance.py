"""Independent F01/F02 acceptance facts, including the actual directed diagram boundary."""

from copy import deepcopy
from xml.etree.ElementTree import fromstring

import pytest

from partner_agent.sinoniks_calculation import solve_project
from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_export import scene_drawio
from partner_agent.sinoniks_layout19 import layout19
from partner_agent.sinoniks_result_export import result_document


def network_project(flows):
    return Requirements19.model_validate({
        "id": "acceptance-network",
        "networks": [{"id": "outer", "name": "Общая сеть", "trust": "open"},
                     {"id": "inner", "name": "Закрытая станция", "trust": "closed"}],
        "systems": [
            {"id": "dispatch", "name": "Диспетчер", "network_id": "outer"},
            {"id": "collector", "name": "Сервер журналов", "network_id": "outer"},
            {"id": "clock", "name": "Сервер времени", "network_id": "outer"},
            *[{"id": f"device-{i}", "name": f"Устройство {i}", "network_id": "inner"}
              for i in range(3)],
        ],
        "flows": flows,
    })


def data_flow(identifier="logs", **changes):
    return {"id": identifier, "kind": "data", "sender_network_id": "inner",
            "receiver_network_id": "outer", "sender_ids": ["device-0"],
            "receiver_ids": ["collector"], "protocol": "UDP", "destination_port": 514,
            "mode": "one-way", **changes}


def f02():
    return network_project([
        data_flow("control", sender_network_id="outer", receiver_network_id="inner",
                  sender_ids=["dispatch"], receiver_ids=["device-0"],
                  protocol="Modbus TCP", destination_port=502, mode="two-way"),
        data_flow(),
        data_flow("time", sender_network_id="outer", receiver_network_id="inner",
                  sender_ids=["clock"], receiver_ids=["device-0"],
                  protocol="UDP", destination_port=123, application="ntp"),
    ])


def reachable(edges, source, target, removed=None):
    graph = {}
    for edge in edges:
        a, b = edge["source"], edge["target"]
        if removed in (a, b):
            continue
        start, end = edge["start_arrow"] != "none", edge["end_arrow"] != "none"
        if end or not start:
            graph.setdefault(a, set()).add(b)
        if start or not end:
            graph.setdefault(b, set()).add(a)
    seen, pending = set(), [source]
    while pending:
        node = pending.pop()
        if node == target:
            return True
        if node not in seen:
            seen.add(node)
            pending.extend(graph.get(node, ()))
    return False


def test_f02_count_modes_firewall_and_bidirectional_request_path():
    request = f02()
    result = solve_project(request)
    document = result_document(request, result, session_id="synthetic-f02")
    assert document["confirmed_rules_count"] == 3 and document["file_users_count"] == 0
    assert {(r.protocol, r.port, r.mode) for r in result.data.rules} == {
        ("Modbus TCP", 502, "two-way"), ("UDP", 514, "one-way"), ("UDP", 123, "one-way"),
    }
    assert result.data.placement.node_a_network_id == "outer"
    assert result.data.placement.node_b_network_id == "inner"
    page = layout19(request, result)["pages"][0]
    boxes = {box["id"]: box for box in page["boxes"]}
    protected, appliance, firewall = (boxes[key] for key in ("closed-zone", "sinoniks", "firewall"))
    assert firewall["x"] < protected["x"] < firewall["x"] + firewall["width"]
    assert protected["x"] < appliance["x"]
    assert appliance["x"] + appliance["width"] < protected["x"] + protected["width"]
    assert boxes["node-A"]["x"] < boxes["core"]["x"] < boxes["node-B"]["x"]
    control = next(rule for rule in result.data.rules if rule.protocol == "Modbus TCP")
    edges = [edge for edge in page["edges"] if edge["rule_id"] == control.id]
    sender = next(box["id"] for box in page["boxes"] if box["text"] == "Диспетчер")
    receiver = next(box["id"] for box in page["boxes"] if box["text"] == "Устройство 0")
    assert reachable(edges, sender, receiver), "The request must enter from the initiator"
    assert reachable(edges, receiver, sender), "The response must return in the same rule"
    for hardware in ("firewall", "node-A", "core", "node-B"):
        assert not reachable(edges, sender, receiver, removed=hardware), "Boundary bypass"
    for protocol, source_name, target_name in (
        ("UDP", "Устройство 0", "Сервер журналов"),
        ("ntp", "Сервер времени", "Устройство 0"),
    ):
        rule = next(r for r in result.data.rules if
                    (r.application == "ntp" if protocol == "ntp" else r.port == 514))
        links = [edge for edge in page["edges"] if edge["rule_id"] == rule.id]
        source = next(b["id"] for b in page["boxes"] if b["text"] == source_name)
        target = next(b["id"] for b in page["boxes"] if b["text"] == target_name)
        assert reachable(links, source, target)
        assert not reachable(links, target, source), "A one-way rule must not allow a response"


@pytest.mark.parametrize("protocol", ["TCP", "MQTT", "Modbus TCP", "МЭК 104"])
def test_query_response_uses_one_rule_with_two_terminal_arrows_in_native_xml(protocol):
    request = network_project([data_flow(protocol=protocol, mode="two-way")])
    result = solve_project(request)
    assert result.minimum == 1
    scene = layout19(request, result)
    page = scene["pages"][0]
    endpoints = {box["id"] for box in page["boxes"] if box["kind"] == "system"}
    terminals = [edge for edge in page["edges"] if
                 edge["source"] in endpoints or edge["target"] in endpoints]
    assert len(terminals) == 2
    xml = fromstring(scene_drawio(scene))
    for edge in terminals:
        assert edge["start_arrow"] == "open" and edge["end_arrow"] == "block"
        cell = xml.find(f".//mxCell[@id='{edge['id']}']")
        style = dict(part.split("=", 1) for part in cell.attrib["style"].split(";") if "=" in part)
        assert style["startArrow"] == "open" and style["endArrow"] == "block"


@pytest.mark.parametrize("change,minimum", [
    ({}, 1), ({"receiver_ids": ["clock"]}, 2), ({"destination_port": 515}, 2),
    ({"protocol": "TCP"}, 2), ({"mode": "two-way"}, 2),
])
def test_f01_compatible_senders_merge_and_explicit_differences_split(change, minimum):
    request = network_project([
        data_flow("a", sender_ids=["device-0"]),
        data_flow("b", sender_ids=["device-1"]),
        data_flow("c", sender_ids=["device-2"], **change),
    ])
    calculation = solve_project(request)
    assert calculation.minimum == minimum
    assert set().union(*(r.sender_ids for r in calculation.data.rules)) == {
        "device-0", "device-1", "device-2",
    }
    initial = [rule.as_dict() for rule in calculation.data.rules]
    snapshot = deepcopy(request.model_dump(mode="json"))
    request.flows.reverse()
    request.systems.reverse()
    request.networks.reverse()
    assert [rule.as_dict() for rule in solve_project(request).data.rules] == initial
    assert sorted(request.model_dump()["flows"], key=lambda f: f["id"]) == sorted(
        snapshot["flows"], key=lambda f: f["id"],
    )


def test_unknown_port_and_ntp_client_count_are_not_invented():
    request = f02()
    request.flows[0].destination_port = None
    request.flows[2].receiver_ids = [f"device-{i}" for i in range(3)]
    result = solve_project(request)
    assert result.minimum == 3
    modbus = next(rule for rule in result.data.rules if rule.protocol == "Modbus TCP")
    assert modbus.port is None
    ntp = next(rule for rule in result.data.rules if rule.application == "ntp")
    assert ntp.mode == "one-way" and len(ntp.receiver_ids) == 3
    exported = result_document(request, result, session_id="synthetic-unknown")
    control = next(rule for rule in exported["rules"] if rule["protocol"] == "Modbus TCP")
    assert control["port"] is None
