from collections import defaultdict
from dataclasses import asdict

from partner_agent.sinoniks_calculation import RuleCalculation, solve_project
from partner_agent.sinoniks_contract19 import FileCheck19, FileFlow19, Requirements19
from partner_agent.sinoniks_data_rules import DataRule, existing_firewall_details
from partner_agent.sinoniks_file_rules import FileIssue, FileRule

FILE_ISSUE_LABELS = {
    "file_direction_unknown": "Не определено направление файлового обмена",
    "file_methods_unknown": "Не заданы стороны файлового обмена",
    "file_checks_unknown": "Не уточнены файловые проверки",
    "file_check_scope_unconfirmed": "Проверки этой стороны не подтверждены; "
                                    "уточните профиль или выберите «Без проверок»",
    "file_visibility_unknown": "Не уточнена видимость принятых файлов",
    "file_method_unknown_or_unavailable": "Не уточнён или недоступен способ передачи файлов",
    "file_automation_details_unknown": "Не уточнены протокол или хранилище файлов",
    "file_automation_pairing_unknown": "Не определено соответствие автоматических операций",
    "automation_conflicts_with_file_direction": "Автоматизация противоречит направлению обмена",
    "usb_conflicts_with_file_method": "Подключение USB противоречит указанному методу",
    "usb_requires_node_a": "USB должен подключаться на стороне Узла А",
    "usb_placement_unverified": "Привязка стороны USB к Узлу А ещё не проверена",
    "unsupported_file_check": "Запрошенная проверка не поддерживается продуктом",
}


def export_data_rule(rule: DataRule, number: int) -> dict[str, object]:
    return {
        **rule.as_dict(), "id": number, "rule_id": rule.id, "type": "data",
        "from": rule.from_network_id, "to": rule.to_network_id,
        "description": "; ".join(sorted(rule.purposes)) or (
            "Синхронизация времени" if rule.application == "ntp" else "Передача данных"
        ),
    }


def check_label(check: FileCheck19) -> str:
    if check.kind == "icap":
        return {
            "antivirus": "ICAP: внешний антивирус",
            "sandbox": "ICAP: внешняя песочница",
            "dlp": "ICAP: внешняя DLP",
            None: "ICAP",
        }[check.icap_service]
    return {
        "name_mask": "маска имени",
        "type_extension": "тип и расширение",
        "size": "размер",
        "signature": "цифровая подпись",
    }[check.kind]


def export_file_rule(
    rule: FileRule, number: int, nodes: dict[str, str], issues: list[FileIssue],
) -> dict[str, object]:
    primary = rule.primary_lane
    protocols: dict[str, set[str]] = defaultdict(set)
    checks: dict[str, list[dict[str, object]] | None] = {}
    labels: set[str] = set()
    for lane in rule.lanes:
        for side, setting in ((lane.source, lane.pull), (lane.destination, lane.push)):
            network_id = side.network_id
            methods = lane.methods_on(network_id)
            labels_on_side = protocols[network_id]
            if methods["usb"] is True:
                labels_on_side.add("USB-носитель на Узле А" if nodes.get(network_id) == "A"
                                   else "USB")
            manual = lane.manual_method_on(network_id)
            if manual is not None:
                labels_on_side.add("SFTP, вручную" if manual == "SFTP" else "Вручную")
            if setting is not None and setting.protocol is not None:
                labels_on_side.add(f"{setting.protocol}, {setting.operation}")
            supported = [check for check in (side.checks or []) if check.kind != "unsupported"]
            labels.update(check_label(check) for check in supported)
            if network_id in nodes:
                checks[nodes[network_id]] = (
                    None if side.checks is None else
                    [check.model_dump(mode="json") for check in supported]
                )
    purposes = sorted({flow.purpose for lane in rule.lanes for flow in lane.flows if flow.purpose})
    return {
        **rule.as_dict(), "id": number, "rule_id": rule.id, "type": "file",
        "flow_ids": rule.flow_ids,
        "from": primary.source.network_id, "to": primary.destination.network_id,
        "mode": "two-way" if rule.direction == "both" else "one-way",
        "protocol": {key: "; ".join(sorted(value)) or None for key, value in protocols.items()},
        "port": None, "description": "; ".join(purposes) or "Передача файлов",
        "checks": sorted(labels), "checks_by_node": checks,
        "provisional": any(lane.provisional for lane in rule.lanes),
        "provisional_reasons": [
            {**asdict(issue), "label": FILE_ISSUE_LABELS[issue.reason]} for issue in issues
        ],
    }


def _file_users(request: Requirements19, result: RuleCalculation) -> dict[str, object]:
    files = result.files
    details = files.user_details
    actors = {actor.id: actor.name for actor in request.actors}
    pulls = [
        {
            "rule_id": rule.id, "network_id": lane.source.network_id,
            "storage_id": lane.pull.storage_id, "protocol": lane.pull.protocol,
            "flow_ids": sorted(flow.id for flow in lane.flows),
        }
        for rule in files.rules for lane in rule.lanes if lane.pull is not None
    ]
    return {
        "manual": {
            "min": details.manual_lower, "max": details.manual_upper,
            "actors": [{"id": key, "name": actors[key]} for key in details.actor_ids],
        },
        "technical_pull_count": files.declared_pulls,
        "technical_pulls": pulls,
        "upper_unknown_reasons": list(details.upper_unknown_reasons),
    }


def _sources(result: RuleCalculation) -> list[dict[str, str]]:
    sections = []
    if result.data.placement:
        sections.append("2. Размещение")
    if result.data.rules:
        sections.append("3.1. Правила передачи данных")
    if result.files.rules:
        sections.extend(["3.2. Правила передачи файлов", "7. Дополнительное задание"])
    return [{"file": "02_methodology.md", "section": section} for section in sections]


def _vendor_requests(request: Requirements19) -> list[str]:
    wishes = [item.text for item in request.requests_for_vendor]
    for flow in request.flows:
        if isinstance(flow, FileFlow19):
            wishes.extend(check.details for side in flow.sides for check in side.checks or []
                          if check.kind == "unsupported" and check.details is not None)
    return list(dict.fromkeys(wishes))


def export_result(
    request: Requirements19, *, session_id: str, protected_network_id: str | None = None,
) -> dict[str, object]:
    result = solve_project(request, protected_network_id=protected_network_id)
    return result_document(request, result, session_id=session_id)


def result_document(
    request: Requirements19, result: RuleCalculation, *, session_id: str,
) -> dict[str, object]:
    if not session_id.strip():
        raise ValueError("A session identifier is required")
    placement = result.data.placement
    nodes = {} if placement is None else {
        placement.node_a_network_id: "A", placement.node_b_network_id: "B",
    }
    rules = [export_data_rule(rule, number) for number, rule in enumerate(result.data.rules, 1)]
    file_issues: dict[str, list[FileIssue]] = defaultdict(list)
    for issue in result.files.issues:
        file_issues[issue.flow_id].append(issue)
    for number, rule in enumerate(result.files.rules, len(rules) + 1):
        issues = [issue for key in rule.flow_ids for issue in file_issues[key]]
        rules.append(export_file_rule(rule, number, nodes, issues))
    assumptions = [item.model_dump(mode="json") for item in request.assumptions]
    if placement and placement.assumed_protected_side:
        assumptions.append({
            "kind": "protected_network_choice", "network_id": placement.protected_network_id,
            "explanation": "При двух открытых сетях выбрана защищаемая сторона проекта.",
        })
    unknowns = [item.model_dump(mode="json") for item in request.unknowns]
    unknowns.extend(asdict(issue) for issue in result.data.issues)
    unknowns.extend(asdict(issue) for issue in result.files.issues)
    unknowns.extend({"field": "port", "rule_id": rule.id, "reason": "port_unknown"}
                    for rule in result.data.rules if rule.port is None)
    unknowns.extend({"field": "trust", "network_id": network.id, "reason": "network_trust_unknown"}
                    for network in request.networks if network.trust is None)
    unnamed_networks = [network.id for network in request.networks if network.name is None]
    unknowns.extend({"field": "name", "network_id": identifier, "reason": "network_name_unknown"}
                    for identifier in unnamed_networks)
    if result.capacity_exceeded or (
        not rules and any(item.kind == "unsupported" for item in request.requests_for_vendor)
    ):
        status = "unsupported"
    elif not rules:
        status = "needs_clarification"
    elif result.minimum is None or unnamed_networks:
        status = "preliminary"
    else:
        status = "completed"
    users_range = result.files.file_users_range
    return {
        "schema_version": "1.1", "session_id": session_id.strip(), "status": status,
        "networks": [{"id": network.id, "name": network.name, "trust": network.trust}
                     for network in request.networks],
        "firewall": None if placement is None else {
            "side": placement.recommended_firewall_side, **existing_firewall_details(request),
        },
        "synonyx": None if placement is None else {
            "location": "closed", "node_a_faces": "open", "node_b_faces": "closed",
            "node_a_network_id": placement.node_a_network_id,
            "node_b_network_id": placement.node_b_network_id,
        },
        "rules": rules, "rules_count": len(rules), "rules_lower_bound": result.lower_bound,
        "rules_lower_bound_breakdown": {
            "data": result.data.confirmed_network_lower_bound, "file": result.files.lower_bound,
        },
        "confirmed_rules_count": result.minimum, "capacity_exceeded": result.capacity_exceeded,
        "file_users_count": result.files.file_users_count,
        "file_users_range": None if users_range is None else {
            "min": users_range.lower, "max": users_range.upper,
        },
        "file_users_breakdown": _file_users(request, result),
        "devices": [
            {"id": system.id, "network_id": system.network_id, "name": system.name,
             "kind": system.kind,
             **({"attachment": system.attachment} if system.attachment else {})}
            for system in request.systems
        ],
        "unknowns": unknowns, "assumptions": assumptions, "sources": _sources(result),
        "requests_for_vendor": _vendor_requests(request),
        "usage": {
            "status": "not_measured", "input_tokens": None, "output_tokens": None,
            "input_cost_usd": None, "output_cost_usd": None, "evaluation_cost_usd": None,
            "actual_cost_usd": None, "calls": [],
        },
        "timing": {"turns": [], "mean_turn_seconds": None},
    }
