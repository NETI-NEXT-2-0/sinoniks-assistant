"""Revision-local engineering review. No LLM judgement or deployment guarantee."""

import json
from dataclasses import asdict
from typing import Any
from xml.etree.ElementTree import ParseError, fromstring

from partner_agent.sinoniks_contract19 import FileFlow19, Requirements19
from partner_agent.sinoniks_contract19_io import assess
from partner_agent.sinoniks_layout import Box, text_lines

TITLES = {
    "checked": "Расчёт проверен по заданным условиям",
    "needs_clarification": "Нужны уточнения",
    "preliminary": "Предварительная схема",
    "conflict": "Есть противоречия",
    "unsupported": "Есть ограничения кейса",
}


def artifact_conflicts(result: dict[str, Any], artifacts: dict[str, str]) -> list[dict[str, Any]]:
    """Check semantic identities, not presentation coordinates or key ordering."""
    try:
        expert = json.loads(artifacts["json"])
        expected = result["rules"]
        if (expert["rules"] != expected or expert["rules_count"] != len(expected)
                or result["rules_count"] != len(expected)
                or expert["confirmed_rules_count"] != result["confirmed_rules_count"]
                or expert["synonyx"] != result["synonyx"]
                or expert["firewall"] != result["firewall"]):
            raise ValueError("result mismatch")
        if bool(artifacts.get("diagram")) != bool(artifacts.get("drawio")):
            raise ValueError("incomplete diagram export")
        if result.get("synonyx") and expected and not result.get("capacity_exceeded"):
            if not artifacts.get("diagram"):
                raise ValueError("missing diagram")
        if "diagram" in artifacts:
            scene = json.loads(artifacts["diagram"])
            rows = scene["rule_rows"]
            root = fromstring(artifacts["drawio"])
            if (root.tag != "mxfile" or rows != json.loads(root.attrib["data-rule-rows"])
                    or len(rows) != len(expected)):
                raise ValueError("diagram mismatch")
            if not scene["pages"]:
                raise ValueError("missing pages")
            for page in scene["pages"]:
                diagram = next((node for node in root.findall("diagram")
                                if node.get("id") == page["id"]), None)
                if diagram is None:
                    raise ValueError("missing native page")
                cells = {cell.get("id"): cell for cell in diagram.findall(".//mxCell")}
                for box in page["boxes"]:
                    if box["id"] not in cells or cells[box["id"]].get("value") != box["text"]:
                        raise ValueError("native object mismatch")
                for edge in page["edges"]:
                    cell = cells.get(edge["id"])
                    if (cell is None or cell.get("source") != edge["source"]
                            or cell.get("target") != edge["target"]):
                        raise ValueError("native link mismatch")
            for row, rule in zip(rows, expected, strict=True):
                if any((row["number"] != rule["id"], row["id"] != rule["rule_id"],
                        row["from_network_id"] != rule["from"],
                        row["to_network_id"] != rule["to"],
                        row["two_way"] != (rule["mode"] == "two-way"))):
                    raise ValueError("rule mismatch")
                if rule["type"] == "data" and (
                    row["protocol"] != rule["protocol"] or row["destination_port"] != rule["port"]
                ):
                    raise ValueError("protocol mismatch")
        return []
    except (ValueError, TypeError, KeyError, ParseError):
        return [{"code": "artifact_mismatch", "path": None, "subject": "Выгрузки",
                 "message": "Схема, правила и экспорт не согласованы. Пересохраните проект; "
                 "этот результат не отмечен как проверенный.", "answered_unknown": False}]


def readiness_report(
    request: Requirements19, result: dict[str, Any], artifacts: dict[str, str],
) -> dict[str, Any]:
    assessment = assess(request)
    groups: dict[str, list[dict[str, Any]]] = {"missing": [], "conflicts": [], "limitations": []}
    for issue in assessment.issues:
        parts = issue.path.split("/")
        subject = "Проект"
        if len(parts) >= 3 and parts[1] in {"networks", "systems", "flows"}:
            entity = getattr(request, parts[1])[int(parts[2])]
            subject = (getattr(entity, "name", None) or getattr(entity, "purpose", None)
                       or f"Обмен {int(parts[2]) + 1}")
        group = ("conflicts" if issue.kind == "conflict" else
                 "limitations" if issue.kind == "unsupported" else "missing")
        groups[group].append({"code": issue.kind, "path": issue.path,
                              "subject": subject[:120], "message": issue.message,
                              "answered_unknown": issue.answered_unknown,
                              "blocking": issue.kind == "missing"})
    integrity = artifact_conflicts(result, artifacts)
    groups["conflicts"].extend(integrity)
    if result.get("synonyx") and not result.get("firewall"):
        groups["conflicts"].append({"code": "firewall_missing", "path": None,
                                    "subject": "Размещение", "answered_unknown": False,
                                    "message": "На границе защищаемого сегмента не указан МЭ, "
                                    "предусмотренный методикой. Это обязательное исправление."})
    if result.get("capacity_exceeded"):
        groups["limitations"].append({"code": "capacity", "path": None, "subject": "Ёмкость",
                                      "message": "Превышен предел одного ПАК в рамках кейса.",
                                      "answered_unknown": False})
    if result.get("confirmed_rules_count") is None and not groups["missing"]:
        groups["missing"].append({"code": "minimum_unconfirmed", "path": None,
                                 "subject": "Расчёт", "message": "Полный минимум правил "
                                 "не подтверждён. Проверьте неполные условия обмена.",
                                 "blocking": False, "answered_unknown": False})
    referenced = {identifier for flow in request.flows
                  for identifier in [*flow.sender_ids, *flow.receiver_ids]}
    recommendations = [{"code": "unused_system", "subject": system.name or system.id,
                        "message": "Устройство не привязано к отправителю или получателю. "
                        "По желанию проверьте, нужен ли для него отдельный обмен.",
                        "source": "Сохранённые условия проекта"}
                       for system in request.systems if system.id not in referenced]
    for flow in request.flows:
        if isinstance(flow, FileFlow19) and any(side.checks == [] for side in flow.sides):
            recommendations.append({"code": "file_checks", "subject": flow.purpose or flow.id,
                                    "message": "Явно выбрано отсутствие файловых проверок. "
                                    "Если появятся требования к фильтрации, можно дополнить "
                                    "правило поддерживаемыми проверками. "
                                    "Это необязательное изменение.",
                                    "source": "02_methodology.md · 3.2. Правила передачи файлов"})
    state = ("conflict" if groups["conflicts"] else
             "unsupported" if result.get("status") == "unsupported" else
             "needs_clarification" if any(item.get("blocking") and not item["answered_unknown"]
                                         for item in groups["missing"]) else
             "preliminary" if groups["missing"] or groups["limitations"]
             or request.assumptions or result.get("confirmed_rules_count") is None else "checked")
    return {"schema_version": 1, "checker_version": "sinoniks19-1", "status": state,
            "title": TITLES[state],
            "summary": "Проверяются сохранённые условия, расчёт и согласованность выгрузок. "
            "Работу физического внедрения проверяет инженер.", **groups,
            "recommendations": recommendations,
            "assumptions": [{"subject": note.path, "message": note.explanation}
                            for note in request.assumptions],
            "checks": [{"code": "exports", "title": "Согласованность расчёта и выгрузок",
                        "status": "failed" if integrity else "passed"},
                       {"code": "minimum", "title": "Минимум правил подтверждён",
                        "status": "passed" if result.get("confirmed_rules_count") is not None
                        else "pending"}]}


def readiness_text(report: dict[str, Any]) -> str:
    lines = ["Проверка готовности: " + report["title"] + ".", report["summary"]]
    for key, title in (("conflicts", "Противоречия"), ("missing", "Недостающие данные"),
                       ("limitations", "Ограничения"), ("recommendations", "Необязательные советы"),
                       ("assumptions", "Принятые допущения")):
        if report[key]:
            lines.append(f"{title}: {len(report[key])}.")
    if any(report[key] for key in ("conflicts", "missing", "limitations", "recommendations",
                                   "assumptions")):
        lines.append("Подробный список проверки доступен в проекте; "
                     "советы не применяются автоматически.")
    return "\n".join(lines)


def annotate_readiness(scene: dict[str, Any], report: dict[str, Any]) -> None:
    """Same neutral palette and editable text objects as the partner's template."""
    for page in scene["pages"]:
        issues = [*report["conflicts"], *report["missing"], *report["limitations"]]
        lines = [report["title"]]
        lines.extend(f"• {item['subject']}: {item['message']}"[:220] for item in issues[:3])
        if len(issues) > 3:
            lines.append(f"Ещё {len(issues) - 3} пунктов — в пояснении проекта.")
        lines.append("Физическое внедрение проверяет инженер.")
        text = text_lines("\n".join(lines), 150)
        height = len(text.splitlines()) * 18 + 16
        page["boxes"].append(asdict(Box("readiness", text, 26, page["height"],
                                        page["width"] - 52, height, "text", "none", "none",
                                        "#B9C5CD", font_size=12)))
        page["height"] += height + 20
