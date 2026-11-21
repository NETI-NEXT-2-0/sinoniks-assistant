"""Explicit requirements for Sinoniks 1.9; distinct from a solved design.

Version 3 describes user requirements, not the organizer's result JSON 1.1.
No trust, protocol, port, file method or rule minimum is inferred here.
"""

import re
from collections.abc import Container
from typing import Annotated, Any, Literal, cast

from pydantic import (
    AfterValidator,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from partner_agent.sinoniks_contract import Identifier, Protocol, SourceReference, StrictModel


def _printable(value: str) -> str:
    import unicodedata

    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("Use printable single-line text")
    return value


Text19 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
                   AfterValidator(_printable)]
Name19 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
                   AfterValidator(_printable)]
Port19 = Annotated[int, Field(strict=True, ge=1, le=65535)]
NOTE_PATH = (r"^/(protected_network_id|"
             r"(networks|systems|flows|manual_users)(/[^/]+)*)$")


class Network19(StrictModel):
    id: Identifier
    name: Name19 | None = None
    trust: Literal["open", "closed"] | None = None
    sources: list[SourceReference] = Field(default_factory=list, max_length=20)


class System19(StrictModel):
    id: Identifier
    network_id: Identifier | None = None
    name: Name19 | None = None
    kind: Literal["system", "storage", "usb"] = "system"
    # A USB medium attaches to node A; it is not an Ethernet endpoint.
    attachment: Literal["node-A"] | None = None
    sources: list[SourceReference] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def physical_attachment(self) -> "System19":
        if self.kind == "usb":
            if self.network_id is not None or self.attachment != "node-A":
                raise ValueError("USB must attach directly to node-A, without a network link")
        elif self.attachment is not None:
            raise ValueError("Only USB media have a direct node attachment")
        return self


class Actor19(StrictModel):
    id: Identifier
    name: Name19 | None = None

    def is_collective(self) -> bool:
        return bool(re.match(
            r"^(?:отдел|подразделение|группа|команда|департамент|"
            r"department|division|group|team)\b",
            (self.name or "").strip(),
            re.IGNORECASE,
        ))


class CountRange19(StrictModel):
    lower: int = Field(ge=0, strict=True)
    upper: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def ordered(self) -> "CountRange19":
        if self.upper is not None and self.upper < self.lower:
            raise ValueError("Range upper bound precedes its lower bound")
        return self


class FileCheck19(StrictModel):
    kind: Literal["name_mask", "type_extension", "size", "signature", "icap", "unsupported"]
    details: Text19 | None = None
    icap_service: Literal["antivirus", "sandbox", "dlp"] | None = None

    @model_validator(mode="after")
    def external_scanner(self) -> "FileCheck19":
        if self.icap_service is not None and self.kind != "icap":
            raise ValueError("Antivirus, sandbox and DLP are external ICAP services")
        if self.kind == "unsupported" and self.details is None:
            raise ValueError("Preserve the unsupported request in details")
        return self


class Automatic19(StrictModel):
    operation: Literal["PULL", "PUSH"]
    protocol: Literal["SFTP", "FTP", "SMB"] | None = None
    storage_id: Identifier | None = None


class FileSide19(StrictModel):
    network_id: Identifier
    manual: bool | None = Field(default=None, strict=True)
    usb: bool | None = Field(default=None, strict=True)
    manual_protocol: Literal["SFTP"] | None = None
    actor_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    # Requirements may demand both PULL and PUSH; the solver must split rules.
    automations: list[Automatic19] = Field(default_factory=list, max_length=8)
    # null = not supplied; [] = explicitly no checks.
    checks: list[FileCheck19] | None = Field(default=None, max_length=20)
    received_files_shared: bool | None = Field(default=None, strict=True)
    access_group: Identifier | None = None


class FlowBase19(StrictModel):
    id: Identifier
    sender_network_id: Identifier | None = None
    receiver_network_id: Identifier | None = None
    sender_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    receiver_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    requirement_ids: list[Identifier] = Field(default_factory=list, max_length=256)
    purpose: Text19 | None = None
    policy: Text19 | None = None
    sources: list[SourceReference] = Field(default_factory=list, max_length=20)


class DataFlow19(FlowBase19):
    kind: Literal["data"]
    mode: Literal["one-way", "two-way"] | None = None
    protocol: Protocol | None = None
    destination_port: Port19 | None = None
    application: Literal["generic", "ntp"] = "generic"


class FileFlow19(FlowBase19):
    kind: Literal["file"]
    direction: Literal["forward", "both"] | None = None
    sides: list[FileSide19] = Field(default_factory=list, max_length=2)

    def has_declared_checks(self) -> bool:
        return any(check.kind != "unsupported"
                   for side in self.sides for check in side.checks or [])


Flow19 = Annotated[DataFlow19 | FileFlow19, Field(discriminator="kind")]


class Unknown19(StrictModel):
    path: str = Field(pattern=NOTE_PATH, max_length=300)
    reason: Literal["missing", "user_unknown"]
    target_id: Identifier | None = None


class Assumption19(StrictModel):
    path: str = Field(pattern=NOTE_PATH, max_length=300)
    target_id: Identifier | None = None
    explanation: Text19
    basis: Literal["product_fact", "user_minimum"]


class VendorRequest19(StrictModel):
    id: Identifier
    text: Text19
    kind: Literal["unsupported", "free_text"]
    flow_id: Identifier | None = None


class LegacyContext19(StrictModel):
    source_version: Literal[1, 2]
    firewall: Literal["A", "B", "both"] | None = None


def pointer_value(document: object, path: str) -> object:
    """Resolve RFC 6901 paths, rejecting stale notes instead of silently ignoring them."""
    if not path.startswith("/"):
        raise ValueError("Expected a JSON pointer")
    current = document
    for raw in path[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and key in current:
            current = current[key]
        elif isinstance(current, list) and key.isascii() and key.isdecimal():
            index = int(key)
            if str(index) != key or index >= len(current):
                raise ValueError("Unknown JSON pointer")
            current = current[index]
        else:
            raise ValueError("Unknown JSON pointer")
    return current


class Requirements19(StrictModel):
    schema_version: Literal[3] = 3
    product_version: Literal["1.9"] = "1.9"
    id: Identifier
    networks: list[Network19] = Field(default_factory=list, max_length=2)
    systems: list[System19] = Field(default_factory=list, max_length=256)
    flows: list[Flow19] = Field(default_factory=list, max_length=256)
    actors: list[Actor19] = Field(default_factory=list, max_length=256)
    manual_users: CountRange19 | None = None
    unknowns: list[Unknown19] = Field(default_factory=list, max_length=1024)
    assumptions: list[Assumption19] = Field(default_factory=list, max_length=256)
    requests_for_vendor: list[VendorRequest19] = Field(default_factory=list, max_length=256)
    legacy: LegacyContext19 | None = None
    protected_network_id: Identifier | None = None
    existing_firewall_name: Name19 | None = None

    @model_serializer(mode="wrap")
    def compatible_dump(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        document = cast(dict[str, Any], handler(self))
        notes: list[Unknown19 | Assumption19] = [*self.unknowns, *self.assumptions]
        # Preserve old command hashes. A new root note must still point to a serialized value.
        if self.protected_network_id is None and not any(
            note.path == "/protected_network_id" for note in notes
        ):
            document.pop("protected_network_id", None)
        if self.existing_firewall_name is None:
            document.pop("existing_firewall_name", None)
        return document

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Schema version must be an integer")
        return value

    @model_validator(mode="after")
    def references(self) -> "Requirements19":
        objects = [*self.networks, *self.systems, *self.flows, *self.actors,
                   *self.requests_for_vendor]
        ids = [item.id for item in objects]
        if len(ids) != len(set(ids)):
            raise ValueError("Stable identifiers must be globally unique")
        networks = {item.id for item in self.networks}
        systems = {item.id: item for item in self.systems}
        actors = {item.id: item for item in self.actors}

        def reference(value: str | None, known: Container[str]) -> None:
            if value is not None and value not in known:
                raise ValueError(f"Unknown reference: {value}")

        def unique(values: list[str]) -> None:
            if len(values) != len(set(values)):
                raise ValueError("Duplicate reference")

        manual_actors: set[str] = set()
        reference(self.protected_network_id, networks)
        for system in self.systems:
            reference(system.network_id, networks)
        for flow in self.flows:
            reference(flow.sender_network_id, networks)
            reference(flow.receiver_network_id, networks)
            if flow.sender_network_id is not None and (
                flow.sender_network_id == flow.receiver_network_id
            ):
                raise ValueError("An exchange must cross the network boundary")
            for refs, network in ((flow.sender_ids, flow.sender_network_id),
                                  (flow.receiver_ids, flow.receiver_network_id)):
                unique(refs)
                for system_id in refs:
                    reference(system_id, systems)
                    system = systems[system_id]
                    if system.kind == "usb":
                        if flow.kind != "file":
                            raise ValueError("USB cannot be a data-stream endpoint")
                    elif network is not None and system.network_id is not None and (
                        system.network_id != network
                    ):
                        raise ValueError("Endpoint is assigned to the wrong network")
            if isinstance(flow, FileFlow19):
                unique([side.network_id for side in flow.sides])
                for side in flow.sides:
                    reference(side.network_id, networks)
                    unique(side.actor_ids)
                    if side.manual is False and side.usb is False and side.actor_ids:
                        raise ValueError("Manual actors require a manual or USB transfer")
                    if side.manual is False and side.manual_protocol is not None:
                        raise ValueError("Manual protocol conflicts with disabled manual transfer")
                    for actor in side.actor_ids:
                        reference(actor, actors)
                        if not actors[actor].is_collective():
                            manual_actors.add(actor)
                    for automation in side.automations:
                        reference(automation.storage_id, systems)
                        if automation.storage_id is not None:
                            storage = systems[automation.storage_id]
                            if storage.kind != "storage" or (
                                storage.network_id is not None
                                and storage.network_id != side.network_id
                            ):
                                raise ValueError("Automation requires storage on its own side")
        if self.manual_users is not None and self.manual_users.upper is not None and (
            self.manual_users.upper < len(manual_actors)
        ):
            raise ValueError("User count cannot be smaller than the distinct known actors")
        for request in self.requests_for_vendor:
            reference(request.flow_id, {flow.id for flow in self.flows})
        paths = [item.path for item in self.unknowns]
        if len(paths) != len(set(paths)):
            raise ValueError("Duplicate unknown path")
        document = self.model_dump(mode="json")
        notes: list[Unknown19 | Assumption19] = [*self.unknowns, *self.assumptions]
        for note in notes:
            segments = note.path.split("/")
            if len(segments) >= 3 and segments[1] in {"networks", "systems", "flows"}:
                owner = pointer_value(document, "/".join(segments[:3]))
                if not isinstance(owner, dict) or owner.get("id") != note.target_id:
                    raise ValueError("Note target ID must match the entity at its path")
            elif note.target_id is not None:
                raise ValueError("A root note has no target entity")
        for note in self.unknowns:
            if pointer_value(document, note.path) not in (None, []):
                raise ValueError("An unknown note cannot hide a supplied value")
        for assumption in self.assumptions:
            pointer_value(document, assumption.path)
        return self
