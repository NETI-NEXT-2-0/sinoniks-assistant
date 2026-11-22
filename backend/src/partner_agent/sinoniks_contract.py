"""Versioned input and output for a two-contour Sinoniks design."""

import unicodedata
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,80}$")]
Side = Literal["A", "B"]
Mode = Literal["diode", "transport", "filetransfer"]
Protocol = Literal["TCP", "UDP", "MQTT", "Modbus TCP", "МЭК 104"]
INDUSTRIAL_PROTOCOLS = frozenset({"MQTT", "Modbus TCP", "МЭК 104"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceReference(StrictModel):
    kind: Literal["message", "document", "operator"]
    reference: str = Field(min_length=1, max_length=200)
    revision: int = Field(default=1, ge=1)

    @field_validator("reference")
    @classmethod
    def supported_reference(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Source reference must not contain a zero character")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Source reference must be valid UTF-8") from None
        return value


class Exchange(StrictModel):
    requirement_id: Identifier | None = None
    sender_side: Side | None = None
    sender: Label | None = None
    receiver: Label | None = None
    mode: Mode | None = None
    service: Label | None = None
    policy: Label | None = None
    protocol: Protocol | None = None
    destination_port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    sources: list[SourceReference] = Field(default_factory=list, max_length=20)

    @field_validator("mode", mode="before")
    @classmethod
    def legacy_mode(cls, value: object) -> object:
        # Version 1 used this name for the bidirectional transport session mode.
        return "transport" if value == "default" else value

    @field_validator("sender", "receiver", "service", "policy")
    @classmethod
    def readable_label(cls, value: str | None) -> str | None:
        if value is not None and any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Use printable single-line labels")
        return value

    @model_validator(mode="after")
    def compatible_transport(self) -> "Exchange":
        if self.mode == "filetransfer" and (
            self.protocol is not None or self.destination_port is not None
        ):
            raise ValueError("Transport protocol/port belong to network exchanges")
        if self.mode is None and self.protocol in INDUSTRIAL_PROTOCOLS:
            self.mode = "transport"
        return self


class DesignInput(StrictModel):
    schema_version: Literal[1, 2] = 2
    network_a: Label | None = None
    network_b: Label | None = None
    firewall: Literal["A", "B", "both"] | None = None
    exchanges: list[Exchange] = Field(default_factory=list, max_length=24)

    @field_validator("network_a", "network_b")
    @classmethod
    def printable_network(cls, value: str | None) -> str | None:
        return Exchange.readable_label(value)

    @model_validator(mode="after")
    def distinct_requirement_ids(self) -> "DesignInput":
        identifiers = [
            item.requirement_id or f"requirement-{index + 1}"
            for index, item in enumerate(self.exchanges)
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Requirement IDs must be unique")
        return self


class Question(StrictModel):
    field: str
    text: str
    reason: Literal["missing", "conflict"] = "missing"


class Rule(Exchange):
    id: Identifier
    requirement_ids: list[Identifier] = Field(min_length=1, max_length=24)
    sources: list[SourceReference] = Field(default_factory=list, max_length=480)
    sender_side: Side
    sender: Label
    additional_senders: list[Label] = Field(
        default_factory=list, max_length=23, exclude_if=lambda value: not value,
    )
    receiver: Label
    mode: Mode
    service: Label
    policy: Label

    @property
    def senders(self) -> list[str]:
        return [self.sender, *self.additional_senders]

    @field_validator("additional_senders")
    @classmethod
    def readable_senders(cls, values: list[str]) -> list[str]:
        for value in values:
            Exchange.readable_label(value)
        return values

    @model_validator(mode="after")
    def valid_shared_receiver(self) -> "Rule":
        if self.protocol in INDUSTRIAL_PROTOCOLS and self.mode != "transport":
            raise ValueError("Industrial protocols require bidirectional transport")
        if self.additional_senders and (
            self.mode == "filetransfer" or self.protocol is None or self.destination_port is None
            or len(self.senders) != len(set(self.senders))
            or len(self.senders) > len(self.requirement_ids)
        ):
            raise ValueError("Grouped senders require a shared known network destination")
        return self


class Network(StrictModel):
    id: str
    side: Side
    name: str


class Device(StrictModel):
    id: str
    kind: Literal["sinoniks_node", "sinoniks_core", "firewall"]
    name: str
    side: Side | None = None
    basis: Literal["product_architecture", "explicit_input"]


class Connection(StrictModel):
    id: str
    source: str
    target: str
    kind: Literal["network_link", "isolated_internal_transfer"]


class Counts(StrictModel):
    input_requirements: int
    logical_exchanges: int
    network_slot_pairs: int
    network_node_settings: int
    file_transfer_policies: int
    evaluation_rule_count: int | None = None
    counting_policy: Literal["exact-exchanges-v2", "shared-destination-v2"] = "exact-exchanges-v2"
    minimality: Literal["exact_duplicates_removed", "compatible_network_senders_grouped"] = (
        "exact_duplicates_removed"
    )


class Design(StrictModel):
    schema_version: Literal[2] = 2
    profile: Literal["two-contours-draft-v2"] = "two-contours-draft-v2"
    id: str
    status: Literal["conceptual"] = "conceptual"
    networks: dict[Side, str]
    network_nodes: list[Network]
    firewall_sides: list[Side]
    devices: list[str]
    device_nodes: list[Device]
    connections: list[Connection]
    rules: list[Rule]
    logical_rule_count: int
    counts: Counts
    warnings: list[str]
    explanation: str

    @model_validator(mode="after")
    def valid_graph(self) -> "Design":
        network_map = {node.id: node.name for node in self.network_nodes}
        if (
            len(self.network_nodes) != 2 or set(network_map) != {"network-A", "network-B"}
            or network_map["network-A"] != self.networks.get("A")
            or network_map["network-B"] != self.networks.get("B")
            or any(node.id != f"network-{node.side}" for node in self.network_nodes)
        ):
            raise ValueError("Network identifiers, sides and names must agree")
        expected_roles: dict[str, tuple[str, str | None]] = {
            "node-A": ("sinoniks_node", "A"), "node-B": ("sinoniks_node", "B"),
            "core": ("sinoniks_core", None),
        }
        expected_roles.update({
            f"firewall-{side}": ("firewall", side) for side in self.firewall_sides
        })
        roles = {device.id: (device.kind, device.side) for device in self.device_nodes}
        if roles != expected_roles or len(set(self.firewall_sides)) != len(self.firewall_sides):
            raise ValueError("Devices must match the two-contour product architecture")
        ids = [node.id for node in self.network_nodes] + [node.id for node in self.device_nodes]
        if len(ids) != len(set(ids)) or not self.connections:
            raise ValueError("Invalid topology identifiers")
        known = set(ids)
        chain = [self.connections[0].source]
        for edge in self.connections:
            if edge.source != chain[-1] or edge.target not in known:
                raise ValueError("Topology connections must form a complete ordered path")
            internal = "core" in (edge.source, edge.target)
            if internal != (edge.kind == "isolated_internal_transfer"):
                raise ValueError("Core transfer must remain isolated from network links")
            chain.append(edge.target)
        if chain[0] != "network-A" or chain[-1] != "network-B" or set(chain) != known:
            raise ValueError("The topology must connect both networks through all devices")
        if len(chain) != len(known) or (
            len({edge.id for edge in self.connections}) != len(self.connections)
        ):
            raise ValueError("Duplicate topology connection")
        if self.logical_rule_count != len(self.rules) or (
            self.counts.logical_exchanges != len(self.rules)
        ):
            raise ValueError("Rule count differs from the rule list")
        expected_chain = ["network-A"]
        if "A" in self.firewall_sides:
            expected_chain.append("firewall-A")
        expected_chain.extend(["node-A", "core", "node-B"])
        if "B" in self.firewall_sides:
            expected_chain.append("firewall-B")
        expected_chain.append("network-B")
        if chain != expected_chain:
            raise ValueError("The topology must keep each Sinoniks node on its own side")
        covered = [identifier for rule in self.rules for identifier in rule.requirement_ids]
        if len(covered) != len(set(covered)) or len(covered) != self.counts.input_requirements:
            raise ValueError("Every requirement must be covered exactly once")
        network_count = sum(rule.mode != "filetransfer" for rule in self.rules)
        if (
            len({rule.id for rule in self.rules}) != len(self.rules)
            or self.counts.network_slot_pairs != network_count
            or self.counts.network_node_settings != 2 * network_count
            or self.counts.file_transfer_policies != len(self.rules) - network_count
        ):
            raise ValueError("Rule IDs and product counters must match the saved rules")
        return self
