"""One scene, exact XML geometry, actual systems, directions and bounded pages."""

import json
from xml.etree.ElementTree import fromstring

import pytest
from test_sinoniks import full_input

from partner_agent.sinoniks import plan
from partner_agent.sinoniks_export import export_files, scene_drawio
from partner_agent.sinoniks_layout import layout


def test_xml_and_saved_scene_have_identical_shapes_routes_and_counts():
    design = plan(full_input())
    files = export_files(design)
    scene = json.loads(files["diagram"])
    assert scene == layout(design)
    assert files["drawio"] == scene_drawio(scene)
    assert files == export_files(design)
    assert json.loads(files["json"])["logical_rule_count"] == len(design.rules)
    document = fromstring(files["drawio"])
    for page, model in zip(scene["pages"], document.findall("./diagram/mxGraphModel"), strict=True):
        assert model.get("background") == "#303E48"
        for box in page["boxes"]:
            vertex = model.find(f"./root/mxCell[@id='{box['id']}']")
            assert vertex is not None and vertex.get("value") == box["text"]
            geo = vertex.find("mxGeometry")
            assert all(int(geo.get(key)) == box[key] for key in ("x", "y", "width", "height"))
        for edge in page["edges"]:
            cell = model.find(f"./root/mxCell[@id='{edge['id']}']")
            assert cell.get("source") == edge["source"] and cell.get("target") == edge["target"]
            route = cell.find("mxGeometry")
            xml_points = [route.find("mxPoint[@as='sourcePoint']"),
                          *route.findall("Array/mxPoint"), route.find("mxPoint[@as='targetPoint']")]
            assert [[int(p.get("x")), int(p.get("y"))] for p in xml_points] == edge["points"]


@pytest.mark.parametrize("side", ["A", "B"])
@pytest.mark.parametrize("mode", ["diode", "transport", "filetransfer"])
def test_each_direction_reaches_receiver_through_saved_hardware(side, mode):
    flow = full_input().exchanges[0].model_dump() | {"sender_side": side, "mode": mode}
    if mode == "filetransfer":
        flow.update(protocol=None, destination_port=None)
    design = plan(full_input(firewall="both", exchanges=[flow]))
    page = layout(design)["pages"][0]
    edges = page["edges"]
    assert len(design.rules) == 1
    assert {edge["rule_id"] for edge in edges} == {design.rules[0].id}
    assert all(edge["end_arrow"] == "block" for edge in edges if not edge["dashed"])
    assert all(edge["start_arrow"] == ("open" if mode == "transport" else "none")
               for edge in edges if not edge["dashed"])
    start = "system-" + side + "-1"
    end = "system-" + ("B" if side == "A" else "A") + "-1"
    path = [start]
    while path[-1] != end:
        candidates = [edge["target"] for edge in edges if edge["source"] == path[-1]]
        assert len(candidates) == 1
        assert candidates[0] not in path
        path.append(candidates[0])
    assert {"node-A", "node-B", "core", "firewall-A", "firewall-B"} <= set(path)


def test_f01_keeps_three_sender_cards_but_only_one_logical_rule():
    flow = full_input().exchanges[0].model_dump()
    design = plan(full_input(exchanges=[
        flow | {"sender": name} for name in ("App1", "App2", "App3")
    ]))
    scene = layout(design)
    assert len(design.rules) == 1
    systems = [box for box in scene["pages"][0]["boxes"] if box["kind"] == "system"]
    assert {box["text"] for box in systems} == {"App1", "App2", "App3", "Мониторинг"}
    assert len([b for b in scene["pages"][0]["boxes"] if b["kind"] == "appliance"]) == 1


def test_maximum_project_has_bounded_editable_pages_and_escapes_untrusted_text():
    flow = full_input().exchanges[0].model_dump()
    design = plan(full_input(exchanges=[flow | {"sender": f"<Узел & {index}>",
                                               "receiver": f"Система {index}"}
                                             for index in range(24)]))
    files = export_files(design)
    scene = json.loads(files["diagram"])
    assert len(scene["pages"]) == 8
    assert "&lt;Узел &amp;" in files["drawio"]
    for page in scene["pages"]:
        ids = [box["id"] for box in page["boxes"]] + [edge["id"] for edge in page["edges"]]
        assert len(ids) == len(set(ids)) and len(ids) < 150
        known = {box["id"] for box in page["boxes"]}
        assert all(edge["source"] in known and edge["target"] in known for edge in page["edges"])
        for box in page["boxes"]:
            assert 0 <= box["x"] < box["x"] + box["width"] < page["width"]
            assert 0 <= box["y"] < box["y"] + box["height"] < page["height"]
        for edge in page["edges"]:
            assert all(a[0] == b[0] or a[1] == b[1]
                       for a, b in zip(edge["points"], edge["points"][1:]))


def test_long_labels_and_provenance_fit_without_changing_original_data():
    flow = full_input().exchanges[0].model_dump()
    request = full_input(network_a="А" * 120, network_b="Б" * 120, exchanges=[
        flow | {"sender": "Ш" * 120, "receiver": "W" * 120,
                "requirement_id": f"req-{i}-" + "R" * 69} for i in range(24)
    ])
    design = plan(request)
    original = design.model_dump_json()
    scene = layout(design)
    page = scene["pages"][0]
    summary = next(b["text"] for b in page["boxes"] if b["id"] == "rule-1")
    assert design.rules[0].id not in summary
    assert scene["rule_rows"][0]["requirement_ids"] == design.rules[0].requirement_ids
    rows = json.loads(fromstring(scene_drawio(scene)).get("data-rule-rows"))
    assert rows[0]["requirement_ids"] == design.rules[0].requirement_ids
    for box in page["boxes"]:
        if box["kind"] == "text":
            assert len(box["text"].splitlines()) * (box["font_size"] + 6) <= box["height"]
        assert box["y"] + box["height"] < page["height"]
    assert design.model_dump_json() == original


def test_compact_hardware_and_common_receiver_are_centred_independently_of_sender_count():
    flow = full_input().exchanges[0].model_dump()
    design = plan(full_input(firewall="A",
                             exchanges=[flow | {"sender": f"App{i}"} for i in range(3)]))
    page = layout(design)["pages"][0]
    boxes = {b["id"]: b for b in page["boxes"]}
    def centre(b):
        return b["y"] + b["height"] / 2
    assert (boxes["sinoniks"]["width"], boxes["sinoniks"]["height"]) == (240, 120)
    assert (boxes["firewall-A"]["width"], boxes["firewall-A"]["height"]) == (48, 166)
    assert centre(boxes["sinoniks"]) == centre(boxes["firewall-A"])
    assert centre(boxes["sinoniks"]) == centre(boxes["system-B-1"])
    assert centre(boxes["system-A-2"]) == centre(boxes["system-B-1"])
    assert page["width"] == 1440 and page["height"] < 800
    labels = [b for b in page["boxes"] if "-label-" in b["id"]]
    assert len(labels) == 4
    for label in labels:
        assert len(label["text"].splitlines()) == 1
