import json
from xml.etree.ElementTree import fromstring

import pytest
from pydantic import ValidationError

from partner_agent.sinoniks import DesignInput, clarification_questions, plan
from partner_agent.sinoniks_contract import Design
from partner_agent.sinoniks_export import export_files


def exchange(**overrides):
    return {
        "sender_side": "A",
        "sender": "Сборщик показаний",
        "receiver": "Архив",
        "mode": "diode",
        "service": "Показания датчиков",
        "policy": "Только телеметрия",
        "protocol": "UDP",
        "destination_port": 9200,
        **overrides,
    }


def conditions(**overrides):
    return {
        "network_a": "Измерительный контур",
        "network_b": "Отчётный контур",
        "firewall": "B",
        "exchanges": [exchange()],
        **overrides,
    }


@pytest.mark.parametrize(
    ("firewall", "side", "expected"),
    [
        pytest.param(
            "A",
            "A",
            ("network-A", "firewall-A", "node-A", "core", "node-B", "network-B"),
            id="T01-firewall-near-sender",
        ),
        pytest.param(
            "B",
            "A",
            ("network-A", "node-A", "core", "node-B", "firewall-B", "network-B"),
            id="T02-firewall-near-receiver",
        ),
        pytest.param(
            "both",
            "A",
            ("network-A", "firewall-A", "node-A", "core", "node-B", "firewall-B", "network-B"),
            id="T03-firewalls-at-both-boundaries",
        ),
        pytest.param(
            "B",
            "B",
            ("network-A", "node-A", "core", "node-B", "firewall-B", "network-B"),
            id="T04-reverse-flow-preserves-physical-placement",
        ),
    ],
)
def test_two_contour_project_preserves_each_trust_boundary(firewall, side, expected):
    result = plan(
        DesignInput.model_validate(
            conditions(firewall=firewall, exchanges=[exchange(sender_side=side)])
        )
    )
    assert tuple((edge.source, edge.target) for edge in result.connections) == tuple(
        zip(expected, expected[1:])
    )
    assert {
        (edge.source, edge.target)
        for edge in result.connections
        if edge.kind == "isolated_internal_transfer"
    } == {("node-A", "core"), ("core", "node-B")}


@pytest.mark.parametrize(
    ("changes", "rejected_field"),
    [
        pytest.param({"firewall": "DMZ"}, "firewall", id="T05-unknown-placement"),
        pytest.param({"network_c": "Третий контур"}, "network_c", id="T06-extra-network"),
    ],
)
def test_unsupported_topology_fails_before_any_result(changes, rejected_field):
    with pytest.raises(ValidationError) as failure:
        DesignInput.model_validate(conditions(**changes))
    assert failure.value.errors()[0]["loc"] == (rejected_field,)


@pytest.mark.parametrize(
    ("exchanges", "slots", "files", "covered_per_rule"),
    [
        pytest.param(
            [exchange(mode="transport")],
            1,
            0,
            [1],
            id="R01-reply-traffic-belongs-to-existing-session",
        ),
        pytest.param(
            [exchange(mode="transport"), exchange(mode="transport", sender_side="B")],
            2,
            0,
            [1, 1],
            id="R02-independent-reverse-initiative",
        ),
        pytest.param(
            [exchange(mode="diode"), exchange(mode="transport")],
            2,
            0,
            [1, 1],
            id="R03-diode-and-transport-are-distinct",
        ),
        pytest.param(
            [
                exchange(
                    mode="filetransfer", protocol=None, destination_port=None, sender_side=side
                )
                for side in ("A", "B")
            ],
            0,
            2,
            [1, 1],
            id="R04-separate-file-directions",
        ),
        pytest.param(
            [exchange(requirement_id="collect"), exchange(requirement_id="observe")],
            1,
            0,
            [2],
            id="R05-duplicate-flow-retains-both-requirements",
        ),
        pytest.param(
            [exchange(), exchange(policy="Только отдельный набор показаний")],
            2,
            0,
            [1, 1],
            id="R06-different-policies",
        ),
        pytest.param(
            [exchange(), exchange(receiver="Резервный архив")],
            2,
            0,
            [1, 1],
            id="R07-different-receivers",
        ),
        pytest.param(
            [exchange(protocol="TCP"), exchange(protocol="UDP"), exchange(destination_port=9201)],
            3,
            0,
            [1, 1, 1],
            id="R08-protocols-and-ports-protect-distinct-flows",
        ),
    ],
)
def test_conceptual_counts_preserve_exchange_constraints(exchanges, slots, files, covered_per_rule):
    result = plan(DesignInput.model_validate(conditions(exchanges=exchanges)))
    assert result.counts.input_requirements == len(exchanges)
    assert result.counts.logical_exchanges == slots + files
    assert result.counts.network_slot_pairs == slots
    assert result.counts.network_node_settings == slots * 2
    assert result.counts.file_transfer_policies == files
    assert result.counts.evaluation_rule_count is None
    assert sorted(len(rule.requirement_ids) for rule in result.rules) == covered_per_rule
    assert {identifier for rule in result.rules for identifier in rule.requirement_ids} == {
        item.get("requirement_id") or f"requirement-{index}"
        for index, item in enumerate(exchanges, 1)
    }


@pytest.mark.parametrize(
    ("changes", "field", "reason"),
    [
        pytest.param({"network_a": None}, "network_a", "missing", id="Q01-missing-network"),
        pytest.param(
            {"network_b": "ИЗМЕРИТЕЛЬНЫЙ КОНТУР"},
            "network_b",
            "conflict",
            id="Q02-network-label-conflict",
        ),
        pytest.param({"firewall": None}, "firewall", "missing", id="Q03-firewall-boundary"),
        pytest.param(
            {"exchanges": [exchange(sender_side=None)]},
            "exchanges.0.sender_side",
            "missing",
            id="Q04-unknown-direction",
        ),
        pytest.param(
            {"exchanges": [exchange(receiver=None)]},
            "exchanges.0.receiver",
            "missing",
            id="Q05-unknown-receiver",
        ),
        pytest.param(
            {"exchanges": [exchange(policy=None)]},
            "exchanges.0.policy",
            "missing",
            id="Q06-unknown-policy",
        ),
    ],
)
def test_incomplete_conditions_request_correction_before_generating_design(changes, field, reason):
    request = DesignInput.model_validate(conditions(**changes))
    assert [(question.field, question.reason) for question in clarification_questions(request)] == [
        (field, reason)
    ]
    with pytest.raises(ValueError, match="уточните"):
        plan(request)
    baseline = conditions()
    corrected = request.model_dump()
    key = next(iter(changes))
    corrected[key] = baseline[key]
    assert plan(DesignInput.model_validate(corrected)).status == "conceptual"


def test_mixed_design_exports_share_the_corrected_input_and_rule_coverage():
    telemetry = exchange(requirement_id="telemetry")
    request = conditions(
        exchanges=[
            telemetry,
            {**telemetry, "requirement_id": "telemetry-repeat"},
            exchange(
                requirement_id="status",
                sender="Диагностика",
                receiver="Сервис статуса",
                mode="transport",
                service="Состояние оборудования",
                policy="Только чтение",
                protocol="TCP",
                destination_port=443,
            ),
            exchange(
                requirement_id="archive-ack",
                sender_side="B",
                sender="Архив",
                receiver="Сборщик показаний",
                service="Квитанции архива",
                policy="Только квитанции",
                destination_port=9300,
            ),
            exchange(
                requirement_id="reports",
                sender="Выгрузка",
                receiver=None,
                mode="filetransfer",
                service="Отчёты",
                policy="Только PDF",
                protocol=None,
                destination_port=None,
            ),
        ]
    )
    draft = DesignInput.model_validate(request)
    assert [question.field for question in clarification_questions(draft)] == [
        "exchanges.4.receiver"
    ]
    with pytest.raises(ValueError, match="уточните"):
        plan(draft)

    corrected = draft.model_dump()
    corrected["exchanges"][4]["receiver"] = "Хранилище отчётов"
    result = plan(DesignInput.model_validate(corrected))
    assert result.counts.input_requirements == 5
    assert result.counts.logical_exchanges == 4
    assert result.counts.network_slot_pairs == 3
    assert result.counts.network_node_settings == 6
    assert result.counts.file_transfer_policies == 1
    assert result.counts.evaluation_rule_count is None
    assert sorted(rule.requirement_ids for rule in result.rules) == [
        ["archive-ack"],
        ["reports"],
        ["status"],
        ["telemetry", "telemetry-repeat"],
    ]

    artifacts = export_files(result)
    assert Design.model_validate_json(artifacts["json"]) == result
    pages = fromstring(artifacts["drawio"]).findall("diagram")
    assert [page.get("id") for page in pages] == ["rules-1", "rules-4"]
    labels = [
        cell.get("value", "")
        for page in pages
        for cell in page.iter("mxCell")
        if cell.get("id", "") in {"rule-1", "rule-2", "rule-3", "rule-4"}
    ]
    for rule, label in zip(result.rules, labels, strict=True):
        assert rule.service in label
        rows = json.loads(fromstring(artifacts["drawio"]).get("data-rule-rows"))
        assert any(row["id"] == rule.id and row["requirement_ids"] == rule.requirement_ids
                   for row in rows)
        assert f"[{rule.id}]" in artifacts["txt"]
