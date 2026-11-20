import copy
import json
from pathlib import Path

import pytest

from partner_agent.benchmark_audit import audit, compare_project, main


def project():
    return {
        "schema_version": "1.1",
        "status": "completed",
        "networks": [{"id": "o", "trust": "open"}, {"id": "c", "trust": "closed"}],
        "firewall": {"side": "open"},
        "synonyx": {
            "location": "closed",
            "node_a_faces": "open",
            "node_b_faces": "closed",
            "node_a_network_id": "o",
            "node_b_network_id": "c",
        },
        "rules_count": 1,
        "confirmed_rules_count": 1,
        "file_users_count": 0,
        "rules": [
            {
                "id": 1,
                "type": "data",
                "from": "o",
                "to": "c",
                "mode": "two-way",
                "protocol": "Modbus TCP",
                "port": 502,
            }
        ],
    }


def test_network_ids_names_and_rule_order_are_not_semantic_differences():
    expected = project()
    actual = copy.deepcopy(expected)
    actual["networks"][0].update(id="new-office", name="Office")
    actual["networks"][1].update(id="new-plant", name="Plant")
    actual["synonyx"].update(node_a_network_id="new-office", node_b_network_id="new-plant")
    actual["rules"][0].update(id=44, **{"from": "new-office", "to": "new-plant"})
    assert compare_project(expected, actual) == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("rules_count", True),
        ("file_users_count", None),
        ("status", "preliminary"),
        ("confirmed_rules_count", None),
        ("schema_version", 3),
    ],
)
def test_counts_unknowns_and_schema_are_not_silently_accepted(key, value):
    actual = project()
    actual[key] = value
    assert compare_project(project(), actual)


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol", "UDP"),
        ("mode", "one-way"),
        ("port", 503),
        ("to", "o"),
    ],
)
def test_rule_count_alone_cannot_hide_wrong_rules(field, value):
    actual = project()
    actual["rules"][0][field] = value
    assert compare_project(project(), actual)


def test_missing_port_stays_unknown_instead_of_becoming_a_default():
    expected = project()
    expected["rules"][0].update(protocol="MQTT", port=None)
    assert compare_project(expected, copy.deepcopy(expected)) == []
    actual = copy.deepcopy(expected)
    actual["rules"][0]["port"] = 1883
    assert "rule_multiset_mismatch" in compare_project(expected, actual)


def test_duplicate_rules_and_wrong_orientation_are_caught():
    expected = project()
    expected["rules"].append({**expected["rules"][0], "port": 504})
    expected.update(rules_count=2, confirmed_rules_count=2)
    actual = copy.deepcopy(expected)
    actual["rules"][1] = copy.deepcopy(actual["rules"][0])
    assert "rule_multiset_mismatch" in compare_project(expected, actual)
    actual = copy.deepcopy(expected)
    actual["synonyx"]["node_a_network_id"] = "c"
    assert "node_network_orientation" in compare_project(expected, actual)


def file_project():
    result = project()
    result["file_users_count"] = 1
    result["rules"] = [
        {
            "type": "file",
            "from": "o",
            "to": "c",
            "mode": "one-way",
            "protocol": {"o": "USB-носитель на Узле А", "c": "SMB, автоматически при поступлении"},
            "checks": ["ICAP: внешний антивирус"],
        }
    ]
    return result


def test_explicit_file_labels_are_normalized_without_guessing():
    expected, actual = file_project(), file_project()
    actual["rules"][0]["protocol"]["c"] = "SMB, PUSH"
    assert compare_project(expected, actual) == []
    actual["rules"][0]["protocol"]["c"] = "SMB, PULL"
    assert "rule_multiset_mismatch" in compare_project(expected, actual)
    expected["rules"][0]["protocol"]["c"] = "SMB, автоматически"
    assert compare_project(expected, actual) == ["reference_ambiguous_file_automation"]


def test_wrong_usb_attachment_or_missing_external_check_cannot_pass():
    expected, actual = file_project(), file_project()
    actual["rules"][0]["checks"] = []
    assert "rule_multiset_mismatch" in compare_project(expected, actual)
    actual = file_project()
    actual["rules"][0]["protocol"]["o"] = "USB-носитель на Узле В"
    assert compare_project(expected, actual)


def test_all_cases_and_follow_up_are_visible_missing_and_manual_stay_open(tmp_path):
    benchmark = {
        "items": [
            {"id": "F01", "group": "basic", "query": "private-query", "expected": project()},
            {"id": "F02", "group": "full", "expected": project()},
            {
                "id": "N01",
                "group": "incomplete",
                "must_ask": ["private-question"],
                "follow_up": {"expected": project()},
            },
            {"id": "Q01", "group": "facts", "expected_answer": "private-answer"},
        ]
    }
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(benchmark))
    (tmp_path / "F01.json").write_text(json.dumps(project()))
    report = audit(path, tmp_path)
    assert report["structural_totals"] == {"pass": 1, "fail": 0, "missing": 2, "manual_review": 2}
    assert report["benchmark_cases"] == 4 and len(report["phases"]) == 5
    assert not report["acceptance_complete"]
    raw = json.dumps(report)
    assert not any(
        secret in raw for secret in ["private-query", "private-question", "private-answer"]
    )
    assert all(
        not row["model_dialogue_verified"] and not row["diagram_verified"]
        for row in report["phases"]
    )


@pytest.mark.parametrize("identifier", ["../../secret", "/file", "F01.json", ""])
def test_benchmark_ids_cannot_be_paths(tmp_path, identifier):
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps({"items": [{"id": identifier, "expected": project()}]}))
    with pytest.raises(ValueError):
        audit(path, tmp_path)


def test_output_is_exclusive_and_partial_evidence_has_nonzero_status(tmp_path: Path):
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text(json.dumps({"items": [{"id": "F01", "expected": project()}]}))
    output = tmp_path / "report.json"
    args = ["--benchmark", str(benchmark), "--results", str(tmp_path), "--output", str(output)]
    assert main(args) == 1
    original = output.read_bytes()
    assert main(args) == 2 and output.read_bytes() == original
    (tmp_path / "F01.json").write_text(json.dumps(project()))
    output.unlink()
    assert main(args) == 0
    assert not json.loads(output.read_bytes())["acceptance_complete"]


def test_reference_ambiguity_is_manual_review_without_fabricated_failure(tmp_path):
    expected, actual = file_project(), file_project()
    expected["rules"][0]["protocol"]["c"] = "SMB, автоматически"
    benchmark = tmp_path / "benchmark.json"
    benchmark.write_text(json.dumps({"items": [{"id": "F01", "expected": expected}]}))
    (tmp_path / "F01.json").write_text(json.dumps(actual))
    report = audit(benchmark, tmp_path)
    assert report["structural_totals"]["manual_review"] == 1
    assert report["structural_totals"]["pass"] == 0
