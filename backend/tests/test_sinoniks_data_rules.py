from partner_agent.sinoniks_contract19 import DataFlow19, Network19, Requirements19, System19
from partner_agent.sinoniks_data_rules import calculate_data_rules


def flow(identifier, sender, receiver, **changes):
    return DataFlow19.model_validate({
        "id": identifier, "kind": "data", "sender_network_id": "plant",
        "receiver_network_id": "office", "sender_ids": sender, "receiver_ids": receiver,
        "mode": "one-way", "protocol": "UDP", "destination_port": 514,
        "requirement_ids": [f"requirement-{identifier}"], **changes,
    })


def request(*flows):
    return Requirements19(
        id="project", networks=[
            Network19(id="plant", trust="closed", name="Цех"),
            Network19(id="office", trust="open", name="Офис"),
        ], systems=[
            System19(id="client-1", network_id="plant"),
            System19(id="client-2", network_id="plant"),
            System19(id="logs", network_id="office"),
            System19(id="backup", network_id="office"),
        ], flows=list(flows),
    )


def test_rules_preserve_shared_receivers_coverage_and_explicit_differences():
    requirements = request(
        flow("logs-1", ["client-1"], ["logs", "backup"]),
        flow("logs-2", ["client-2"], ["logs"]),
        flow("alt-port", ["client-2"], ["logs"], destination_port=515),
        flow("alt-policy", ["client-2"], ["logs"], policy="Другой профиль"),
    )
    calculated = calculate_data_rules(requirements)
    assert calculated.network_minimum == 4
    rule = next(item for item in calculated.rules if len(item.flow_ids) == 2)
    assert rule.sender_ids == {"client-1", "client-2"}
    assert rule.receiver_ids == {"logs"}
    assert rule.requirement_ids == {"requirement-logs-1", "requirement-logs-2"}
    assert rule.as_dict()["collect"] == {
        "network_id": "plant", "system_ids": ["client-1", "client-2"],
    }
    requirements.flows.reverse()
    assert [rule.as_dict() for rule in calculate_data_rules(requirements).rules] == [
        rule.as_dict() for rule in calculated.rules
    ]


def test_time_sync_is_one_rule_and_keeps_unknown_port_without_invented_receivers():
    to_plant = {"sender_network_id": "office", "receiver_network_id": "plant"}
    calculated = calculate_data_rules(request(
        flow("logs-stream", ["client-1"], ["logs"]),
        flow("modbus", ["backup"], ["client-1"], **to_plant, mode=None,
             protocol="Modbus TCP", destination_port=502),
        flow("time", ["backup"], [], **to_plant, application="ntp",
             mode=None, protocol=None, destination_port=None),
    ))
    assert calculated.network_minimum == 3
    time = next(rule for rule in calculated.rules if rule.application == "ntp")
    assert (time.mode, time.protocol, time.port, time.receiver_ids) == (
        "one-way", "UDP", None, set(),
    )
    assert calculated.placement is not None
    assert calculated.placement.node_a_network_id == "office"
    assert calculated.placement.node_b_network_id == "plant"


def test_unresolved_compatibility_and_conflicting_direction_are_exposed():
    calculated = calculate_data_rules(request(
        flow("unknown-port", ["client-1"], ["logs"], destination_port=None),
        flow("known-port", ["client-2"], ["logs"]),
        flow("reverse-time", ["client-2"], [], application="ntp"),
        flow("forbid-reply", ["client-2"], ["backup"], protocol="MQTT"),
    ))
    assert len(calculated.rules) == 2
    assert calculated.confirmed_network_lower_bound == 1
    assert calculated.network_minimum is None
    assert {issue.reason for issue in calculated.issues} == {
        "unknown_protocol_or_port_prevents_grouping",
        "ntp_requires_protected_destination",
        "industrial_protocol_requires_two_way",
    }


def test_two_open_networks_require_an_explicit_protected_side_and_preserve_trust():
    requirements = request(flow("logs-stream", ["client-1"], ["logs"]))
    requirements.networks[0].trust = "open"
    pending = calculate_data_rules(requirements)
    assert pending.placement is None and pending.network_minimum is None
    calculated = calculate_data_rules(requirements, protected_network_id="plant")
    assert calculated.placement is not None
    assert calculated.placement.assumed_protected_side
    assert calculated.network_minimum == 1
    assert all(network.trust == "open" for network in requirements.networks)


def test_confirmed_network_rules_above_capacity_are_retained_for_review():
    receivers = [f"server-{index}" for index in range(129)]
    requirements = Requirements19.model_validate({
        **request().model_dump(exclude={"systems", "flows"}),
        "systems": [System19(id="client-1", network_id="plant"),
                    *(System19(id=target, network_id="office") for target in receivers)],
        "flows": [
            flow("a", ["client-1"], receivers),
            flow("b", ["client-1"], receivers, destination_port=515),
        ],
    })
    calculated = calculate_data_rules(requirements)
    assert calculated.network_minimum == len(calculated.rules) == 258
    assert any(issue.reason == "single_appliance_capacity_exceeded"
               for issue in calculated.issues)
