"""Offline validation and explicit migration, without running the v2 solver."""

import argparse
import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from partner_agent.sinoniks_calculation import calculate_rules, solve_project
from partner_agent.sinoniks_contract import Design, DesignInput, StrictModel
from partner_agent.sinoniks_contract19 import (
    DataFlow19,
    FileFlow19,
    Flow19,
    LegacyContext19,
    Network19,
    Requirements19,
    System19,
)
from partner_agent.sinoniks_data_rules import calculate_data_rules
from partner_agent.sinoniks_export import scene_drawio
from partner_agent.sinoniks_file_rules import has_unidentified_manual_users
from partner_agent.sinoniks_layout19 import layout19
from partner_agent.sinoniks_result_export import export_result


def stable_id(prefix: str, *parts: str) -> str:
    identity = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return prefix + "-" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def upgrade_input(request: DesignInput, *, project_id: str) -> Requirements19:
    """A one-time migration. Afterwards IDs are persisted, never derived from names again."""
    networks = [Network19(id="network-A", name=request.network_a),
                Network19(id="network-B", name=request.network_b)]
    systems: dict[str, System19] = {}
    flows: list[Flow19] = []
    for index, exchange in enumerate(request.exchanges):
        sender_network = f"network-{exchange.sender_side}" if exchange.sender_side else None
        receiver_network = ("network-B" if exchange.sender_side == "A" else "network-A"
                            if exchange.sender_side == "B" else None)

        def endpoint(name: str | None, network: str | None, role: str) -> list[str]:
            if name is None:
                return []
            # Unknown side must not merge two distinct unknown systems by name.
            identity = network or f"unassigned-{index}-{role}"
            identifier = stable_id("system", identity, name)
            systems.setdefault(identifier, System19(id=identifier, name=name, network_id=network))
            return [identifier]

        values: dict[str, Any] = {
            "id": stable_id("flow", exchange.requirement_id or f"requirement-{index + 1}"),
            "requirement_ids": [exchange.requirement_id or f"requirement-{index + 1}"],
            "sender_network_id": sender_network, "receiver_network_id": receiver_network,
            "sender_ids": endpoint(exchange.sender, sender_network, "sender"),
            "receiver_ids": endpoint(exchange.receiver, receiver_network, "receiver"),
            "purpose": exchange.service, "policy": exchange.policy, "sources": exchange.sources,
        }
        if exchange.mode == "filetransfer":
            # The old file mode does not prove method/checks or bidirectional intent.
            flows.append(FileFlow19(kind="file", **values))
        elif exchange.mode is None:
            # Without a mode old v2 cannot distinguish incomplete data from files.
            raise ValueError(f"Flow {index + 1}: clarify data/file kind before migration")
        else:
            flows.append(DataFlow19(kind="data", mode=(
                "one-way" if exchange.mode == "diode" else "two-way"
            ), protocol=exchange.protocol, destination_port=exchange.destination_port, **values))
    return Requirements19(
        id=project_id, networks=networks, systems=list(systems.values()), flows=flows,
        legacy=LegacyContext19(source_version=request.schema_version, firewall=request.firewall),
    )


def read_document(value: object) -> Requirements19 | DesignInput | Design:
    """Dispatch versions explicitly; a damaged v3 must never fall back to a v2 reader."""
    if not isinstance(value, Mapping):
        raise ValueError("Expected a JSON object")
    version = value.get("schema_version", 2)
    if type(version) is not int:
        raise ValueError("Schema version must be an integer")
    if version == 3:
        return Requirements19.model_validate(value)
    if version in (1, 2):
        if "profile" in value:
            return Design.model_validate(value)
        return DesignInput.model_validate(value)
    raise ValueError("Unsupported schema version")


class ContractIssue19(StrictModel):
    path: str
    kind: Literal["missing", "conflict", "unsupported", "optional"]
    message: str
    answered_unknown: bool = False


class Assessment19(StrictModel):
    status: Literal["needs_clarification", "preliminary", "ready_for_solver"]
    # A requirements validator cannot claim a solved rule minimum.
    rules_count: None = None
    issues: list[ContractIssue19] = Field(default_factory=list)


def assess(request: Requirements19) -> Assessment19:
    issues: list[ContractIssue19] = []
    unknown = {note.path for note in request.unknowns if note.reason == "user_unknown"}
    documented_gaps = {note.path for note in request.unknowns}

    def issue(path: str, message: str, kind: Literal[
        "missing", "conflict", "unsupported", "optional",
    ] = "missing") -> None:
        issues.append(ContractIssue19(path=path, message=message, kind=kind,
                                      answered_unknown=path in unknown))

    if len(request.networks) != 2:
        issue("/networks", "Укажите две сети: кейс рассматривает одну границу.")
    if not request.flows:
        issue("/flows", "Опишите хотя бы один поток данных или файлов.")
    trusts = [network.trust for network in request.networks]
    if trusts.count("closed") == 2:
        issue("/networks", "Уточните, какая сеть защищаемая относительно другой.", "conflict")
    if len(trusts) == 2 and trusts.count("open") == 2 and request.protected_network_id is None:
        issue("/protected_network_id", "Какую открытую сеть считать защищаемой в этом проекте?")
    closed = [network.id for network in request.networks if network.trust == "closed"]
    if len(closed) == 1 and request.protected_network_id not in (None, closed[0]):
        issue("/protected_network_id", "Выбор стороны противоречит закрытой сети. Уточните его.",
              "conflict")
    for index, network in enumerate(request.networks):
        if network.name is None:
            issue(f"/networks/{index}/name", "Уточните название сети.")
        if network.trust is None:
            issue(f"/networks/{index}/trust", "Уточните доверие сети.")
    for index, system in enumerate(request.systems):
        if system.kind != "usb" and system.network_id is None:
            issue(f"/systems/{index}/network_id", "К какой сети относится система?")
    for index, flow in enumerate(request.flows):
        root = f"/flows/{index}"
        if flow.sender_network_id is None:
            issue(root + "/sender_network_id", "Из какой сети идёт поток?")
        if flow.receiver_network_id is None:
            issue(root + "/receiver_network_id", "В какую сеть идёт поток?")
        file_source = next(
            (side for side in flow.sides if side.network_id == flow.sender_network_id),
            None,
        ) if isinstance(flow, FileFlow19) else None
        if not flow.sender_ids and not (
            file_source is not None and (
                file_source.manual is True or file_source.usb is True or file_source.actor_ids
                or any(setting.operation == "PULL" for setting in file_source.automations)
            )
        ):
            issue(root + "/sender_ids", "Какая система отправляет поток?")
        if not flow.receiver_ids and not (
            isinstance(flow, DataFlow19) and flow.application == "ntp"
        ):
            issue(root + "/receiver_ids", "Какая система принимает поток?")
        if isinstance(flow, DataFlow19):
            if flow.destination_port is None and flow.application != "ntp":
                issue(root + "/destination_port", "Какой порт используется, если он известен?",
                      "optional")
            for field in ("mode", "protocol"):
                known_fact = flow.application == "ntp" or (
                    field == "mode" and flow.protocol in {"MQTT", "Modbus TCP", "МЭК 104"}
                )
                if getattr(flow, field) is None and not known_fact:
                    issue(root + "/" + field, "Нужны ли ответные пакеты?" if field == "mode"
                          else "Какой протокол используется для этого обмена?")
            if flow.protocol in {"MQTT", "Modbus TCP", "МЭК 104"} and flow.mode == "one-way":
                issue(root + "/mode", "Промышленный протокол требует двунаправленного режима.",
                      "conflict")
            if flow.application == "ntp" and flow.mode == "two-way":
                issue(root + "/mode", "В методике кейса NTP — однонаправленное правило.",
                      "conflict")
        else:
            if flow.direction is None:
                issue(root + "/direction", "Файлы передаются в одну сторону или в обе?")
            if len(flow.sides) != 2:
                issue(root + "/sides", "Уточните способы передачи и проверки с обеих сторон.")
            for side_index, side in enumerate(flow.sides):
                path = root + f"/sides/{side_index}"
                if side.manual is None and side.usb is None and not side.automations:
                    issue(path + "/manual", "Как файлы попадают на узел и забираются с него?")
                checks_path = path + "/checks"
                if side.checks is None and (
                    checks_path in documented_gaps or not flow.has_declared_checks()
                ):
                    issue(checks_path, "Уточните проверки файлов на этой стороне.")
                receives_files = (
                    flow.direction != "forward" or side.network_id != flow.sender_network_id
                )
                if receives_files and side.received_files_shared is None:
                    issue(path + "/received_files_shared",
                          "Могут ли получатели видеть общие файлы?")
                for n, automatic in enumerate(side.automations):
                    for field in ("protocol", "storage_id"):
                        if getattr(automatic, field) is None:
                            issue(path + f"/automations/{n}/{field}",
                                  "Уточните протокол и хранилище автоматической передачи.")
                for check_index, check in enumerate(side.checks or []):
                    check_path = checks_path + f"/{check_index}"
                    if check.details is None and check.kind in ("name_mask", "size"):
                        question = ("Какую маску имени файла использовать?"
                                    if check.kind == "name_mask"
                                    else "Какое ограничение по размеру файла использовать?")
                        issue(check_path + "/details", question)
                    if check.kind == "unsupported":
                        issue(check_path,
                              "Эта проверка — пожелание вендору, а не функция продукта.",
                              "unsupported")
    if request.manual_users is None and has_unidentified_manual_users(
        flow for flow in request.flows if isinstance(flow, FileFlow19)
    ):
        issue("/manual_users", "Уточните число ручных пользователей для лицензии.", "optional")
    # Several file flows can refer to the same optional session-wide user count.
    issues = list({(item.path, item.kind): item for item in issues}.values())
    blocking = any(item.kind in {"missing", "conflict"} and not item.answered_unknown
                   for item in issues)
    status: Literal["needs_clarification", "preliminary", "ready_for_solver"] = (
        "needs_clarification" if blocking else "preliminary" if issues or request.unknowns
        or request.assumptions else "ready_for_solver"
    )
    return Assessment19(status=status, issues=issues)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Sinoniks requirements; no model calls")
    commands = parser.add_subparsers(dest="command", required=True)
    schema = commands.add_parser("schema")
    schema.add_argument("output", type=Path)
    validate = commands.add_parser("validate")
    validate.add_argument("input", type=Path)
    solve = commands.add_parser("solve-data")
    solve.add_argument("input", type=Path)
    solve.add_argument("--protected-network-id")
    solve_all = commands.add_parser("solve")
    solve_all.add_argument("input", type=Path)
    solve_all.add_argument("--protected-network-id")
    export = commands.add_parser("export-result")
    export.add_argument("input", type=Path)
    export.add_argument("output", type=Path)
    export.add_argument("--session-id", required=True)
    export.add_argument("--protected-network-id")
    diagram = commands.add_parser("export-diagram")
    diagram.add_argument("input", type=Path)
    diagram.add_argument("output", type=Path)
    diagram.add_argument("--protected-network-id")
    diagram.add_argument("--format", choices=("drawio", "scene"), default="drawio")
    migrate = commands.add_parser("upgrade-input")
    migrate.add_argument("input", type=Path)
    migrate.add_argument("output", type=Path)
    migrate.add_argument("--project-id", required=True)
    args = parser.parse_args()
    output: str | dict[str, Any]
    try:
        if args.command == "schema":
            output = Requirements19.model_json_schema()
        else:
            request = read_document(json.loads(args.input.read_text(encoding="utf-8-sig")))
            if args.command == "upgrade-input":
                if not isinstance(request, DesignInput):
                    raise ValueError("Migration requires original v1/v2 input, not an archive")
                output = upgrade_input(request, project_id=args.project_id).model_dump(mode="json")
            elif args.command == "export-result":
                if not isinstance(request, Requirements19):
                    raise ValueError("Expert result requires v3 input")
                output = export_result(
                    request, session_id=args.session_id,
                    protected_network_id=args.protected_network_id,
                )
            elif args.command == "export-diagram":
                if not isinstance(request, Requirements19):
                    raise ValueError("The 1.9 diagram requires v3 input")
                result = solve_project(request, protected_network_id=args.protected_network_id)
                scene = layout19(request, result)
                output = scene_drawio(scene) if args.format == "drawio" else scene
            elif args.command == "solve":
                if not isinstance(request, Requirements19):
                    raise ValueError("Rule calculation requires v3 input")
                output = calculate_rules(
                    request, protected_network_id=args.protected_network_id,
                )
            elif args.command == "solve-data":
                if not isinstance(request, Requirements19):
                    raise ValueError("Network rule calculation requires v3 input")
                calculation = calculate_data_rules(request, protected_network_id=(
                    args.protected_network_id
                ))
                output = {
                    "placement": asdict(calculation.placement) if calculation.placement else None,
                    "data_rules": [rule.as_dict() for rule in calculation.rules],
                    "network_minimum": calculation.network_minimum,
                    "confirmed_network_lower_bound": calculation.confirmed_network_lower_bound,
                    "unhandled_file_flow_ids": calculation.file_flow_ids,
                    "issues": [asdict(issue) for issue in calculation.issues],
                }
            else:
                output = {"schema_version": request.schema_version,
                          "kind": type(request).__name__}
                if isinstance(request, Requirements19):
                    output["assessment"] = assess(request).model_dump(mode="json")
        content = (output if isinstance(output, str)
                   else json.dumps(output, ensure_ascii=False, indent=2)) + "\n"
        if args.command in {"schema", "upgrade-input", "export-result", "export-diagram"}:
            # Exclusive creation: an existing archive/output is never overwritten.
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(content)
        else:
            print(content, end="")
    except (ValueError, ValidationError, OSError) as error:
        parser.exit(2, f"Contract validation failed: {error}\n")


if __name__ == "__main__":
    main()
