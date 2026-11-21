"""Synthetic F02/F07 and USB variants: native, legible, directed engineering objects."""

import json
from copy import deepcopy
from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest

from partner_agent.sinoniks_calculation import solve_project
from partner_agent.sinoniks_contract19 import Requirements19
from partner_agent.sinoniks_export import scene_drawio
from partner_agent.sinoniks_layout19 import layout19


def test_known_server_without_a_calculated_flow_remains_visible_without_fake_edges():
    from test_sinoniks_contract19 import data_project
    raw = data_project()
    original = Requirements19.model_validate(raw)
    original_scene = layout19(original, solve_project(original))
    raw["systems"].append({"id": "garage", "name": "Сервер автомастерской",
                           "network_id": "office"})
    request = Requirements19.model_validate(raw)
    scene = layout19(request, solve_project(request))
    page = scene["pages"][0]
    server = next(box for box in page["boxes"] if "автомастерской" in box["text"])
    assert server["x"] < 400
    assert "Обмен не рассчитан" in server["text"]
    assert all(server["id"] not in (edge["source"], edge["target"]) for edge in page["edges"])
    assert scene["rule_rows"] == original_scene["rule_rows"]
    assert len(page["edges"]) == len(original_scene["pages"][0]["edges"])
    assert "автомастерской" in scene_drawio(scene)


def test_many_unconnected_devices_are_preserved_on_bounded_supplementary_pages():
    from test_sinoniks_contract19 import data_project
    raw = data_project()
    raw["systems"] += [{"id": f"garage-{i}", "name": f"Дополнительный-{i}",
                        "network_id": "office"} for i in range(40)]
    request = Requirements19.model_validate(raw)
    scene = layout19(request, solve_project(request))
    text = scene_drawio(scene)
    for i in range(40):
        assert f"Дополнительный-{i}" in text
    assert len(scene["pages"]) == 3
    assert all(sum(box["id"].startswith("endpoint-") for box in page["boxes"]) <= 32
               for page in scene["pages"])
    assert all(not page["edges"] for page in scene["pages"][1:])


def fixture(name):
    return json.loads((Path(__file__).parent / f"fixtures/network-{name}.json").read_text(
        encoding="utf-8",
    ))


@pytest.mark.parametrize("direction", ["forward", "reverse", "both"])
def test_usb_terminal_has_the_direction_of_the_actual_file_exchange(direction):
    raw = fixture("f07")
    flow = raw["flows"][-1]
    flow["direction"] = "both" if direction == "both" else "forward"
    if direction == "reverse":
        flow["sender_network_id"], flow["receiver_network_id"] = (
            flow["receiver_network_id"], flow["sender_network_id"],
        )
        flow["sender_ids"], flow["receiver_ids"] = flow["receiver_ids"], flow["sender_ids"]
    flow["sides"][1].update(manual=True, manual_protocol="SFTP", automations=[])
    raw["flows"] = [flow]
    request = Requirements19.model_validate(raw)
    result = solve_project(request)
    assert result.minimum == 1
    scene = layout19(request, result)
    edge = next(e for e in scene["pages"][0]["edges"] if e["id"].endswith("-usb"))
    assert {edge["source"], edge["target"]} == {"usb-1", "node-A"}
    assert edge["end_arrow"] == "block"
    assert edge["start_arrow"] == ("open" if direction == "both" else "none")
    assert edge["target"] == ("node-A" if direction == "forward" else "usb-1")
    cell = fromstring(scene_drawio(scene)).find(f".//mxCell[@id='{edge['id']}']")
    assert f"startArrow={edge['start_arrow']};" in cell.attrib["style"]
    assert "endArrow=block;" in cell.attrib["style"]


@pytest.mark.parametrize("name", ["f02", "f07"])
def test_named_legend_explains_each_rule_without_changing_the_calculation(name):
    request = Requirements19.model_validate(fixture(name))
    snapshot = request.model_dump_json()
    result = solve_project(request)
    scene = layout19(request, result)
    assert result.minimum == len(scene["rule_rows"]) == 3
    boxes = {box["id"]: box for box in scene["pages"][0]["boxes"]}
    assert boxes["count"]["text"] == "Правил Синоникса: 3"
    for row in scene["rule_rows"]:
        text = boxes[f"rule-{row['number']}"]["text"].replace("\n", " ")
        for name in [*row["senders"], row["receiver"]]:
            assert name in text
        assert ("↔" if row["two_way"] else "→") in text
    assert request.model_dump_json() == snapshot


@pytest.mark.parametrize("name", ["f02", "f07"])
def test_template_palette_orthogonal_geometry_and_editable_xml_share_one_scene(name):
    request = Requirements19.model_validate(fixture(name))
    scene = layout19(request, solve_project(request))
    assert scene["background"] == "#303E48" and scene["font_family"] == "Open Sans"
    root = fromstring(scene_drawio(scene))
    assert json.loads(root.attrib["data-rule-rows"]) == scene["rule_rows"]
    for page, model in zip(scene["pages"], root.findall("./diagram/mxGraphModel"), strict=True):
        boxes = {box["id"]: box for box in page["boxes"]}
        assert boxes["open-zone"]["stroke"] == "#E06733"
        assert boxes["closed-zone"]["stroke"] == "#35C4AF"
        assert boxes["closed-zone"]["x"] <= boxes["firewall"]["x"] + boxes["firewall"]["width"]
        assert boxes["firewall"]["x"] <= boxes["closed-zone"]["x"]
        assert boxes["closed-zone"]["x"] < boxes["sinoniks"]["x"]
        assert boxes["node-A"]["x"] < boxes["core"]["x"] < boxes["node-B"]["x"]
        for box in page["boxes"]:
            assert 0 <= box["x"] < box["x"] + box["width"] < page["width"]
            assert 0 <= box["y"] < box["y"] + box["height"] < page["height"]
            cell = model.find(f"./root/mxCell[@id='{box['id']}'][@vertex='1']")
            assert cell is not None and cell.attrib["value"] == box["text"]
            assert "fontFamily=Open Sans;" in cell.attrib["style"]
            if box["kind"] == "system":
                assert box["fill"] == "#4A5D6A"
        sockets = {}
        colors = {}
        for edge in page["edges"]:
            assert all(a[0] == b[0] or a[1] == b[1]
                       for a, b in zip(edge["points"], edge["points"][1:]))
            colors.setdefault(edge["rule_id"], set()).add(edge["color"])
            cell = model.find(f"./root/mxCell[@id='{edge['id']}'][@edge='1']")
            assert cell is not None and cell.get("source") == edge["source"]
            assert cell.get("target") == edge["target"]
            geometry = cell.find("mxGeometry")
            points = [geometry.find("mxPoint[@as='sourcePoint']"),
                      *geometry.findall("Array/mxPoint"),
                      geometry.find("mxPoint[@as='targetPoint']")]
            assert [[int(p.get("x")), int(p.get("y"))] for p in points] == edge["points"]
            for endpoint, point in ((edge["source"], edge["points"][0]),
                                    (edge["target"], edge["points"][-1])):
                if endpoint.startswith("endpoint-"):
                    sockets.setdefault(endpoint, []).append(point[1])
        assert all(len(values) == 1 for values in colors.values())
        for positions in sockets.values():
            assert all(b - a >= 30 for a, b in zip(sorted(positions), sorted(positions)[1:]))
        labels = [box for box in page["boxes"] if "-label-" in box["id"]]
        if "usb-1" in boxes:
            usb = boxes["usb-1"]
            label = next(box for box in labels if box["id"].endswith("-label-usb"))
            assert label["y"] + label["height"] < usb["y"]
        for index, left in enumerate(labels):
            for right in labels[index + 1:]:
                assert (left["x"] + left["width"] <= right["x"]
                        or right["x"] + right["width"] <= left["x"]
                        or left["y"] + left["height"] <= right["y"]
                        or right["y"] + right["height"] <= left["y"])


def test_three_usb_rules_have_distinct_legible_labels_above_the_medium():
    raw = fixture("f07")
    flow = raw["flows"][-1]
    raw["flows"] = []
    for number in range(3):
        variant = deepcopy(flow)
        variant["id"] = f"usb-group-{number}"
        variant["sides"][1].update(received_files_shared=False, access_group=f"group-{number}")
        raw["flows"].append(variant)
    request = Requirements19.model_validate(raw)
    result = solve_project(request)
    assert result.minimum == 3
    page = layout19(request, result)["pages"][0]
    usb = next(box for box in page["boxes"] if box["id"] == "usb-1")
    labels = [box for box in page["boxes"] if box["id"].endswith("-label-usb")]
    assert len(labels) == 3
    assert all(label["y"] + label["height"] < usb["y"] for label in labels)
    assert all(b["y"] - a["y"] >= a["height"] + 4 for a, b in zip(labels, labels[1:]))
    paths = [edge for edge in page["edges"] if edge["id"].endswith("-usb")]
    assert len({edge["points"][0][0] for edge in paths}) == 3
    assert all(label["x"] > max(edge["points"][0][0] for edge in paths) for label in labels)
