import json
from codecs import BOM_UTF8
from xml.etree.ElementTree import fromstring

import pytest

from partner_agent.sinoniks import DesignInput, clarification_questions, clarify, drawio, plan
from partner_agent.sinoniks import main as design_cli
from partner_agent.sinoniks_export import export_files


def full_input(**changes):
    body = {
        "network_a": "Производство", "network_b": "Офис", "firewall": "B",
        "exchanges": [{
            "sender_side": "A", "sender": "Датчик", "receiver": "Мониторинг",
            "mode": "transport", "service": "Телеметрия", "policy": "Только мониторинг",
            "protocol": "TCP", "destination_port": 443,
        }],
    }
    return DesignInput.model_validate({**body, **changes})


def test_legacy_input_maps_transport_and_preserves_requirement_provenance():
    source = full_input().model_dump()
    flow = source["exchanges"][0]
    flow["mode"] = "default"
    flow["sources"] = [{"kind": "message", "reference": "message-1", "revision": 2}]
    source["schema_version"] = 1
    source["exchanges"] = [flow, {**flow, "requirement_id": "business-demand"}]
    result = plan(DesignInput.model_validate(source))
    assert result.schema_version == 2 and result.rules[0].mode == "transport"
    assert result.rules[0].requirement_ids == ["business-demand", "requirement-1"]
    assert len(result.rules[0].sources) == 1
    assert result.counts.logical_exchanges == 1 and result.counts.input_requirements == 2
    assert result.counts.network_slot_pairs == 1 and result.counts.network_node_settings == 2
    assert result.counts.evaluation_rule_count is None
    assert result.status == "conceptual" and result.warnings


def test_rule_identity_excludes_input_order_and_provenance():
    first = full_input().exchanges[0].model_dump()
    second = {**first, "sender_side": "B", "sender": "Мониторинг", "receiver": "Датчик"}
    forward = plan(full_input(exchanges=[first, second]))
    reverse = plan(full_input(exchanges=[second, first]))
    assert [rule.id for rule in forward.rules] == [rule.id for rule in reverse.rules]
    assert forward.logical_rule_count == 2
    assert forward.counts.network_node_settings == 4


def test_shared_receiver_combines_three_senders_and_preserves_them_in_exports():
    source = full_input().exchanges[0].model_dump()
    exchanges = [
        {
            **source, "mode": "diode", "protocol": "UDP", "destination_port": 514,
            "sender_side": "A", "sender": f"Компьютер {number}",
            "requirement_id": f"logs-{number}",
            "sources": [{"kind": "message", "reference": f"message-{number}"}],
        }
        for number in (3, 1, 2)
    ]
    result = plan(full_input(exchanges=exchanges))
    assert result.logical_rule_count == result.counts.network_slot_pairs == 1
    assert result.counts.input_requirements == 3
    assert result.counts.evaluation_rule_count is None
    rule, = result.rules
    assert rule.senders == ["Компьютер 1", "Компьютер 2", "Компьютер 3"]
    assert rule.requirement_ids == ["logs-1", "logs-2", "logs-3"]
    assert [item.reference for item in rule.sources] == ["message-1", "message-2", "message-3"]
    assert plan(full_input(exchanges=list(reversed(exchanges)))).rules[0].id == rule.id

    files = export_files(result)
    exported_rule, = json.loads(files["json"])["rules"]
    assert exported_rule["sender"] == rule.sender
    assert exported_rule["additional_senders"] == rule.additional_senders
    sender_cells = fromstring(files["drawio"]).findall(".//mxCell[@data-kind='system']")
    assert sender_cells
    for sender in rule.senders:
        assert any(sender in " ".join(cell.attrib["value"].split()) for cell in sender_cells)
        assert sender in files["txt"]


@pytest.mark.parametrize(("first_change", "second_change"), [
    ({}, {"receiver": "Другой получатель"}),
    ({}, {"destination_port": 444}),
    ({}, {"protocol": "UDP"}),
    ({}, {"mode": "diode"}),
    ({}, {"policy": "Другая политика"}),
    ({}, {"sender_side": "B"}),
    ({"destination_port": None}, {"destination_port": None}),
    ({"protocol": None}, {"protocol": None}),
    (
        {"mode": "filetransfer", "protocol": None, "destination_port": None},
        {"mode": "filetransfer", "protocol": None, "destination_port": None},
    ),
])
def test_distinct_or_unconfirmed_destinations_do_not_combine_senders(first_change, second_change):
    source = full_input().exchanges[0].model_dump()
    result = plan(full_input(exchanges=[
        {**source, **first_change},
        {**source, **second_change, "sender": "Другой отправитель"},
    ]))
    assert result.logical_rule_count == 2
    assert all(not rule.additional_senders for rule in result.rules)
    assert all("additional_senders" not in rule.model_dump() for rule in result.rules)


def test_questions_refer_to_specific_missing_fields():
    request = full_input().model_dump()
    request["exchanges"][0]["receiver"] = None
    questions = clarification_questions(DesignInput.model_validate(request))
    assert len(questions) == 1 and questions[0].field == "exchanges.0.receiver"


@pytest.mark.parametrize(("protocol", "port"), [
    ("MQTT", None), ("Modbus TCP", 502), ("МЭК 104", None),
])
def test_industrial_protocol_uses_transport_without_guessing_port(protocol, port):
    source = full_input().exchanges[0].model_dump()
    request = full_input(exchanges=[
        {**source, "mode": None, "protocol": protocol, "destination_port": port},
    ])
    assert not clarification_questions(request)
    rule, = plan(request).rules
    assert rule.mode == "transport"
    assert rule.protocol == protocol
    assert rule.destination_port == port


def test_explicit_one_way_industrial_requirement_needs_clarification():
    source = full_input().exchanges[0].model_dump()
    request = full_input(exchanges=[
        {**source, "mode": "diode", "protocol": "MQTT", "destination_port": None},
    ])
    question, = clarification_questions(request)
    assert question.field == "exchanges.0.mode" and question.reason == "conflict"
    assert request.exchanges[0].mode == "diode"
    with pytest.raises(ValueError, match="уточните"):
        plan(request)


@pytest.mark.parametrize("patch", [
    {"destination_port": True}, {"destination_port": 65536},
    {"sender": "A\nB"}, {"mode": "filetransfer", "protocol": "TCP"},
])
def test_invalid_exchange_fails_before_calculation(patch):
    flow = full_input().exchanges[0].model_dump()
    with pytest.raises(ValueError):
        full_input(exchanges=[{**flow, **patch}])


def test_generated_and_explicit_requirement_ids_cannot_collide():
    flow = full_input().exchanges[0].model_dump()
    with pytest.raises(ValueError, match="unique"):
        full_input(exchanges=[flow, {**flow, "requirement_id": "requirement-1"}])


def test_network_graph_has_no_direct_link_between_contours():
    design = plan(full_input(firewall="both"))
    assert {device.id for device in design.device_nodes} == {
        "node-A", "core", "node-B", "firewall-A", "firewall-B",
    }
    assert len(design.connections) == 6
    for edge in design.connections:
        assert edge.source != "network-A" or edge.target == "firewall-A"
        if "core" in (edge.source, edge.target):
            assert edge.kind == "isolated_internal_transfer"


@pytest.mark.parametrize("damage", ["side", "core", "counter", "coverage", "internal_link"])
def test_saved_result_rejects_inconsistent_topology_and_counts(damage):
    from partner_agent.sinoniks_contract import Design

    result = plan(full_input()).model_dump(mode="json")
    if damage == "side":
        result["device_nodes"][0]["side"] = "B"
    elif damage == "core":
        result["device_nodes"] = [node for node in result["device_nodes"] if node["id"] != "core"]
    elif damage == "counter":
        result["counts"]["network_node_settings"] = 1
    elif damage == "coverage":
        result["counts"]["input_requirements"] = 2
    else:
        edge = next(edge for edge in result["connections"] if edge["source"] == "core")
        edge["kind"] = "network_link"
    with pytest.raises(ValueError):
        Design.model_validate(result)


def test_incomplete_design_requests_information():
    request = DesignInput(exchanges=[{"sender_side": "A"}])
    questions = clarify(request)
    assert any("межсетевой экран" in item for item in questions)
    assert any("получает" in item for item in questions)
    with pytest.raises(ValueError, match="уточните"):
        plan(request)


def test_design_deduplicates_without_merging_permissions_or_directions():
    flow = dict(
        sender_side="A",
        sender="Отправитель",
        receiver="Получатель",
        service="Отчёты",
        mode="filetransfer",
        policy="Только согласованные PDF",
    )
    request = DesignInput.model_validate(
        {
            "network_a": "Внешний & тестовый",
            "network_b": "Внутренний",
            "firewall": "B",
            "exchanges": [
                flow,
                flow,
                {**flow, "policy": "Только согласованные CSV"},
                {**flow, "sender_side": "B"},
            ],
        }
    )
    design = plan(request)
    assert json.loads(design.model_dump_json())["logical_rule_count"] == 3
    assert design.firewall_sides == ["B"]
    pages = fromstring(drawio(design)).findall("./diagram/mxGraphModel/root")
    assert len(pages) == 1
    for page in pages:
        cells = page.findall("mxCell")
        ids = {cell.attrib["id"] for cell in cells}
        assert len(ids) == len(cells)
        assert all(
            cell.attrib[key] in ids
            for cell in cells
            for key in ("parent", "source", "target")
            if key in cell.attrib
        )
    network = pages[0].find("mxCell[@id='network-A']")
    assert network is not None
    assert " ".join(network.get("value", "").splitlines()) == "Сеть A · " + request.network_a
    assert all(pages[0].find(f"mxCell[@id='rule-{number}']") is not None for number in (1, 2, 3))


@pytest.mark.parametrize("prefix", [b"", BOM_UTF8], ids=["utf8", "utf8-bom"])
def test_cli_writes_the_exact_utf8_bytes_returned_by_the_exporter(tmp_path, monkeypatch, prefix):
    request = full_input()
    source = tmp_path / "input.json"
    target = tmp_path / "design"
    source.write_bytes(prefix + request.model_dump_json().encode("utf-8"))
    monkeypatch.setattr("sys.argv", ["sinoniks", str(source), str(target)])

    design_cli()

    expected = export_files(plan(request))
    assert {path.name: path.read_bytes() for path in target.iterdir()} == {
        f"design.{extension}": content.encode("utf-8") for extension, content in expected.items()
    }
