import argparse
import hashlib
import json
import unicodedata
from pathlib import Path

from partner_agent.sinoniks_contract import (
    INDUSTRIAL_PROTOCOLS,
    Connection,
    Counts,
    Device,
    Exchange,
    Network,
    Question,
    Rule,
    Side,
)
from partner_agent.sinoniks_contract import Design as Design
from partner_agent.sinoniks_contract import DesignInput as DesignInput
from partner_agent.sinoniks_export import drawio as drawio
from partner_agent.sinoniks_export import export_files
from partner_agent.sinoniks_slot_bound import network_slot_bounds


def _network_identity(name: str) -> str:
    return unicodedata.normalize("NFC", " ".join(name.casefold().split()))


def clarification_questions(request: DesignInput) -> list[Question]:
    questions = []
    for field, text in (
        ("network_a", "Назовите контур A."),
        ("network_b", "Назовите контур B."),
        ("firewall", "На каких границах требуется межсетевой экран — A, B или обеих?"),
    ):
        if getattr(request, field) is None:
            questions.append(Question(field=field, text=text))
    if request.network_a and request.network_b and (
        _network_identity(request.network_a) == _network_identity(request.network_b)
    ):
        questions.append(Question(
            field="network_b", text="Чем отличаются контуры A и B? Укажите разные названия.",
            reason="conflict",
        ))
    if not request.exchanges:
        questions.append(Question(
            field="exchanges", text="Какие данные или файлы передаются и в каком направлении?",
        ))
    for index, exchange in enumerate(request.exchanges):
        if exchange.protocol in INDUSTRIAL_PROTOCOLS and exchange.mode == "diode":
            questions.append(Question(
                field=f"exchanges.{index}.mode",
                text=(
                    f"Обмен {index + 1} — для {exchange.protocol} нужен двунаправленный режим. "
                    "Допустимы ли ответы в рамках соединения или нужно изменить протокол?"
                ),
                reason="conflict",
            ))
        for field, text in (
            ("sender_side", "из какого контура начинается обмен"),
            ("sender", "кто отправляет"), ("receiver", "кто получает"),
            ("mode", "какой режим обмена требуется"),
            ("service", "какой сервис или категория файлов участвует"),
            ("policy", "какие ограничения действуют"),
        ):
            if getattr(exchange, field) is None:
                questions.append(Question(
                    field=f"exchanges.{index}.{field}", text=f"Обмен {index + 1} — {text}?",
                ))
    return questions


def clarify(request: DesignInput) -> list[str]:
    return [question.text for question in clarification_questions(request)]


def _group_rules(exchanges: list[Exchange]) -> list[Rule]:
    groups: dict[str, list[tuple[str, Exchange]]] = {}
    for index, exchange in enumerate(exchanges, 1):
        payload = exchange.model_dump(exclude={"requirement_id", "sources"})
        if exchange.mode != "filetransfer" and exchange.protocol and exchange.destination_port:
            del payload["sender"]
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        identifier = exchange.requirement_id or f"requirement-{index}"
        groups.setdefault(key, []).append((identifier, exchange))

    rules = []
    for key, requirements in groups.items():
        senders = sorted({exchange.sender for _, exchange in requirements if exchange.sender})
        payload = json.loads(key)
        payload["sender"] = senders[0]
        identity = key if len(senders) > 1 else json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        references = {
            (source.kind, source.reference, source.revision): source
            for _, exchange in requirements
            for source in exchange.sources
        }
        explicit_ids = sorted(
            exchange.requirement_id
            for _, exchange in requirements
            if exchange.requirement_id is not None
        )
        rules.append(Rule.model_validate({
            **payload,
            "id": "rule-" + hashlib.sha256(identity.encode()).hexdigest()[:20],
            "additional_senders": senders[1:],
            "requirement_id": explicit_ids[0] if explicit_ids else None,
            "requirement_ids": sorted(identifier for identifier, _ in requirements),
            "sources": [references[reference] for reference in sorted(references)],
        }))
    return sorted(rules, key=lambda rule: rule.id)


def plan(request: DesignInput) -> Design:
    if clarify(request):
        raise ValueError("Сначала уточните исходные данные.")
    assert request.network_a and request.network_b
    firewall_sides: list[Side] = []
    if request.firewall in ("A", "both"):
        firewall_sides.append("A")
    if request.firewall in ("B", "both"):
        firewall_sides.append("B")
    networks = {"A": request.network_a, "B": request.network_b}
    device_nodes = [
        Device(
            id="node-A", kind="sinoniks_node", name="Узел A", side="A",
            basis="product_architecture",
        ),
        Device(id="core", kind="sinoniks_core", name="Ядро Синоникс", basis="product_architecture"),
        Device(
            id="node-B", kind="sinoniks_node", name="Узел B", side="B",
            basis="product_architecture",
        ),
    ]
    for side in firewall_sides:
        device_nodes.append(Device(
            id=f"firewall-{side}", kind="firewall", name=f"МЭ {side}", side=side,
            basis="explicit_input",
        ))
    chain = ["network-A"]
    if "A" in firewall_sides:
        chain.append("firewall-A")
    chain.extend(["node-A", "core", "node-B"])
    if "B" in firewall_sides:
        chain.append("firewall-B")
    chain.append("network-B")
    connections = [
        Connection(
            id=f"link-{source}-{target}", source=source, target=target,
            kind="isolated_internal_transfer" if "core" in (source, target) else "network_link",
        )
        for source, target in zip(chain, chain[1:])
    ]
    rules = _group_rules(request.exchanges)
    network_count = sum(rule.mode != "filetransfer" for rule in rules)
    shared_receivers = any(rule.additional_senders for rule in rules)
    counts = Counts(
        input_requirements=len(request.exchanges), logical_exchanges=len(rules),
        network_slot_pairs=network_count, network_node_settings=2 * network_count,
        file_transfer_policies=len(rules) - network_count,
        counting_policy="shared-destination-v2" if shared_receivers else "exact-exchanges-v2",
        minimality=(
            "compatible_network_senders_grouped" if shared_receivers else "exact_duplicates_removed"
        ),
    )
    warnings = [
        "Размещение МЭ задано исходным условием и требует проверки инженером.",
        "Полный минимальный набор правил по методологии кейса пока не рассчитывается.",
        "Это концептуальный проект. Адреса узлов, лицензия и точные настройки требуют сверки.",
    ]
    incomplete_network_rules = sum(
        rule.mode != "filetransfer" and (rule.protocol is None or rule.destination_port is None)
        for rule in rules
    )
    if incomplete_network_rules:
        warnings.append(
            f"Сетевых обменов с неуказанным протоколом или портом — "
            f"{incomplete_network_rules}. Уточните поля, отмеченные в описании правил."
        )
    details = [_describe_rule(number, rule) for number, rule in enumerate(rules, 1)]
    placement = ", ".join(f"между сетью {side} и узлом {side}" for side in firewall_sides)
    grouping = (
        "объединения совместимых требований" if shared_receivers else "удаления точных дублей"
    )
    explanation = "\n".join([
        "Концептуальный проект для двух контуров.",
        f"Контур A — {request.network_a}. Контур B — {request.network_b}.",
        "Узлы A и B подключаются к своим сетям. Внутренний обмен проходит через ядро Синоникса.",
        f"По исходному условию МЭ размещается {placement}.",
        f"Исходных требований — {counts.input_requirements}. "
        f"Логических обменов после {grouping} — {counts.logical_exchanges}.",
        f"Сетевых пар слотов — {network_count}, настроек узлов — {2 * network_count}.",
        f"Файловых политик обмена — {counts.file_transfer_policies}.",
        *details, *warnings,
    ])
    slot_bounds = network_slot_bounds(rules)
    if slot_bounds:
        by_direction = ", ".join(
            f"{mode} {side} → {'B' if side == 'A' else 'A'} — {count}"
            for (mode, side), count in sorted(slot_bounds.items())
        )
        explanation += (
            f"\nНижняя оценка числа сетевых пар слотов по известным настройкам — "
            f"{sum(slot_bounds.values())}. Одна пара допускает один режим, одно направление "
            f"инициативы, один протокол и один номер порта. "
            f"По режимам и направлениям — {by_direction}."
        )
    identity = json.dumps({
        "networks": networks, "firewalls": firewall_sides,
        "rules": [rule.model_dump(mode="json") for rule in rules],
    }, ensure_ascii=False, sort_keys=True)
    network_nodes = [
        Network(id="network-A", side="A", name=request.network_a),
        Network(id="network-B", side="B", name=request.network_b),
    ]
    return Design(
        id="design-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
        networks={"A": request.network_a, "B": request.network_b}, network_nodes=network_nodes,
        firewall_sides=firewall_sides, device_nodes=device_nodes,
        devices=[next(device.name for device in device_nodes if device.id == identifier)
                 for identifier in chain[1:-1]],
        connections=connections, rules=rules, logical_rule_count=len(rules), counts=counts,
        warnings=warnings, explanation=explanation,
    )


def _describe_rule(number: int, rule: Rule) -> str:
    destination = "B" if rule.sender_side == "A" else "A"
    description = (
        f"{number}. [{rule.id}] {rule.sender_side}/{', '.join(rule.senders)} → "
        f"{destination}/{rule.receiver}, "
        f"{rule.mode}, {rule.service}. Ограничения — {rule.policy}."
        f" Требования — {', '.join(rule.requirement_ids)}."
    )
    if rule.mode == "filetransfer":
        return description
    protocol = rule.protocol if rule.protocol is not None else "уточнить"
    port = str(rule.destination_port) if rule.destination_port is not None else "уточнить"
    description += f" Протокол — {protocol}. Порт получателя — {port}."
    if rule.mode == "transport":
        description += " Ответные пакеты этой сессии разрешены."
    return description


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    request = DesignInput.model_validate_json(args.input.read_text(encoding="utf-8-sig"))
    questions = clarify(request)
    if questions:
        print(
            json.dumps(
                {"status": "needs_clarification", "questions": questions}, ensure_ascii=False
            )
        )
        return
    design = plan(request)
    args.output.mkdir()
    for extension, content in export_files(design).items():
        (args.output / f"design.{extension}").write_bytes(content.encode("utf-8"))


if __name__ == "__main__":
    main()
