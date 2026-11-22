from collections import defaultdict
from dataclasses import asdict, dataclass

from partner_agent.sinoniks_calculation import RuleCalculation
from partner_agent.sinoniks_contract19 import DataFlow19, FileCheck19, Requirements19, System19
from partner_agent.sinoniks_data_rules import (
    FIREWALL_ID,
    RULE_LIMIT,
    DataPlacement,
    DataRule,
    existing_firewall_details,
    known_protected_network,
)
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
from partner_agent.sinoniks_result_export import check_label

RULES_PER_PAGE = 3
ENDPOINTS_PER_SIDE = 16


def _check_summary(checks: list[FileCheck19] | None) -> str:
    labels = []
    for check in checks or []:
        if check.kind == "unsupported":
            continue
        label = check_label(check)
        if check.details is not None:
            label += f" ({check.details})"
        elif check.kind in ("name_mask", "size", "type_extension"):
            label += " (параметр не задан)"
        labels.append(label)
    return ", ".join(labels)


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
    flow_ids: list[str]
    from_network_id: str
    to_network_id: str
    file: bool = False
    provisional: bool = False
    primary_senders: list[Endpoint] | None = None
    primary_receivers: list[Endpoint] | None = None
    file_check_directions: list[dict[str, object]] | None = None
    file_check_summary: str = ""
    file_access_directions: list[dict[str, object]] | None = None
    file_access_summary: str = ""

    def table_row(self) -> dict[str, object]:
        return {
            "id": self.id, "number": self.number, "sender_side": self.sender_side,
            "flow_ids": self.flow_ids,
            "from_network_id": self.from_network_id, "to_network_id": self.to_network_id,
            "senders": [endpoint.label for endpoint in (self.primary_senders or self.senders)],
            "receiver": ", ".join(endpoint.label
                                  for endpoint in (self.primary_receivers or self.receivers)),
            "mode": "filetransfer" if self.file else "transport" if self.two_way else "diode",
            "protocol": self.protocol, "destination_port": self.destination_port,
            "service": self.service, "policy": self.policy,
            "two_way": self.two_way, "provisional": self.provisional,
            **({"file_check_directions": self.file_check_directions}
               if self.file_check_directions is not None else {}),
            **({"file_access_directions": self.file_access_directions}
               if self.file_access_directions is not None else {}),
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
        receiver_label = "Защищаемая сеть" if rule.application == "ntp" else "Получатель данных"
        receivers = self.endpoints(rule.to_network_id, rule.receiver_ids, receiver_label)
        label: str = rule.protocol or "Протокол неизвестен"
        label += f" {rule.port}" if rule.port is not None else " · порт неизвестен"
        return DisplayRule(
            rule.id, number, senders[0].side, senders, receivers, rule.mode == "two-way", label,
            ", ".join(sorted(rule.purposes)) or (
                "Синхронизация времени" if rule.application == "ntp" else "Передача данных"
            ), rule.policy or "", rule.protocol, rule.port,
            sorted(rule.flow_ids), rule.from_network_id, rule.to_network_id,
            provisional=rule.protocol is None or rule.provisional,
        )

    def file(self, rule: FileRule, number: int, network_names: dict[str, str]) -> DisplayRule:
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
        for direction in rule.lanes:
            for side in (direction.source, direction.destination):
                manual = direction.manual_method_on(side.network_id)
                if manual is not None:
                    methods.add("SFTP вручную" if manual == "SFTP" else "Вручную")
        description = ", ".join(sorted(methods)) or "Файлы"
        check_lines = []
        for direction in rule.lanes:
            for side in (direction.source, direction.destination):
                summary = _check_summary(side.checks)
                if summary:
                    check_lines.append(f"{network_names[side.network_id]}: {summary}")
        restricted = [direction.destination for direction in rule.lanes
                      if direction.destination.received_files_shared is False]
        access_lines = [
            f"{network_names[side.network_id]}: {side.access_group or 'группа не уточнена'}"
            for side in restricted
        ]
        return DisplayRule(
            rule.id, number, senders[0].side, list(dict.fromkeys(senders)),
            list(dict.fromkeys(receivers)), rule.direction == "both", description,
            ", ".join(sorted(purposes)) or "Передача файлов",
            lane.flows[0].policy or "", description, None, rule.flow_ids,
            lane.source.network_id, lane.destination.network_id, file=True,
            provisional=any(direction.provisional for direction in rule.lanes),
            primary_senders=primary_senders, primary_receivers=primary_receivers,
            file_check_directions=rule.check_directions(),
            file_check_summary="; ".join(dict.fromkeys(check_lines)),
            file_access_directions=rule.access_directions(),
            file_access_summary="; ".join(dict.fromkeys(access_lines)),
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


def _equipment(
    page: Page, centre: int, count: int, bottom: int, names: tuple[str, str],
    existing_firewall_name: str | None,
) -> dict[str, Box]:
    height = count * 36 + 60
    top = centre - height // 2
    boxes = {
        FIREWALL_ID: Box(FIREWALL_ID, "", 496, top - 10, 47, height + 20,
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
    if existing_firewall_name:
        label = (existing_firewall_name if len(existing_firewall_name) <= 28
                 else existing_firewall_name[:27] + "…")
        _text(page, "firewall-title", label, 388, top - 48, 256, color="#FFFFFF")
    else:
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
        rule.id, "open" if rule.two_way and terminal else "none",
        "block" if terminal and (incoming or rule.two_way) else "none",
    ))


def _page(
    rules: list[DisplayRule], names: tuple[str, str], count_text: str,
    existing_firewall_name: str | None,
    extras: list[Endpoint] | None = None,
) -> Page:
    start, end = rules[0].number, rules[-1].number
    page = Page(f"rules-{start}", f"Правила {start}–{end}")
    branches = {rule.number: _arms(rule) for rule in rules}
    regular: dict[Endpoint, list[int]] = defaultdict(list)
    usb: dict[Endpoint, list[int]] = defaultdict(list)
    for rule in rules:
        for endpoint in branches[rule.number]:
            (usb if endpoint.usb else regular)[endpoint].append(rule.number)
    for endpoint in extras or []:
        regular.setdefault(endpoint, [])
    cards, card_bottom = _endpoints(page, regular)
    centre = max(246, card_bottom // 2 + 72)
    usb_rules = [number for number, arms in branches.items() if any(item.usb for item in arms)]
    usb_top = centre + 152 + max(0, len(usb_rules) - 1) * 36
    bottom = max(card_bottom + 28, usb_top + (92 if usb else 0) + 24)
    equipment = _equipment(page, centre, len(rules), bottom, names, existing_firewall_name)
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
            _segment(page, rule, "wall", equipment[FIREWALL_ID], equipment["node-A"],
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
                usb_box.id if receives else node.id, points, color, rule.id,
                "open" if rule.two_way else "none", "block",
            ))
            _text(page, f"{rule.id}-label-usb", f"{rule.number} · файлы, USB",
                  node.x + 35 + (len(rules) - 1) * 16,
                  node.y + node.height + 18 + usb_rules.index(rule.number) * 36,
                  170, color=color, size=10)
        for index, (endpoint, received) in enumerate(branches[rule.number].items()):
            if endpoint.usb:
                continue
            box = cards[endpoint]
            uses = regular[endpoint]
            socket = box.y + box.height * (uses.index(rule.number) + 1) // (len(uses) + 1)
            left = endpoint.side == "A"
            device = equipment[FIREWALL_ID if left else "node-B"]
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
        senders = ", ".join(endpoint.label for endpoint in (rule.primary_senders or rule.senders))
        receivers = ", ".join(endpoint.label for endpoint in (
            rule.primary_receivers or rule.receivers
        ))
        arrow = "↔" if rule.two_way else "→"
        text = (f"{rule.number}. {senders} {arrow} {receivers}: {rule.service} · "
                f"{rule.label} · {direction}")
        if rule.provisional:
            text += " · предварительно"
        if rule.file_check_summary:
            text += "\nПроверки файлов · " + rule.file_check_summary
        if rule.file_access_summary:
            text += "\nРаздельный доступ · " + rule.file_access_summary
        page.boxes.append(Box(f"rule-marker-{rule.number}", "", 26, top + 22, 12, 12,
                              "detail", COLORS[(rule.number - 1) % len(COLORS)], radius=2))
        top += _text(page, f"rule-{rule.number}", text, 48, top + 6, 1338,
                     color="#FFFFFF") + 6
    top += _text(page, "placement", "МЭ на границе. Узел А обращён наружу, Узел В — к "
                 "защищаемой сети. Пунктир обозначает изолированный перенос через Ядро.",
                 26, top + 14, 1360)
    page.height = top + 44
    return page


def _topology_systems(request: Requirements19) -> list[System19]:
    return [system for system in request.systems if system.name and system.network_id is not None]


def _topology_pages(
    request: Requirements19, placement: DataPlacement, names: tuple[str, str],
    *, systems: list[System19] | None = None,
) -> list[Page]:
    supplementary = systems is not None
    systems = _topology_systems(request) if systems is None else systems
    sides = [
        [Endpoint(side, system.id, system.name or system.id) for system in systems
         if system.network_id == network_id]
        for side, network_id in (("A", placement.node_a_network_id),
                                 ("B", placement.node_b_network_id))
    ]
    pages: list[Page] = []
    for start in range(0, max(map(len, sides)), ENDPOINTS_PER_SIDE):
        index = len(pages) + 1
        title = ("Устройства без рассчитанных связей" if supplementary
                 else "Предварительная топология")
        page = Page(f"topology-{index}", f"{title} · {index}")
        endpoints: dict[Endpoint, list[int]] = {
            endpoint: [] for side in sides for endpoint in side[start:start + ENDPOINTS_PER_SIDE]
        }
        _, card_bottom = _endpoints(page, endpoints)
        centre = max(246, card_bottom // 2 + 72)
        bottom = max(card_bottom + 28, centre + 95)
        _equipment(page, centre, 0, bottom, names, request.existing_firewall_name)
        _text(page, "title", "Синоникс 1.9 · " + page.title, 26, 12, 1380,
              color="#FFFFFF", size=22)
        hint = ("Связи этих устройств пока не рассчитаны. " if supplementary
                else "Связи пока не рассчитаны. ")
        height = _text(page, "pending-rules", hint +
                       "Уточните условия обмена, чтобы увидеть правила.",
                       26, bottom + 18, 1360)
        page.height = bottom + height + 60
        pages.append(page)
    return pages


def diagram_unavailability19(request: Requirements19, result: RuleCalculation) -> str | None:
    rule_count = len(result.data.rules) + len(result.files.rules)
    if result.capacity_exceeded or rule_count > RULE_LIMIT:
        return "The selected rules exceed a single appliance diagram"
    if result.data.placement is None:
        known_networks = {network.id for network in request.networks if network.name}
        known_exchange = any({flow.sender_network_id, flow.receiver_network_id} <= known_networks
                             for flow in request.flows
                             if flow.sender_network_id and flow.receiver_network_id)
        if not _topology_systems(request) and not known_exchange:
            return "The preview requires a named system in a known network"
        return None
    if not rule_count and (
        not all(network.name for network in request.networks) or not _topology_systems(request)
    ):
        return "The diagram requires named networks and at least one known system or rule"
    return None


def _unplaced_topology(request: Requirements19) -> list[Page]:
    names = {network.id: network.name or "Сеть без названия" for network in request.networks}
    members = _topology_systems(request)
    groups = [[system for system in members if system.network_id == network.id]
              for network in request.networks]
    protected_network = known_protected_network(request)
    usb_systems = {system.id: system for system in request.systems if system.kind == "usb"}
    pages: list[Page] = []
    for start in range(0, max(1, max(map(len, groups), default=0)), ENDPOINTS_PER_SIDE):
        number = len(pages) + 1
        page = Page(f"preview-{number}", f"Предварительная топология · {number}")
        _text(page, "title", "Предварительная топология", 26, 12, 1380, size=22)
        _text(page, "placement", "Размещение оборудования требует уточнения.", 26, 58, 1380)
        bottom = 130
        for index, (network, systems) in enumerate(zip(request.networks, groups, strict=True)):
            left = 26 + index * 710
            trust = {"open": "Открытая", "closed": "Закрытая"}.get(
                network.trust or "", "Доверие неизвестно",
            )
            protected = (
                " · защищаемая по условиям" if network.id == protected_network else ""
            )
            caption = text_lines(f"{names[network.id]} · {trust}{protected}", 64)
            top = 130 + 20 * len(caption.splitlines()) + 16
            for system in systems[start:start + ENDPOINTS_PER_SIDE]:
                label = text_lines(system.name or system.id, 62)
                height = 22 * len(label.splitlines()) + 24
                page.boxes.append(Box(f"system-{system.id}", label, left + 20, top, 630, height))
                top += height + 12
            page.boxes.append(Box(f"zone-{network.id}", caption, left, 108, 680, top - 108,
                                  "zone", "none", COLORS[3], COLORS[3], True))
            bottom = max(bottom, top)
        page.height = bottom + 26
        pages.append(page)
    flows = [flow for flow in request.flows if flow.sender_network_id in names
             and flow.receiver_network_id in names]
    for offset in range(0, len(flows), ENDPOINTS_PER_SIDE):
        page = pages[0] if offset == 0 else Page(
            f"preview-flows-{offset}", "Заявленные обмены",
        )
        top = page.height if offset == 0 else 26
        top += _text(page, "flow-heading", "Заявленные обмены · маршрут не рассчитан",
                     26, top, 1380, size=17)
        for flow in flows[offset:offset + ENDPOINTS_PER_SIDE]:
            source = names[flow.sender_network_id or ""]
            target = names[flow.receiver_network_id or ""]
            service: str
            if isinstance(flow, DataFlow19):
                service = flow.protocol or "протокол неизвестен"
                if flow.destination_port is not None:
                    service += f"/{flow.destination_port}"
                direction = {"one-way": "→", "two-way": "↔"}.get(flow.mode or "", "·")
            else:
                service = "файлы"
                direction = {"forward": "→", "both": "↔"}.get(flow.direction or "", "·")
                attached_usb = [usb_systems[key] for key in flow.sender_ids
                                if key in usb_systems]
                if attached_usb:
                    source = ", ".join(item.name or "USB-носитель" for item in attached_usb)
                    label = text_lines(
                        f"{source} · прямое подключение к Узлу А; размещение требует проверки", 90,
                    )
                    height = 20 * len(label.splitlines()) + 20
                    page.boxes.append(Box(f"usb-{flow.id}", label, 40, top, 740, height))
                    top += height + 8
            top += _text(page, f"flow-{flow.id}", f"{source} {direction} {target} · {service}",
                         26, top, 1380)
            if not isinstance(flow, DataFlow19):
                for side in flow.sides:
                    summary = _check_summary(side.checks)
                    if summary:
                        network_name = (source if side.usb and side.network_id ==
                                        flow.sender_network_id else names[side.network_id])
                        top += _text(page, f"checks-{flow.id}-{side.network_id}",
                                     f"{network_name}: {summary}", 40, top, 1360)
                    if side.received_files_shared is False and (
                        flow.direction == "both" or side.network_id == flow.receiver_network_id
                    ):
                        group = side.access_group or "группа не уточнена"
                        top += _text(page, f"access-{flow.id}-{side.network_id}",
                                     f"Раздельный доступ · {names[side.network_id]}: {group}",
                                     40, top, 1360)
        page.height = top + 26
        if offset:
            pages.append(page)
    return pages


def layout19(request: Requirements19, result: RuleCalculation) -> dict[str, object]:
    error = diagram_unavailability19(request, result)
    if error:
        raise ValueError(error)
    placement = result.data.placement
    scene = {
        "schema_version": 1, "design_id": request.id, "background": BACKGROUND,
        "font_family": "Open Sans", "profile": "calculated-sinoniks-19",
    }
    if placement is None:
        return {**scene, "placement_pending": True, "rule_rows": [],
                "preview_file_checks": {
                    rule.id: rule.check_directions() for rule in result.files.rules
                },
                "preview_file_access": {
                    rule.id: rule.access_directions() for rule in result.files.rules
                },
                "pages": [asdict(page) for page in _unplaced_topology(request)]}
    context = LayoutContext({system.id: system for system in request.systems}, placement)
    networks = {network.id: network.name or network.id for network in request.networks}
    rules = [context.data(rule, number) for number, rule in enumerate(result.data.rules, 1)]
    rules.extend(context.file(rule, number, networks)
                 for number, rule in enumerate(result.files.rules, len(rules) + 1))
    names = (networks[placement.node_a_network_id], networks[placement.node_b_network_id])
    count = (f"Правил Синоникса: {result.minimum}" if result.minimum is not None else
             f"Предварительный расчёт · минимум {result.lower_bound}")
    used = {endpoint.id for rule in rules for endpoint in [*rule.senders, *rule.receivers]}
    unconnected = [system for system in _topology_systems(request) if system.id not in used]
    extras: list[Endpoint] = []
    overflow: list[System19] = []
    if rules:
        first_arms = {endpoint for rule in rules[:RULES_PER_PAGE] for endpoint in _arms(rule)
                      if not endpoint.usb}
        sizes = {side: sum(endpoint.side == side for endpoint in first_arms) for side in ("A", "B")}
        for system in unconnected:
            side = "A" if system.network_id == placement.node_a_network_id else "B"
            if sizes[side] >= ENDPOINTS_PER_SIDE:
                overflow.append(system)
                continue
            extras.append(Endpoint(side, system.id,
                                   f"{system.name or system.id}\nОбмен не рассчитан"))
            sizes[side] += 1
    pages = [
        _page(rules[start:start + RULES_PER_PAGE], names, count,
              request.existing_firewall_name, extras if start == 0 else None)
        for start in range(0, len(rules), RULES_PER_PAGE)
    ] if rules else _topology_pages(request, placement, names)
    if overflow:
        pages.extend(_topology_pages(request, placement, names, systems=overflow))
    firewall = existing_firewall_details(request)
    return {
        **scene,
        "rule_rows": [rule.table_row() for rule in rules],
        "pages": [asdict(page) for page in pages],
        **({"firewall": firewall} if firewall else {}),
    }
