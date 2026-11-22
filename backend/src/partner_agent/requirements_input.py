import re
from typing import Any

from partner_agent.sinoniks_contract19 import Requirements19

_OFFICE_DEPARTMENTS = re.compile(
    r"\b(?:отдел\w*|подразделени\w*)\s+в\s+(?:одной\s+)?офисной\s+сети\b", re.I,
)
_LOG_TRANSFER = re.compile(
    r"(?:нужно\s+)?передавать\s+журналы\s+событий\s+из\s+закрытого\s+цеха\s+"
    r"в\s+офис\s+по\s+udp\s+(?:на\s+порт\s+)?(\d{1,5})[,\s]+"
    r"(?:в\s+одном\s+направлении|однонаправленно|только\s+наружу)",
    re.I,
)
_SHARED_CHECKS = re.compile(r"\bпроверки\s+одинаковые\s*(?=[,.!?]|$)", re.I)


def declared_log_transfer(question: str, project_id: str) -> Requirements19 | None:
    statement = re.split(r"[.!?]", question.strip(), maxsplit=1)[0]
    match = _LOG_TRANSFER.match(statement)
    if match is None or not 1 <= int(match[1]) <= 65535:
        return None
    tail = question.strip()[match.end():].strip(" .!")
    if tail and not re.fullmatch(
        r"где разместить синоникс и сколько правил потребуется\?", tail, re.I,
    ):
        return None
    return Requirements19.model_validate({
        "id": project_id,
        "networks": [
            {"id": "workshop", "name": "Закрытый цех", "trust": "closed"},
            {"id": "office", "name": "Офис", "trust": None},
        ],
        "flows": [{"id": "logs", "kind": "data", "mode": "one-way", "protocol": "UDP",
                   "destination_port": int(match[1]), "purpose": "Передача журналов событий",
                   "sender_network_id": "workshop", "receiver_network_id": "office"}],
    })


def bind_usb_source(document: dict[str, Any]) -> None:
    networks = document.get("networks")
    systems = document.get("systems")
    flows = document.get("flows")
    if (not isinstance(networks, list) or len(networks) != 2
            or not all(isinstance(network, dict) for network in networks)
            or not isinstance(systems, list) or not isinstance(flows, list)):
        return
    attachments = {system.get("id") for system in systems if isinstance(system, dict)
                   and isinstance(system.get("id"), str) and system.get("kind") == "usb"
                   and system.get("attachment") == "node-A" and system.get("network_id") is None}
    ids = {network["id"] for network in networks if isinstance(network.get("id"), str)}
    closed = [network.get("id") for network in networks if network.get("trust") == "closed"]
    protected = document.get("protected_network_id") or (closed[0] if len(closed) == 1 else None)
    if len(ids) != 2 or not isinstance(protected, str) or protected not in ids:
        return
    outside, = ids - {protected}
    used_counts = [flow for flow in flows if isinstance(flow, dict) and "manual_users" in flow]
    known_paths = set()
    for index, flow in enumerate(flows):
        if not isinstance(flow, dict) or flow.get("kind") != "file":
            continue
        senders = flow.get("sender_ids")
        sides = flow.get("sides")
        if (flow.get("direction") != "forward" or flow.get("receiver_network_id") != protected
                or flow.get("sender_network_id") not in (None, outside)
                or not isinstance(senders, list)
                or not any(isinstance(item, str) and item in attachments for item in senders)
                or not isinstance(sides, list) or len(sides) != 2
                or not all(isinstance(side, dict) for side in sides)):
            continue
        source = [side for side in sides if side.get("usb") is True
                  and side.get("network_id") in (None, outside)]
        destination = [side for side in sides if side.get("network_id") == protected]
        if len(source) != 1 or len(destination) != 1:
            continue
        side_index = next(n for n, side in enumerate(sides) if side is source[0])
        flow["sender_network_id"] = outside
        source[0]["network_id"] = outside
        known_paths.update({
            f"/flows/{index}/sender_network_id", f"/flows/{index}/sides/{side_index}/network_id",
        })
        if len(used_counts) == 1 and used_counts[0] is flow and all(
            isinstance(other, dict) and (other is flow or other.get("kind") == "data")
            for other in flows
        ) and document.get("manual_users") in (None, flow["manual_users"]):
            document["manual_users"] = flow.pop("manual_users")
            known_paths.add("/manual_users")
    if known_paths and isinstance(document.get("unknowns"), list):
        document["unknowns"] = [note for note in document["unknowns"] if not (
            isinstance(note, dict) and note.get("reason") == "missing"
            and isinstance(note.get("path"), str) and note["path"] in known_paths
        )]


def bind_office_departments(document: dict[str, Any], question: str) -> None:
    if not _OFFICE_DEPARTMENTS.search(question):
        return
    networks, flows = document.get("networks"), document.get("flows")
    systems = document.get("systems", [])
    if (not isinstance(networks, list) or len(networks) != 3
            or not all(isinstance(network, dict) and isinstance(network.get("id"), str)
                       for network in networks)
            or not isinstance(flows, list) or len(flows) != 2
            or not all(isinstance(flow, dict) and flow.get("kind") == "file"
                       and flow.get("direction") == "forward" for flow in flows)
            or not isinstance(systems, list)):
        return
    for field in ("unknowns", "assumptions"):
        notes = document.get(field, [])
        if not isinstance(notes, list) or any(
            not isinstance(note, dict) or not isinstance(note.get("path"), str)
            or note["path"].startswith("/networks/") for note in notes
        ):
            return
    protected = [network for network in networks if network.get("trust") == "closed"]
    offices = [network for network in networks if network.get("trust") is None]
    if len(protected) != 1 or len(offices) != 2:
        return
    if not all(isinstance(network.get("name"), str)
               and network["name"].casefold() in question.casefold() for network in offices):
        return
    target = protected[0]["id"]
    old_ids = {network["id"] for network in offices}
    if document.get("protected_network_id") not in (None, target):
        return
    if {flow.get("sender_network_id") for flow in flows
        if isinstance(flow.get("sender_network_id"), str)} != old_ids:
        return
    groups = []
    for flow in flows:
        sides = flow.get("sides")
        if (flow.get("receiver_network_id") != target or not isinstance(sides, list)
                or len(sides) != 2 or not all(isinstance(side, dict) for side in sides)):
            return
        if not any(side.get("network_id") == flow["sender_network_id"] and
                   side.get("manual") is True and side.get("manual_protocol") == "SFTP"
                   for side in sides):
            return
        receiver: dict[str, Any] = next(
            (side for side in sides if side.get("network_id") == target), {},
        )
        if receiver.get("received_files_shared") is not False:
            return
        group = receiver.get("access_group")
        if not isinstance(group, str):
            return
        groups.append(group)
    if len(set(groups)) != 2:
        return
    office = offices[0]["id"]
    document["networks"] = [{"id": office, "name": "Офисная сеть", "trust": None}, protected[0]]
    for system in systems:
        if (isinstance(system, dict) and isinstance(system.get("network_id"), str)
                and system["network_id"] in old_ids):
            system["network_id"] = office
    for flow in flows:
        flow["sender_network_id"] = office
        for side in flow["sides"]:
            if isinstance(side.get("network_id"), str) and side["network_id"] in old_ids:
                side["network_id"] = office


def bind_shared_file_checks(document: dict[str, Any], question: str) -> None:
    if not _SHARED_CHECKS.search(question):
        return
    flows = document.get("flows")
    if not isinstance(flows, list) or len(flows) not in (1, 2):
        return
    sides = []
    for flow in flows:
        if not isinstance(flow, dict) or flow.get("kind") != "file":
            return
        flow_sides = flow.get("sides")
        if not isinstance(flow_sides, list) or len(flow_sides) != 2:
            return
        for side in flow_sides:
            if not isinstance(side, dict) or side.get("checks") is not None:
                return
            automations = side.get("automations")
            if (side.get("manual") is not False or side.get("usb") is not False
                    or not isinstance(automations, list) or not automations
                    or not all(isinstance(setting, dict) and setting.get("protocol") == "SFTP"
                               for setting in automations)):
                return
            sides.append(side)
    if any("checks_group" in side for side in sides):
        return
    for side in sides:
        side["checks_group"] = "shared-file-checks"
