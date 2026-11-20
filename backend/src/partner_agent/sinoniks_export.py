"""Editable draw.io pages rendered from the same topology and rules as JSON."""

from textwrap import wrap
from xml.etree.ElementTree import Element, SubElement, tostring

from partner_agent.sinoniks_contract import Design, Device, Rule

WIDTH = 150
STEP = 175
MARGIN = 30
RULES_PER_PAGE = 3
LINE_HEIGHT = 19
TEXT_PADDING = 24


def _page(file: Element, identifier: str, title: str) -> tuple[Element, Element]:
    diagram = SubElement(file, "diagram", id=identifier, name=title)
    model = SubElement(diagram, "mxGraphModel", {
        "grid": "1", "gridSize": "10", "page": "1", "pageScale": "1",
        "pageWidth": "1400", "pageHeight": "1100",
    })
    root = SubElement(model, "root")
    SubElement(root, "mxCell", id="0")
    SubElement(root, "mxCell", {"id": "1", "parent": "0"})
    return model, root


def _box_lines(text: str, width: int) -> list[str]:
    columns = max(1, (width - TEXT_PADDING) // 12)
    return [
        line
        for paragraph in text.split("\n")
        for line in (wrap(paragraph, width=columns, break_on_hyphens=False) or [""])
    ]


def _box_height(lines: list[str], minimum: int) -> int:
    return max(minimum, TEXT_PADDING + LINE_HEIGHT * len(lines))


def _box(
    root: Element, identifier: str, text: str, x: int, y: int, width: int, height: int,
    *, fill: str = "#ffffff", border: str = "#64748b",
) -> int:
    lines = _box_lines(text, width)
    height = _box_height(lines, height)
    cell = SubElement(root, "mxCell", {
        "id": identifier, "parent": "1", "value": "\n".join(lines), "vertex": "1",
        "style": "rounded=1;whiteSpace=wrap;html=0;fontSize=13;"
        f"spacing=8;fillColor={fill};strokeColor={border};",
    })
    SubElement(cell, "mxGeometry", {
        "x": str(x), "y": str(y), "width": str(width), "height": str(height), "as": "geometry",
    })
    return height


def _edge(
    root: Element, identifier: str, source: str, target: str, *, internal: bool,
    directed: bool = False, session_reply: bool = False,
) -> None:
    start_arrow = "open" if session_reply else "none"
    end_arrow = "block" if directed else "none"
    cell = SubElement(root, "mxCell", {
        "id": identifier, "parent": "1", "source": source, "target": target,
        "edge": "1",
        "style": "edgeStyle=orthogonalEdgeStyle;html=0;"
        f"startArrow={start_arrow};startFill=0;endArrow={end_arrow};"
        f"dashed={int(internal)};fontSize=10;labelBackgroundColor=#ffffff;",
    })
    SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})


def _chain(design: Design) -> list[str]:
    # The canonical connection list already encodes the physical order from A to B.
    return [design.connections[0].source, *(edge.target for edge in design.connections)]


def _render_chain(
    root: Element, design: Design, *, prefix: str, y: int, rule: Rule | None = None,
) -> int:
    chain = _chain(design)
    devices: dict[str, Device] = {device.id: device for device in design.device_nodes}
    labels = {network.id: network.name for network in design.network_nodes}
    labels.update({device.id: device.name for device in design.device_nodes})
    if rule is not None:
        source = f"network-{rule.sender_side}"
        target = "network-B" if rule.sender_side == "A" else "network-A"
        heading = "Источники" if rule.additional_senders else "Источник"
        labels[source] += f"\n{heading}\n" + "\n".join(rule.senders)
        labels[target] += "\nПолучатель\n" + rule.receiver
    height = max(_box_height(_box_lines(label, WIDTH), 115) for label in labels.values())
    for index, identifier in enumerate(chain):
        device = devices.get(identifier)
        fill = "#f1f5f9"
        if device is not None:
            fill = "#fff7ed" if device.kind == "firewall" else "#e0f2fe"
        _box(root, prefix + identifier, labels[identifier],
             MARGIN + STEP * index, y, WIDTH, height, fill=fill)
    reverse = rule is not None and rule.sender_side == "B"
    session_reply = rule is not None and rule.mode == "transport"
    for edge in design.connections:
        source, target = (edge.target, edge.source) if reverse else (edge.source, edge.target)
        _edge(
            root, prefix + edge.id, prefix + source, prefix + target,
            internal=edge.kind == "isolated_internal_transfer", directed=rule is not None,
            session_reply=session_reply,
        )
    return y + height


def drawio(design: Design) -> str:
    file = Element("mxfile", host="app.diagrams.net", type="device")
    model, root = _page(file, "topology", "Размещение")
    width = STEP * len(_chain(design)) - 25
    _box(root, "design-title", "Синоникс · концептуальный проект\n" + design.id,
         MARGIN, 20, width, 65, fill="#f8fafc")
    top = _render_chain(root, design, prefix="", y=140) + MARGIN
    top += MARGIN + _box(root, "legend", "Сплошные линии — сетевые соединения. "
         "Пунктир — изолированный внутренний перенос через ядро.\n"
         "На листах правил закрашенная стрелка показывает инициативу обмена. "
         "Открытая стрелка показывает ответ в той же сессии.", MARGIN, top, width, 75)
    counts = design.counts
    top += MARGIN + _box(root, "counts", f"Исходных требований — {counts.input_requirements}\n"
         f"Логических обменов — {counts.logical_exchanges}\n"
         f"Сетевых пар слотов — {counts.network_slot_pairs}, "
         f"настроек узлов — {counts.network_node_settings}\n"
         f"Файловых политик — {counts.file_transfer_policies}", MARGIN, top, width, 110)
    top += MARGIN + _box(
        root, "warnings", "\n".join(design.warnings), MARGIN, top, width, 120, fill="#fff7ed"
    )
    model.set("pageHeight", str(max(1100, top)))
    for start in range(0, len(design.rules), RULES_PER_PAGE):
        end = min(start + RULES_PER_PAGE, len(design.rules))
        model, root = _page(file, f"rules-{start + 1}", f"Правила {start + 1}–{end}")
        top = MARGIN
        for row, rule in enumerate(design.rules[start : start + RULES_PER_PAGE]):
            number = start + row + 1
            prefix = f"{rule.id}-"
            response = (
                " Открытая стрелка — ответ той же сессии." if rule.mode == "transport" else ""
            )
            label = f"{number}. {rule.id}\n{rule.mode} · {rule.service}.{response}\n"
            label += f"Ограничения — {rule.policy}\nТребования — {', '.join(rule.requirement_ids)}"
            if rule.mode != "filetransfer":
                label += f"\nПротокол — {rule.protocol or 'уточнить'}, "
                label += f"порт получателя — {rule.destination_port or 'уточнить'}"
            header_height = _box(
                root, f"rule-{number}", label, MARGIN, top, width, 135, fill="#f8fafc"
            )
            top = _render_chain(
                root, design, prefix=prefix, y=top + header_height + 20, rule=rule
            ) + MARGIN
        model.set("pageHeight", str(max(1100, top)))
    return tostring(file, encoding="unicode", xml_declaration=True)


def export_files(design: Design) -> dict[str, str]:
    return {
        "json": design.model_dump_json(indent=2) + "\n",
        "drawio": drawio(design),
        "txt": design.explanation + "\n",
    }
