"""Editable XML and a saved browser scene from one deterministic layout."""

import json
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

from partner_agent.sinoniks_contract import Design
from partner_agent.sinoniks_layout import layout


def _box(root: Element, box: dict[str, Any]) -> None:
    style = (f"rounded=1;absoluteArcSize=1;arcSize={box.get('radius', 10) * 2};"
             "whiteSpace=wrap;html=0;align=left;verticalAlign=top;"
             f"spacing=12;fillColor={box['fill']};strokeColor={box['stroke']};"
             f"fontColor={box['color']};fontSize={box['font_size']};fontFamily=Open Sans;"
             f"fontStyle={int(box.get('bold', False))};strokeWidth=2;dashed={int(box['dashed'])};")
    if box.get("image"):
        style += f"shape=image;image=data:image/svg+xml,{box['image']};"
    cell = SubElement(root, "mxCell", {"id": box["id"], "parent": "1", "value": box["text"],
                                      "vertex": "1", "style": style, "data-kind": box["kind"]})
    SubElement(cell, "mxGeometry", {key: str(box[key]) for key in (
        "x", "y", "width", "height",
    )} | {"as": "geometry"})


def _edge(root: Element, edge: dict[str, Any], boxes: dict[str, Any]) -> None:
    first, last = edge["points"][0], edge["points"][-1]
    source, target = boxes[edge["source"]], boxes[edge["target"]]
    style = ("edgeStyle=none;orthogonal=1;rounded=0;html=0;strokeWidth=2;"
             f"strokeColor={edge['color']};startArrow={edge['start_arrow']};startFill=0;"
             f"endArrow={edge['end_arrow']};dashed={int(edge['dashed'])};"
             f"exitX={(first[0] - source['x']) / source['width']};"
             f"exitY={(first[1] - source['y']) / source['height']};exitPerimeter=0;"
             f"entryX={(last[0] - target['x']) / target['width']};"
             f"entryY={(last[1] - target['y']) / target['height']};entryPerimeter=0;")
    cell = SubElement(root, "mxCell", {"id": edge["id"], "parent": "1", "edge": "1",
                                      "style": style, "source": edge["source"],
                                      "target": edge["target"], "data-rule-id": edge["rule_id"]})
    geometry = SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
    for point, name in ((first, "sourcePoint"), (last, "targetPoint")):
        SubElement(geometry, "mxPoint", {"x": str(point[0]), "y": str(point[1]), "as": name})
    points = SubElement(geometry, "Array", {"as": "points"})
    for x, y in edge["points"][1:-1]:
        SubElement(points, "mxPoint", {"x": str(x), "y": str(y)})


def scene_drawio(scene: dict[str, Any]) -> str:
    file = Element("mxfile", {"host": "app.diagrams.net", "type": "device",
                             "data-rule-rows": json.dumps(scene.get("rule_rows", []),
                                                          ensure_ascii=False)})
    for page in scene["pages"]:
        diagram = SubElement(file, "diagram", id=page["id"], name=page["title"])
        model = SubElement(diagram, "mxGraphModel", {
            "grid": "1", "gridSize": "10", "page": "1", "pageScale": "1",
            "pageWidth": str(page["width"]), "pageHeight": str(page["height"]),
            "background": scene["background"],
        })
        root = SubElement(model, "root")
        SubElement(root, "mxCell", id="0")
        SubElement(root, "mxCell", {"id": "1", "parent": "0"})
        boxes = {box["id"]: box for box in page["boxes"]}
        backgrounds = {"zone", "appliance"}
        for box in page["boxes"]:
            if box["kind"] in backgrounds:
                _box(root, box)
        for edge in page["edges"]:
            _edge(root, edge, boxes)
        for box in sorted(page["boxes"], key=lambda item: item["kind"] == "detail"):
            if box["kind"] not in backgrounds:
                _box(root, box)
    return tostring(file, encoding="unicode", xml_declaration=True)


def drawio(design: Design) -> str:
    return scene_drawio(layout(design))


def diagram_artifacts(scene: dict[str, Any]) -> dict[str, str]:
    return {
        "drawio": scene_drawio(scene),
        "diagram": json.dumps(scene, ensure_ascii=False, separators=(",", ":")),
    }


def export_files(design: Design) -> dict[str, str]:
    return {
        **diagram_artifacts(layout(design)),
        "json": design.model_dump_json(indent=2) + "\n",
        "txt": design.explanation + "\n",
    }
