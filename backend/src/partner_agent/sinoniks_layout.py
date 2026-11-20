"""Deterministic scene shared by editable XML and the local SVG viewer.

This is a renderer of the saved graph, never a trust-zone or rule calculator.
"""

from base64 import b64encode
from dataclasses import asdict, dataclass, field
from textwrap import wrap
from typing import Any

from partner_agent.sinoniks_contract import Design, Rule

BACKGROUND = "#303E48"
COLORS = ("#E06733", "#35C4AF", "#FFAB4D", "#B9C5CD", "#6FA8DC")
FIREWALL_FLAME = b64encode(
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 40">'
    b'<path fill="#E06733" d="M16 0C21 12 10 14 20 23C24 18 23 13 23 13'
    b'C39 32 28 40 16 40C0 40-4 25 8 14C6 23 12 24 12 24C8 12 18 10 16 0Z"/>'
    b'</svg>'
).decode()


@dataclass
class Box:
    id: str
    text: str
    x: int
    y: int
    width: int
    height: int
    kind: str = "system"
    fill: str = "#4A5D6A"
    stroke: str = "none"
    color: str = "#FFFFFF"
    dashed: bool = False
    font_size: int = 13
    radius: int = 10
    bold: bool = False
    image: str | None = None


@dataclass
class Edge:
    id: str
    source: str
    target: str
    points: list[list[int]]
    color: str
    rule_id: str
    start_arrow: str = "none"
    end_arrow: str = "block"
    dashed: bool = False


@dataclass
class Page:
    id: str
    title: str
    width: int = 1440
    height: int = 1000
    boxes: list[Box] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


def text_lines(text: str, columns: int) -> str:
    return "\n".join(line for paragraph in text.split("\n")
                     for line in (wrap(paragraph, columns, break_on_hyphens=False) or [""]))


def _text(page: Page, identifier: str, text: str, x: int, y: int, width: int,
          *, color: str = "#B9C5CD", size: int = 13) -> int:
    value = text_lines(text, max(8, width // (size // 2 + 2)))
    height = (size + 6) * len(value.splitlines()) + 16
    page.boxes.append(Box(identifier, value, x, y, width, height, "text",
                          "none", "none", color, font_size=size))
    return height


def _protocol(rule: Rule) -> str:
    if rule.mode == "filetransfer":
        return "файлы · метод не задан"
    return f"{rule.protocol or 'протокол ?'} / {rule.destination_port or 'порт ?'}"


def _page(design: Design, rules: list[Rule], start: int) -> Page:
    page = Page(f"rules-{start + 1}", f"Правила {start + 1}–{start + len(rules)}")
    endpoints: dict[tuple[str, str], Box] = {}
    occurrences: dict[tuple[str, str], list[str]] = {}
    for rule in rules:
        side, other = rule.sender_side, "B" if rule.sender_side == "A" else "A"
        for network, name in [*((side, sender) for sender in rule.senders), (other, rule.receiver)]:
            occurrences.setdefault((network, name), []).append(rule.id)
    # Cards determine zone height, never appliance proportions. Both columns share
    # a centre line even when three senders have one common receiver.
    columns: dict[str, list[Box]] = {"A": [], "B": []}
    for side in ("A", "B"):
        for index, (network, name) in enumerate(key for key in occurrences if key[0] == side):
            text = text_lines(name, 25)
            count = len(occurrences[(network, name)])
            height = max(64, len(text.splitlines()) * 21 + 28,
                         (count + 1) * 36 if count > 1 else 0)
            box = Box(f"system-{side}-{index + 1}", text,
                      70 if side == "A" else 1150, 0, 240, height,
                      font_size=15, bold=True)
            endpoints[(side, name)] = box
            columns[side].append(box)
    span = max(240, *(sum(box.height for box in boxes) + max(0, len(boxes) - 1) * 24
                      for boxes in columns.values()))
    zone_top = max(100, 62 + max(len(text_lines(name, 34).splitlines()) * 19
                               for name in design.networks.values()))
    centre = zone_top + 64 + span // 2
    bottom = zone_top + span + 104
    for boxes in columns.values():
        top = centre - (sum(box.height for box in boxes) + max(0, len(boxes) - 1) * 24) // 2
        for box in boxes:
            box.y = top
            top += box.height + 24
    board_height = 80 + 24 * (len(rules) - 1)
    body_top = centre - (board_height + 40) // 2
    page.boxes.extend([
        Box("network-A", text_lines("Сеть A · " + design.networks["A"], 34),
            30, 90, 320, bottom - 90, "zone", "none", COLORS[0], COLORS[0], True),
        Box("network-B", text_lines("Сеть B · " + design.networks["B"], 82),
            584, 90, 836, bottom - 90, "zone", "none", COLORS[1], COLORS[1], True),
        Box("sinoniks", "", 644, body_top, 240, board_height + 40,
            "appliance", "#FFFFFF", COLORS[0], "#303E48"),
    ])
    _text(page, "appliance-title", "СИНОНИКС", 644, body_top - 40, 240,
          color="#FFFFFF", size=16)
    devices: dict[str, Box] = {
        key: Box(key, name, x, body_top + 20, 64, board_height,
                 "device", "#E8EDF0", "none", "#303E48", font_size=12, radius=6)
        for key, name, x in (("node-A", "Узел А", 656), ("core", "Ядро", 732),
                              ("node-B", "Узел В", 808))
    }
    for side in design.firewall_sides:
        identifier = f"firewall-{side}"
        x, y = (560 if side == "A" else 920), centre - 83
        devices[identifier] = Box(identifier, "", x, y, 48, 166,
                                  "firewall", "#354552", "#FFAB4D", radius=2)
        page.boxes.append(Box(identifier + "-flame", "", x + 12, y + 60, 24, 30,
                              "detail", "none", image=FIREWALL_FLAME))
        _text(page, identifier + "-title", "МЭ " + side, x - 12, y - 40, 90,
              color="#FFFFFF", size=15)
        # Native editable brick pattern; no external or private image asset.
        for row in range(1, 7):
            page.boxes.append(Box(f"{identifier}-brick-{row}", "", x + 1, y + row * 23,
                                  46, 1, "detail", "#FFAB4D", radius=0))
            page.boxes.append(Box(f"{identifier}-joint-{row}", "",
                                  x + (16 if row % 2 else 32), y + (row - 1) * 23,
                                  1, 23, "detail", "#FFAB4D", radius=0))
    page.boxes.sort(key=lambda box: box.image is not None)
    page.boxes.extend(devices.values())
    page.boxes.extend(endpoints.values())
    _text(page, "title", f"Синоникс · {page.title} из {len(design.rules)}", 30, 5, 1370,
          color="#FFFFFF", size=22)
    _text(page, "subtitle", "Сети и направления — по сохранённым условиям проекта",
          30, 44, 1370, size=13)

    def endpoint_port(side: str, name: str, rule_id: str) -> tuple[Box, int]:
        box = endpoints[(side, name)]
        ids = occurrences[(side, name)]
        offset = ids.index(rule_id) + 1
        return box, box.y + box.height * offset // (len(ids) + 1)

    chain = [design.connections[0].source, *(edge.target for edge in design.connections)]
    for offset, rule in enumerate(rules):
        color = COLORS[(start + offset) % len(COLORS)]
        port_y = centre + (2 * offset - len(rules) + 1) * 14
        reply = "open" if rule.mode == "transport" else "none"
        other = "B" if rule.sender_side == "A" else "A"
        # Each sender remains an individual system. Shared receiver count stays unchanged.
        pairs: list[tuple[str, str]] = [(rule.sender_side, sender) for sender in rule.senders]
        pairs.append((other, rule.receiver))
        for endpoint_index, (endpoint_side, name) in enumerate(pairs):
            box, endpoint_y = endpoint_port(endpoint_side, name, rule.id)
            is_sender = endpoint_index < len(rule.senders)
            path = ([box.id, *chain[1:-1]] if endpoint_side == "A"
                    else [*chain[1:-1], box.id])
            # Only the endpoint's side is drawn here; internal transfer is added below.
            node_position = path.index("node-A" if endpoint_side == "A" else "node-B")
            path = path[:node_position + 1] if endpoint_side == "A" else path[node_position:]
            if (endpoint_side == "B" and is_sender) or (endpoint_side == "A" and not is_sender):
                path.reverse()
            for leg, (source, target) in enumerate(zip(path, path[1:])):
                source_box = box if source == box.id else devices[source]
                target_box = box if target == box.id else devices[target]
                forward = source_box.x < target_box.x
                sx = source_box.x + (source_box.width if forward else 0)
                tx = target_box.x + (0 if forward else target_box.width)
                sy = endpoint_y if source == box.id else port_y
                ty = endpoint_y if target == box.id else port_y
                channel = (470 + offset * 24 + endpoint_index * 2 if endpoint_side == "A"
                           else 990 + offset * 12 + endpoint_index * 2)
                points = [[sx, sy], [channel, sy], [channel, ty], [tx, ty]]
                if sy == ty:
                    points = [[sx, sy], [tx, ty]]
                page.edges.append(Edge(f"{rule.id}-endpoint-{endpoint_index}-{leg}",
                                       source, target, points, color, rule.id, reply))
            protocol = {"Modbus TCP": "Modbus", "МЭК 104": "IEC104"}.get(
                rule.protocol or "?", rule.protocol or "?")
            label = (f"{start + offset + 1} · файлы" if rule.mode == "filetransfer"
                     else f"{start + offset + 1} {protocol}:{rule.destination_port or '?'}")
            _text(page, f"{rule.id}-label-{endpoint_index}", label,
                  354 if endpoint_side == "A" else 1022, endpoint_y - 38,
                  110 if endpoint_side == "A" else 126, color=color, size=11)
        for index, (source, target) in enumerate((("node-A", "core"), ("core", "node-B"))):
            if rule.sender_side == "B":
                source, target = target, source
            left, right = devices[source], devices[target]
            forward = left.x < right.x
            points = [[left.x + (left.width if forward else 0), port_y],
                      [right.x + (0 if forward else right.width), port_y]]
            page.edges.append(Edge(f"{rule.id}-internal-{index}", source, target, points,
                                   "#B9C5CD", rule.id, "none", "none", dashed=True))
    top = bottom + 20
    top += _text(page, "count", f"Логических правил: {design.logical_rule_count}. "
                 "Минимум по методике 1.9 не подтверждён.",
                 30, top, 1370, color="#FFFFFF", size=16)
    for offset, rule in enumerate(rules):
        number = start + offset + 1
        mode = {"diode": "Односторонний", "transport": "Сессия с ответом",
                "filetransfer": "Файлы"}[rule.mode]
        text = (f"{number}. {rule.service} · {mode} · {_protocol(rule)}\n"
                f"Условия: {rule.policy}")
        top += _text(page, f"rule-{number}", text, 30, top + 10, 1370,
                     color=COLORS[(start + offset) % len(COLORS)]) + 10
    top += _text(page, "scope", "Схема не включает USB и не определяет "
                 "доверие сетей автоматически. "
                 "Проверьте заданное размещение межсетевого экрана. "
                 "Пунктир внутри Синоникса — изолированный перенос через ядро.",
                 30, top + 10, 1370)
    page.height = top + 70
    return page


def layout(design: Design) -> dict[str, Any]:
    pages = [_page(design, design.rules[start:start + 3], start)
             for start in range(0, len(design.rules), 3)]
    return {"schema_version": 1, "design_id": design.id, "background": BACKGROUND,
            "font_family": "Open Sans", "profile": "saved-graph-v2-compact",
            "rule_rows": [{"id": rule.id, "sender_side": rule.sender_side,
                           "senders": rule.senders, "receiver": rule.receiver,
                           "mode": rule.mode, "protocol": rule.protocol,
                           "destination_port": rule.destination_port,
                           "service": rule.service, "policy": rule.policy,
                           "requirement_ids": rule.requirement_ids}
                          for rule in design.rules],
            "pages": [asdict(page) for page in pages]}
