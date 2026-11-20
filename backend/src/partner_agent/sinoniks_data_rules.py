import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

from partner_agent.sinoniks_contract import INDUSTRIAL_PROTOCOLS, Protocol, SourceReference
from partner_agent.sinoniks_contract19 import DataFlow19, Requirements19

RULE_LIMIT = 256
Mode = Literal["one-way", "two-way"]


@dataclass(frozen=True)
class DataIssue:
    field: str
    reason: str
    flow_id: str | None = None


@dataclass(frozen=True)
class DataPlacement:
    protected_network_id: str
    node_a_network_id: str
    node_b_network_id: str
    recommended_firewall_side: Literal["open"] = "open"
    assumed_protected_side: bool = False


@dataclass
class DataRule:
    id: str
    application: Literal["generic", "ntp"]
    from_network_id: str
    to_network_id: str
    mode: Mode
    protocol: Protocol | None
    port: int | None
    policy: str | None
    sender_ids: set[str] = field(default_factory=set)
    receiver_ids: set[str] = field(default_factory=set)
    flow_ids: set[str] = field(default_factory=set)
    requirement_ids: set[str] = field(default_factory=set)
    purposes: set[str] = field(default_factory=set)
    sources: dict[tuple[str, str, int], SourceReference] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id, "application": self.application, "mode": self.mode,
            "from_network_id": self.from_network_id, "to_network_id": self.to_network_id,
            "protocol": self.protocol, "port": self.port, "policy": self.policy,
            "collect": {"network_id": self.from_network_id, "system_ids": sorted(self.sender_ids)},
            "exchange": {"network_id": self.to_network_id, "system_ids": sorted(self.receiver_ids)},
            "flow_ids": sorted(self.flow_ids), "requirement_ids": sorted(self.requirement_ids),
            "purposes": sorted(self.purposes),
            "sources": [self.sources[key].model_dump(mode="json") for key in sorted(self.sources)],
        }


@dataclass(frozen=True)
class DataCalculation:
    placement: DataPlacement | None
    rules: list[DataRule]
    issues: list[DataIssue]
    file_flow_ids: list[str]
    network_minimum: int | None
    confirmed_network_lower_bound: int


def place_data_exchange(
    request: Requirements19, protected_network_id: str | None,
) -> DataPlacement | None:
    if len(request.networks) != 2:
        return None
    networks = {network.id: network.trust for network in request.networks}
    closed = [key for key, trust in networks.items() if trust == "closed"]
    assumed = all(trust == "open" for trust in networks.values())
    if assumed and protected_network_id in networks:
        protected = protected_network_id
    elif len(closed) == 1 and "open" in networks.values() and protected_network_id in (
        None, closed[0],
    ):
        protected = closed[0]
    else:
        return None
    assert protected is not None
    outer = next(key for key in networks if key != protected)
    return DataPlacement(protected, outer, protected, assumed_protected_side=assumed)


def data_flow_issue(flow: DataFlow19, placement: DataPlacement | None) -> DataIssue | None:
    if flow.sender_network_id is None or flow.receiver_network_id is None:
        return DataIssue("direction", "network_direction_unknown", flow.id)
    if not flow.sender_ids or (not flow.receiver_ids and flow.application != "ntp"):
        return DataIssue("endpoints", "systems_unknown", flow.id)
    if flow.application == "ntp":
        if placement is None or flow.receiver_network_id != placement.protected_network_id:
            return DataIssue("direction", "ntp_requires_protected_destination", flow.id)
        if len(flow.sender_ids) != 1:
            return DataIssue("sender_ids", "ntp_requires_one_time_source", flow.id)
        if flow.mode == "two-way" or flow.protocol not in (None, "UDP"):
            return DataIssue("mode", "ntp_requires_one_way_udp", flow.id)
    elif flow.mode == "one-way" and flow.protocol in INDUSTRIAL_PROTOCOLS:
        return DataIssue("mode", "industrial_protocol_requires_two_way", flow.id)
    elif flow.mode is None and flow.protocol not in INDUSTRIAL_PROTOCOLS:
        return DataIssue("mode", "return_traffic_unknown", flow.id)
    return None


def calculate_data_rules(
    request: Requirements19, *, protected_network_id: str | None = None,
) -> DataCalculation:
    placement = place_data_exchange(request, protected_network_id)
    issues = [] if placement else [DataIssue("networks", "protected_side_unresolved")]
    if not request.flows:
        issues.append(DataIssue("flows", "flows_unknown"))
    grouped: dict[tuple[object, ...], DataRule] = {}
    coarse_groups: dict[tuple[object, ...], list[bool]] = {}
    certain: dict[tuple[object, ...], set[tuple[Protocol, int]]] = {}
    file_flow_ids = []
    unassigned = {system.id for system in request.systems if system.network_id is None}

    for flow in request.flows:
        if not isinstance(flow, DataFlow19):
            file_flow_ids.append(flow.id)
            continue
        issue = data_flow_issue(flow, placement)
        if issue is not None:
            issues.append(issue)
            continue
        if not unassigned.isdisjoint((*flow.sender_ids, *flow.receiver_ids)):
            issues.append(DataIssue("endpoints", "system_network_unknown", flow.id))
            continue
        assert flow.sender_network_id is not None and flow.receiver_network_id is not None
        ntp = flow.application == "ntp"
        mode: Mode = "one-way" if ntp else flow.mode or "two-way"
        protocol: Protocol | None = "UDP" if ntp else flow.protocol
        if protocol is None:
            issues.append(DataIssue("protocol", "protocol_unknown", flow.id))
        targets = [None] if ntp else flow.receiver_ids
        for target in targets:
            endpoints = tuple(sorted(flow.sender_ids)) if ntp else (target,)
            group = (flow.application, flow.sender_network_id, flow.receiver_network_id,
                     endpoints, mode, flow.policy)
            known_port = protocol is not None and flow.destination_port is not None
            key = (*group, protocol, flow.destination_port, None if known_port else flow.id)
            coarse_groups.setdefault(group, []).append(known_port)
            if protocol is not None and flow.destination_port is not None:
                certain.setdefault(group, set()).add((protocol, flow.destination_port))
            if key not in grouped:
                identity = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
                identifier = hashlib.sha256(identity.encode()).hexdigest()[:24]
                grouped[key] = DataRule(
                    f"data-{identifier}", flow.application, flow.sender_network_id,
                    flow.receiver_network_id, mode, protocol, flow.destination_port, flow.policy,
                )
            rule = grouped[key]
            rule.sender_ids.update(flow.sender_ids)
            rule.receiver_ids.update(flow.receiver_ids if target is None else [target])
            rule.flow_ids.add(flow.id)
            rule.requirement_ids.update(flow.requirement_ids)
            if flow.purpose is not None:
                rule.purposes.add(flow.purpose)
            for source in flow.sources:
                rule.sources[(source.kind, source.reference, source.revision)] = source

    ambiguous = any(len(states) > 1 and not all(states) for states in coarse_groups.values())
    if ambiguous:
        issues.append(DataIssue("rules", "unknown_protocol_or_port_prevents_grouping"))
    rules = sorted(grouped.values(), key=lambda rule: rule.id)
    lower_bound = sum(max(1, len(certain.get(key, ()))) for key in coarse_groups)
    minimum = len(rules) if not issues else None
    if lower_bound > RULE_LIMIT:
        issues.append(DataIssue("rules", "single_appliance_capacity_exceeded"))
    issues.sort(key=lambda issue: (issue.flow_id or "", issue.field, issue.reason))
    return DataCalculation(placement, rules, issues, sorted(file_flow_ids), minimum, lower_bound)
