from collections import defaultdict
from dataclasses import asdict, dataclass

from partner_agent.sinoniks_calculation import RuleCalculation
from partner_agent.sinoniks_contract19 import Requirements19, System19
from partner_agent.sinoniks_data_rules import RULE_LIMIT, DataPlacement, DataRule
from partner_agent.sinoniks_file_rules import FileLane, FileRule
from partner_agent.sinoniks_layout import (
    BACKGROUND,
    COLORS,
    FIREWALL_FLAME,
    Box,
    Edge,
    Page,
    _text,
    text_lines,
)

RULES_PER_PAGE = 3
ENDPOINTS_PER_SIDE = 16


@dataclass(frozen=True)
class Endpoint:
    side: str
    id: str
    label: str
    usb: bool = False


@dataclass
class DisplayRule:
    id: str
    number: int
    sender_side: str
    senders: list[Endpoint]
    receivers: list[Endpoint]
    two_way: bool
    label: str
    service: str
    policy: str
    protocol: str | None
    destination_port: int | None
    file: bool = False
    provisional: bool = False
    primary_senders: list[Endpoint] | None = None
    primary_receivers: list[Endpoint] | None = None

    def table_row(self) -> dict[str, object]:
        return {
            "id": self.id, "number": self.number, "sender_side": self.sender_side,
            "senders": [endpoint.label for endpoint in (self.primary_senders or self.senders)],
            "receiver": ", ".join(endpoint.label
                                  for endpoint in (self.primary_receivers or self.receivers)),
            "mode": "filetransfer" if self.file else "transport" if self.two_way else "diode",
            "protocol": self.protocol, "destination_port": self.destination_port,
            "service": self.service, "policy": self.policy,
            "two_way": self.two_way, "provisional": self.provisional,
        }


@dataclass
class LayoutContext:
    systems: dict[str, System19]
    placement: DataPlacement

    def endpoints(self, network: str, identifiers: set[str], fallback: str) -> list[Endpoint]:
        side = "A" if network == self.placement.node_a_network_id else "B"
        return [
            Endpoint(side, identifier, self.systems[identifier].name or identifier,
                     self.systems[identifier].kind == "usb")
            for identifier in sorted(identifiers)
        ] or [Endpoint(side, f"fallback-{fallback}", fallback)]

    def file_endpoints(self, lane: FileLane, sending: bool) -> list[Endpoint]:
        side = lane.source if sending else lane.destination
        setting = lane.pull if sending else lane.push
        network = side.network_id
        identifiers = {
            identifier for flow in lane.flows for identifier in (
                flow.sender_ids if network == flow.sender_network_id else flow.receiver_ids
            )
        }
        if setting and setting.storage_id:
            identifiers.add(setting.storage_id)
        result = self.endpoints(
            network, identifiers, "Источник файлов" if sending else "Получатель файлов",
        )
        if lane.methods_on(network)["usb"] is True and not any(item.usb for item in result):
            if not identifiers:
                result = []
            usb_side = "A" if network == self.placement.node_a_network_id else "B"
            result.append(Endpoint(usb_side, "manual-usb", "USB-носитель", True))
        return result

    def data(self, rule: DataRule, number: int) -> DisplayRule:
        senders = self.endpoints(rule.from_network_id, rule.sender_ids, "Источник данных")
        receivers = self.endpoints(rule.to_network_id, rule.receiver_ids, "Защищаемая сеть")
        label: str = rule.protocol or "Протокол неизвестен"
        label += f" {rule.port}" if rule.port is not None else " · порт неизвестен"
        return DisplayRule(
            rule.id, number, senders[0].side, senders, receivers, rule.mode == "two-way", label,
            ", ".join(sorted(rule.purposes)) or (
                "Синхронизация времени" if rule.application == "ntp" else "Передача данных"
            ), rule.policy or "", rule.protocol, rule.port, provisional=rule.protocol is None,
        )

    def file(self, rule: FileRule, number: int) -> DisplayRule:
        lane = rule.primary_lane
        primary_senders = self.file_endpoints(lane, True)
        primary_receivers = self.file_endpoints(lane, False)
        senders, receivers = list(primary_senders), list(primary_receivers)
        for reverse in rule.lanes:
            if lane is not reverse:
                senders += self.file_endpoints(reverse, True)
                receivers += self.file_endpoints(reverse, False)
        purposes = {flow.purpose for direction in rule.lanes for flow in direction.flows
                    if flow.purpose is not None}
        methods = {
            f"{setting.protocol} {setting.operation}" for direction in rule.lanes
            for setting in (direction.pull, direction.push)
            if setting is not None and setting.protocol is not None
        }
        if any(endpoint.usb for endpoint in (*senders, *receivers)):
            methods.add("USB")
        if any(direction.methods_on(side.network_id)["manual"] is True
               for direction in rule.lanes for side in (direction.source, direction.destination)):
            methods.add("SFTP вручную")
        description = ", ".join(sorted(methods)) or "Файлы"
        return DisplayRule(
            rule.id, number, senders[0].side, list(dict.fromkeys(senders)),
            list(dict.fromkeys(receivers)), rule.direction == "both", description,
            ", ".join(sorted(purposes)) or "Передача файлов",
            lane.flows[0].policy or "", description, None, True,
            any(direction.provisional for direction in rule.lanes),
            primary_senders, primary_receivers,
        )


def _arms(rule: DisplayRule) -> dict[Endpoint, bool]:
    arms = {endpoint: rule.two_way for endpoint in rule.senders}
    arms.update((endpoint, True) for endpoint in rule.receivers)
    arms = {endpoint: receives for endpoint, receives in arms.items()
            if not endpoint.usb or endpoint.side == "A"}
    for side in ("A", "B"):
        regular = [endpoint for endpoint in arms if endpoint.side == side and not endpoint.usb]
        for received in (False, True):
            endpoints = [endpoint for endpoint in regular if arms[endpoint] == received]
            if len(endpoints) > ENDPOINTS_PER_SIDE:
                for endpoint in endpoints:
                    del arms[endpoint]
                direction = "получателей" if received else "отправителей"
                label = f"Группа {direction} · {len(endpoints)}"
                arms[Endpoint(side, f"group-{rule.number}-{received}", label)] = received
    return arms


def _endpoints(
    page: Page, regular: dict[Endpoint, list[int]],
) -> tuple[dict[Endpoint, Box], int]:
    cards: dict[Endpoint, Box] = {}
    bottoms = [0]
    for side, x in (("A", 68), ("B", 1090)):
        top = 170
        for endpoint, uses in regular.items():
            if endpoint.side != side:
                continue
            name = text_lines(endpoint.label, 29)
            height = max(68, len(name.splitlines()) * 21 + 26, len(uses) * 35 + 34)
            box = Box(f"endpoint-{len(cards) + 1}", name, x, top, 244, height,
                      font_size=15, bold=True)
            page.boxes.append(box)
            cards[endpoint] = box
            top += height + 26
        bottoms.append(top)
    return cards, max(bottoms)


def _equipment(page: Page, centre: int, count: int, bottom: int, names: tuple[str, str]) -> dict[
    str, Box
]:
    height = count * 36 + 60
    top = centre - height // 2
    boxes = {
        "firewall": Box("firewall", "", 496, top - 10, 47, height + 20,
                        "firewall", "#354552", "#FFAB4D", radius=2),
        "node-A": Box("node-A", "Узел А", 666, top, 70, height,
                      "device", "#E8EDF0", "none", "#303E48", font_size=12),
        "core": Box("core", "Ядро", 756, top, 70, height,
                    "device", "#E8EDF0", "none", "#303E48", font_size=12),
        "node-B": Box("node-B", "Узел В", 846, top, 70, height,
                      "device", "#E8EDF0", "none", "#303E48", font_size=12),
    }
    page.boxes.extend([
        Box("open-zone", text_lines(f"Открытая сеть · {names[0]}", 42), 26, 83, 340,
            bottom - 83, "zone", "none", COLORS[0], COLORS[0], True),
        Box("closed-zone", text_lines(f"Защищаемая сеть · {names[1]}", 85), 531, 83, 873,
            bottom - 83, "zone", "none", COLORS[1], COLORS[1], True),
        Box("sinoniks", "", 653, top - 13, 278, height + 26,
            "appliance", "#FFFFFF", COLORS[0], BACKGROUND),
        *boxes.values(),
    ])
    _text(page, "firewall-title", "МЭ", 496, top - 48, 64, color="#FFFFFF")
    _text(page, "appliance-title", "СИНОНИКС", 653, top - 45, 242, color="#FFFFFF", size=16)
    for offset in range(20, height + 17, 21):
        page.boxes.extend([
            Box(f"brick-{offset}", "", 497, top - 10 + offset, 45, 1,
                "detail", "#FFAB4D", radius=0),
            Box(f"joint-{offset}", "", 508 + 13 * ((offset // 21) % 2),
                top - 30 + offset, 1, 20, "detail", "#FFAB4D", radius=0),
        ])
    page.boxes.append(Box("firewall-flame", "", 508, centre - 16, 24, 32,
                          "detail", "none", image=FIREWALL_FLAME))
    return boxes


def _segment(
    page: Page, rule: DisplayRule, label: str, endpoint: Box, equipment: Box,
    endpoint_y: int, equipment_y: int, channel: int, incoming: bool, terminal: bool,
) -> None:
    left = endpoint.x < equipment.x
    start = [endpoint.x + (endpoint.width if left else 0), endpoint_y]
    end = [equipment.x + (0 if left else equipment.width), equipment_y]
    points = [start, [channel, endpoint_y], [channel, equipment_y], end]
    if endpoint_y == equipment_y:
        points = [start, end]
    source, target = endpoint.id, equipment.id
    if incoming:
        source, target = target, source
        points.reverse()
    page.edges.append(Edge(
        f"{rule.id}-{label}", source, target, points, COLORS[(rule.number - 1) % len(COLORS)],
        rule.id, "none", "block" if incoming and terminal else "none",
    ))


def _page(
    rules: list[DisplayRule], names: tuple[str, str], count_text: str,
) -> Page:
    start, end = rules[0].number, rules[-1].number
    page = Page(f"rules-{start}", f"Правила {start}–{end}")
    branches = {rule.number: _arms(rule) for rule in rules}
    regular: dict[Endpoint, list[int]] = defaultdict(list)
    usb: dict[Endpoint, list[int]] = defaultdict(list)
    for rule in rules:
        for endpoint in branches[rule.number]:
            (usb if endpoint.usb else regular)[endpoint].append(rule.number)
    cards, card_bottom = _endpoints(page, regular)
    centre = max(246, card_bottom // 2 + 72)
    usb_top = centre + 152
    bottom = max(card_bottom + 28, usb_top + (92 if usb else 0) + 24)
    equipment = _equipment(page, centre, len(rules), bottom, names)
    _text(page, "title", "Синоникс 1.9 · " + page.title, 26, 12, 1380,
          color="#FFFFFF", size=22)
    usb_box = None
    if usb:
        usb_title = next(iter(usb)).label if len(usb) == 1 else f"USB-носители · {len(usb)}"
        usb_box = Box(
            "usb-1", text_lines(usb_title, 26), 642, usb_top,
            204, 64, "system", "#4A5D6A", COLORS[2], bold=True,
        )
        page.boxes.append(usb_box)
    for offset, rule in enumerate(rules):
        port = centre + 10 + (offset - len(rules) // 2) * 31
        color = COLORS[(rule.number - 1) % len(COLORS)]
        left_arms = [received for endpoint, received in branches[rule.number].items()
                     if endpoint.side == "A" and not endpoint.usb]
        if left_arms:
            _segment(page, rule, "wall", equipment["firewall"], equipment["node-A"],
                     port, port, 590, any(left_arms), False)
        usb_arms = [received for endpoint, received in branches[rule.number].items()
                    if endpoint.usb]
        if usb_box is not None and usb_arms:
            node = equipment["node-A"]
            x = node.x + 15 + offset * 16
            points = [[x, usb_box.y], [x, node.y + node.height]]
            receives = any(usb_arms)
            if receives:
                points.reverse()
            page.edges.append(Edge(
                f"{rule.id}-usb", node.id if receives else usb_box.id,
                usb_box.id if receives else node.id, points, color, rule.id, "none",
                "block" if receives else "none",
            ))
        for index, (endpoint, received) in enumerate(branches[rule.number].items()):
            if endpoint.usb:
                continue
            box = cards[endpoint]
            uses = regular[endpoint]
            socket = box.y + box.height * (uses.index(rule.number) + 1) // (len(uses) + 1)
            left = endpoint.side == "A"
            device = equipment["firewall" if left else "node-B"]
            _segment(page, rule, f"arm-{index}", box, device, socket, port,
                     (398 + offset * 30) if left else (948 + offset * 30), received, True)
            short_protocol = {"Modbus TCP": "Modbus", "МЭК 104": "МЭК104"}.get(
                rule.protocol or "?", rule.protocol or "?",
            )
            branch = "файлы" if rule.file else f"{short_protocol} {rule.destination_port or '?'}"
            _text(page, f"{rule.id}-label-{index}", f"{rule.number} · {branch}",
                  319 if left else 992, socket - 31, 167 if left else 98,
                  color=color, size=10)
        chain = ["node-A", "core", "node-B"]
        if rule.sender_side == "B":
            chain.reverse()
        for index, (source, target) in enumerate(zip(chain, chain[1:])):
            _segment(page, rule, f"isolated-{index}", equipment[source], equipment[target],
                     port, port, 0, False, False)
            page.edges[-1].dashed = True
    top = bottom + 18
    top += _text(page, "count", count_text, 26, top, 1360, color="#FFFFFF", size=16)
    for rule in rules:
        direction = "двунаправленное" if rule.two_way else "однонаправленное"
        text = f"{rule.number}. {rule.service} · {direction} · {rule.label}"
        if rule.provisional:
            text += " · предварительно"
        top += _text(page, f"rule-{rule.number}", text, 26, top + 6, 1360,
                     color=COLORS[(rule.number - 1) % len(COLORS)]) + 6
    top += _text(page, "placement", "МЭ на границе. Узел А обращён наружу, Узел В — к "
                 "защищаемой сети. Пунктир обозначает изолированный перенос через Ядро.",
                 26, top + 14, 1360)
    page.height = top + 44
    return page


def diagram_unavailability19(result: RuleCalculation) -> str | None:
    rule_count = len(result.data.rules) + len(result.files.rules)
    if result.data.placement is None:
        return "A protected network is required for the diagram"
    if result.capacity_exceeded or rule_count > RULE_LIMIT:
        return "The selected rules exceed a single appliance diagram"
    if not rule_count:
        return "The diagram requires at least one calculated rule"
    return None


def layout19(request: Requirements19, result: RuleCalculation) -> dict[str, object]:
    error = diagram_unavailability19(result)
    if error:
        raise ValueError(error)
    placement = result.data.placement
    assert placement is not None
    context = LayoutContext({system.id: system for system in request.systems}, placement)
    rules = [context.data(rule, number) for number, rule in enumerate(result.data.rules, 1)]
    rules.extend(context.file(rule, number)
                 for number, rule in enumerate(result.files.rules, len(rules) + 1))
    networks = {network.id: network.name or network.id for network in request.networks}
    names = (networks[placement.node_a_network_id], networks[placement.node_b_network_id])
    count = (f"Логических правил: {result.minimum}" if result.minimum is not None else
             f"Предварительный расчёт · минимум {result.lower_bound}")
    pages = [_page(rules[start:start + RULES_PER_PAGE], names, count)
             for start in range(0, len(rules), RULES_PER_PAGE)]
    return {
        "schema_version": 1, "design_id": request.id, "background": BACKGROUND,
        "font_family": "Open Sans", "profile": "calculated-sinoniks-19",
        "rule_rows": [rule.table_row() for rule in rules],
        "pages": [asdict(page) for page in pages],
    }
