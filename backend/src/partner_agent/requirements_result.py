"""Persisted 1.9 engineering output; consumes the solver, never reimplements it."""

import json
from typing import Any

from partner_agent.project_readiness import annotate_readiness, readiness_report, readiness_text
from partner_agent.sinoniks_calculation import solve_project
from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_export import diagram_artifacts
from partner_agent.sinoniks_layout19 import diagram_unavailability19, layout19
from partner_agent.sinoniks_result_export import FILE_ISSUE_LABELS, result_document

ISSUE_LABELS = {
    "protected_side_unresolved": "Не определена защищаемая сторона",
    "placement_unresolved": "Не определено размещение оборудования",
    "flows_unknown": "Не описаны потоки",
    "network_direction_unknown": "Не указано направление обмена",
    "systems_unknown": "Не указаны системы отправления или получения",
    "system_network_unknown": "Не определена сеть системы",
    "protocol_unknown": "Не указан протокол данных",
    "port_unknown": "Порт не задан пользователем",
    "network_trust_unknown": "Не определено доверие сети",
    "network_name_unknown": "Не указано название сети",
    "return_traffic_unknown": "Не уточнена необходимость ответа",
    "industrial_protocol_requires_two_way": "Промышленный протокол требует обмена с ответом",
    "ntp_requires_protected_destination": "Время должно передаваться в защищаемую сеть",
    "ntp_requires_one_time_source": "Нужен один источник времени",
    "ntp_requires_one_way_udp": "Для NTP в кейсе нужен однонаправленный UDP",
    **FILE_ISSUE_LABELS,
}


def explanation19(request: Requirements19, result: dict[str, Any]) -> str:
    networks = {item.id: item.name or "Сеть без названия" for item in request.networks}
    systems = {item.id: item.name or "Система без названия" for item in request.systems}
    placement = result.get("synonyx")
    lines = ["Проект Синоникса 1.9 по сохранённым требованиям."]
    if placement:
        outer = networks[placement["node_a_network_id"]]
        inner = networks[placement["node_b_network_id"]]
        lines.extend(
            [
                f"Открытая сторона — {outer}; защищаемая сторона — {inner}.",
                f"МЭ размещается на границе защищаемого сегмента {inner}, "
                "Синоникс — сразу за ним, внутри этого сегмента.",
                f"Узел А обращён к МЭ и сети {outer}; Узел В — к системам сети {inner}. "
                "Сети не соединяются напрямую, перенос проходит через изолированное Ядро.",
            ]
        )
        if request.existing_firewall_name is not None:
            lines.append(
                "На этой границе используется установленный экран "
                f"«{request.existing_firewall_name}»."
            )
    else:
        lines.append("Размещение пока не определено. Проверьте условия сетей.")
        if request.protected_network_id is not None:
            protected_name = networks[request.protected_network_id]
            lines.append(f"Защищаемая сторона по условиям: {protected_name}.")
    if result.get("capacity_exceeded"):
        lines.append("Одного ПАК недостаточно: нижняя оценка превышает 256 правил.")
    confirmed = result.get("confirmed_rules_count")
    if confirmed is not None:
        lines.append(f"Минимальное число правил Синоникса по заданным условиям: {confirmed}.")
    else:
        lower_bound = result.get("rules_lower_bound_breakdown")
        breakdown = (
            f" ({lower_bound['data']} для данных и {lower_bound['file']} для файлов)"
            if lower_bound else ""
        )
        lines.append(
            f"Предварительный набор: {result['rules_count']} правил. "
            f"Подтверждённая нижняя оценка: {result['rules_lower_bound']}{breakdown}. "
            "Полный минимум пока не подтверждён."
        )
    for rule in result["rules"]:
        direction = "↔" if rule["mode"] == "two-way" else "→"
        source, target = networks[rule["from"]], networks[rule["to"]]
        if rule["type"] == "data":
            sender_names = ", ".join(systems.get(key, key) for key in rule["collect"]["system_ids"])
            receiver_names = ", ".join(
                systems.get(key, key) for key in rule["exchange"]["system_ids"]
            )
            if rule.get("application") == "ntp" and not receiver_names:
                receiver_names = "получатели времени защищаемой сети"
            detail = (
                f"{sender_names} {direction} {receiver_names}; "
                f"{rule.get('protocol') or 'протокол не уточнён'}, "
                f"порт {rule.get('port') if rule.get('port') is not None else 'не задан'}"
            )
        else:
            methods = "; ".join(
                f"{networks.get(key, key)}: {value or 'метод не уточнён'}"
                for key, value in rule["protocol"].items()
            )
            checks = ", ".join(rule.get("checks", [])) or "явных проверок нет или они не уточнены"
            detail = f"{methods}. Проверки: {checks}"
        lines.append(
            f"{rule['id']}. {rule['description']}: {source} {direction} {target}. {detail}."
        )
        for issue in rule.get("provisional_reasons", []):
            network = networks.get(issue["network_id"])
            scope = f" ({network})" if network else ""
            label = issue["label"]
            if issue["reason"] != "file_check_scope_unconfirmed":
                label = f"Требует уточнения: {label}"
            lines.append(f"   {label}{scope}.")
    if any(system.kind == "usb" for system in request.systems) or any(
        flow.kind == "file" and any(side.usb is True for side in flow.sides)
        for flow in request.flows
    ):
        lines.append(
            "USB-носитель подключается непосредственно к Узлу А, минуя МЭ. "
            "Файлы с USB проверяет Синоникс."
        )
    users = result.get("file_users_count")
    user_range = result.get("file_users_range")
    if users is not None:
        lines.append(f"Пользователей передачи файлов: {users}.")
    elif user_range and user_range["max"] is not None:
        lines.append(
            f"Пользователей передачи файлов: от {user_range['min']} до {user_range['max']}."
        )
    elif user_range:
        lines.append(f"Пользователей передачи файлов: не менее {user_range['min']}.")
    else:
        lines.append("Число пользователей передачи файлов не определено.")
    user_details = result.get("file_users_breakdown")
    if user_details and any(flow.kind == "file" for flow in request.flows):
        manual = user_details["manual"]
        names = ", ".join(item["name"] or item["id"] for item in manual["actors"])
        people = f" ({names})" if names else ""
        manual_count = (
            str(manual["min"]) if manual["min"] == manual["max"] else
            f"{manual['min']}–{manual['max']}" if manual["max"] is not None else
            f"не менее {manual['min']}"
        )
        lines.append(f"Ручные пользователи: {manual_count}{people}.")
        lines.append(
            f"Технические пользователи: {user_details['technical_pull_count']} "
            "по одному на каждую учтённую направленную операцию PULL. "
            "Ручные пользователи считаются по уникальным участникам и заявленному диапазону."
        )
        if "manual_upper_unknown" in user_details["upper_unknown_reasons"]:
            lines.append("Верхняя граница числа ручных пользователей неизвестна.")
        if "file_rules_incomplete" in user_details["upper_unknown_reasons"]:
            lines.append(
                "Верхняя граница общего числа пока открыта из-за неполных файловых правил."
            )
    issues = list(
        dict.fromkeys(
            ISSUE_LABELS.get(item.get("reason"), "Остались неуточнённые условия")
            for item in result.get("unknowns", [])
        )
    )
    if issues:
        lines.append("Пробелы и ограничения: " + "; ".join(issues) + ".")
    if result.get("assumptions"):
        lines.append("В результате есть явные допущения; проверьте их перед внедрением.")
    if result.get("requests_for_vendor"):
        lines.append(
            "Отдельные пожелания продукту не считаются реализованными функциями: "
            + "; ".join(result["requests_for_vendor"])
            + "."
        )
    lines.append(
        "Считаются логические правила Синоникса, не настройки МЭ. "
        "Адреса узлов, профили и точные настройки проверяет инженер."
    )
    return "\n".join(lines)


def result_artifacts19(
    request: Requirements19,
    *,
    session_id: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    calculation = solve_project(request)
    result = result_document(request, calculation, session_id=session_id)
    text = explanation19(request, result)
    # Render-time observations arrive after commit; do not invent immutable
    # zero-cost/zero-time snapshots. The owner can export the actual ledger.
    result["accounting_export"] = f"/api/v1/chats/{session_id}/accounting/export"
    artifacts = {
        "requirements": request.model_dump_json(indent=2) + "\n",
        "json": json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        "txt": text + "\n",
    }
    scene = None
    if diagram_unavailability19(request, calculation) is None:
        scene = layout19(request, calculation)
        artifacts.update(diagram_artifacts(scene))
    report = readiness_report(request, result, artifacts)
    text += "\n\n" + readiness_text(report)
    artifacts["txt"] = text + "\n"
    if scene is not None:
        annotate_readiness(scene, report)
        artifacts.update(diagram_artifacts(scene))
    return {**result, "explanation": text, "readiness": report}, artifacts


def expert_accounting(ledger: dict[str, Any]) -> dict[str, Any]:
    turns = ledger["turns"]
    if not turns:
        return {}
    summary = ledger["summary"]
    active = any(turn["status"] in {"queued", "running"} for turn in turns)
    complete = ledger["accounting_complete"] and not active
    calls = [
        {**{key: value for key, value in call.items() if key not in {"usage", "raw_usage_format"}},
         "usage": {key: value for key, value in call["usage"].items()
                   if key not in {"raw", "reconciliation"}}}
        for call in ledger["calls"]
    ]
    meaningful_totals = (
        not active and not ledger["legacy_untracked_turns"] and not summary["unfinished_calls"]
    )
    full_mean = ledger["mean_full_elapsed_ms"]
    return {
        "usage": {
            "status": "measured" if complete else "partial",
            "input_tokens": summary["input_tokens"] if meaningful_totals else None,
            "output_tokens": summary["output_tokens"] if meaningful_totals else None,
            "input_cost_usd": summary["input_cost_usd"] if meaningful_totals else None,
            "output_cost_usd": summary["output_cost_usd"] if meaningful_totals else None,
            "evaluation_cost_usd": summary["estimated_cost_usd"] if meaningful_totals else None,
            "actual_cost_usd": summary["provider_cost_usd"] if meaningful_totals else None,
            "known_recorded_cost_usd": summary["known_recorded_cost_usd"],
            "calls": calls,
        },
        "timing": {
            "turns": turns,
            "mean_turn_seconds": full_mean / 1000 if full_mean is not None else None,
        },
    }
