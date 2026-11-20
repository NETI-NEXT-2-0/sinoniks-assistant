"""Persisted 1.9 engineering output; consumes the solver, never reimplements it."""

import json
from typing import Any

from partner_agent.sinoniks_calculation import solve_project
from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_export import diagram_artifacts
from partner_agent.sinoniks_layout19 import diagram_unavailability19, layout19
from partner_agent.sinoniks_result_export import result_document

ISSUE_LABELS = {
    "protected_side_unresolved": "Не определена защищаемая сторона",
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
    "file_checks_unknown": "Не уточнены файловые проверки",
    "file_visibility_unknown": "Не уточнена видимость принятых файлов",
    "file_method_unknown_or_unavailable": "Не уточнён или недоступен способ передачи файлов",
    "file_automation_details_unknown": "Не уточнены протокол или хранилище файлов",
    "file_automation_pairing_unknown": "Не определено соответствие автоматических операций",
    "usb_requires_node_a": "USB должен подключаться на стороне Узла А",
    "unsupported_file_check": "Запрошенная проверка не поддерживается продуктом",
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
    else:
        lines.append("Размещение пока не определено: уточните две сети и защищаемую сторону.")
    if result.get("capacity_exceeded"):
        lines.append("Одного ПАК недостаточно: нижняя оценка превышает 256 правил.")
    confirmed = result.get("confirmed_rules_count")
    if confirmed is not None:
        lines.append(f"Минимальное число правил Синоникса по заданным условиям: {confirmed}.")
    else:
        lines.append(
            f"Предварительный набор: {result['rules_count']} правил. "
            f"Подтверждённая нижняя оценка: {result['rules_lower_bound']}. "
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
    upper = user_range["max"] if user_range and user_range["max"] is not None else "неизвестно"
    lines.append(
        f"Пользователей передачи файлов: {users}."
        if users is not None
        else (
            f"Пользователей передачи файлов: от {user_range['min']} до {upper} (верхняя граница)."
            if user_range
            else "Число пользователей передачи файлов не определено."
        )
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
    if diagram_unavailability19(calculation) is None:
        artifacts.update(diagram_artifacts(layout19(request, calculation)))
    return {**result, "explanation": text}, artifacts
