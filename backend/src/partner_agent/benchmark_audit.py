"""Compare saved expert JSON against private examples without running a model."""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

MAX_BYTES = 2_000_000
CASE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,49}")


class ReferenceReview(ValueError):
    """A reference leaves method direction unspecified; do not guess an oracle."""


def compare_native_export(expert: dict[str, Any], native: Element) -> list[str]:
    expected = expert.get("rules")
    metadata = native.get("data-rule-rows")
    if not isinstance(expected, list) or metadata is None:
        return ["export_rules_unverified"] if expected else []
    actual = json.loads(metadata)
    fields = ("from_network_id", "to_network_id", "checks_by_network")
    if native.get("data-placement-pending") == "true":
        if (expert.get("synonyx") is not None or actual != []
                or native.findall(".//mxCell[@edge='1']")):
            return ["export_preview_mismatch"]
        known_file_checks = {
            rule["rule_id"]: [
                {field: lane[field] for field in fields} for lane in rule["directions"]
            ]
            for rule in expected if rule["type"] == "file" and rule.get("checks")
        }
        preview_checks = json.loads(native.get("data-preview-file-checks", "{}"))
        if not isinstance(preview_checks, dict) or any(
            preview_checks.get(identifier) != directions
            for identifier, directions in known_file_checks.items()
        ):
            return ["export_file_checks_mismatch"]
        return []
    if not isinstance(actual, list) or len(actual) != len(expected):
        return ["export_rule_count_mismatch"]
    issues = []
    for rule, row in zip(expected, actual, strict=True):
        if any((rule["id"] != row["number"], rule["rule_id"] != row["id"],
                rule["from"] != row["from_network_id"], rule["to"] != row["to_network_id"],
                (rule["mode"] == "two-way") != row["two_way"])):
            issues.append("export_rule_mismatch")
        if rule["type"] == "data" and (rule["protocol"], rule["port"]) != (
            row["protocol"], row["destination_port"],
        ):
            issues.append("export_protocol_mismatch")
        if rule["type"] == "file" and any(rule.get("checks", [])):
            directions = [{field: lane[field] for field in fields} for lane in rule["directions"]]
            if directions != row.get("file_check_directions"):
                issues.append("export_file_checks_mismatch")
    return list(dict.fromkeys(issues))


def read_json(path: Path) -> tuple[Any, str]:
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Input exceeds size limit")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def finite(value: str) -> None:
        raise ValueError("Nonfinite JSON")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=finite), hashlib.sha256(
        raw
    ).hexdigest()


def _networks(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, str]:
    wanted, served = expected.get("networks"), actual.get("networks")
    if not isinstance(wanted, list) or not isinstance(served, list) or len(wanted) != len(served):
        raise ValueError("network_structure")
    if len(wanted) != 2:
        raise ValueError("network_structure")
    if not all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in wanted + served):
        raise ValueError("network_structure")
    if len({row["id"] for row in served}) != len(served):
        raise ValueError("network_structure")
    mapping = {}
    for row in wanted:
        candidates = [item for item in served if item.get("trust") == row.get("trust")]
        if len(candidates) != 1 or row.get("trust") not in {"open", "closed"}:
            raise ValueError("network_trust_or_ambiguous_identity")
        mapping[candidates[0]["id"]] = row["id"]
    if len(mapping) != 2:
        raise ValueError("network_trust_or_ambiguous_identity")
    return mapping


def _methods(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("file_methods_missing_or_invalid")
    result = []
    for method in value.split(";"):
        method = method.strip()
        # Explicit format equivalences, not fuzzy matching or inferred requirements.
        if method == "USB-носитель на Узле А":
            result.append("USB:node-A")
        elif method == "SFTP, вручную":
            result.append("SFTP:manual")
        elif match := re.fullmatch(r"(SFTP|FTP|SMB), (PULL|PUSH)", method):
            result.append(match[1] + ":" + match[2])
        elif match := re.fullmatch(r"(SFTP|FTP|SMB), автоматически при поступлении", method):
            result.append(match[1] + ":PUSH")
        elif match := re.fullmatch(r"(SFTP|FTP|SMB), автоматически по расписанию", method):
            result.append(match[1] + ":PULL")
        elif re.fullmatch(r"(SFTP|FTP|SMB), автоматически", method):
            raise ReferenceReview("reference_ambiguous_file_automation")
        else:
            raise ValueError("unrecognized_file_method_requires_review")
    if len(set(result)) != len(result):
        raise ValueError("duplicate_file_method")
    return ";".join(sorted(result))


def _rule_signature(rule: Any, mapping: dict[str, str]) -> str:
    if not isinstance(rule, dict):
        raise ValueError("invalid_rule")
    kind, mode = rule.get("type"), rule.get("mode")
    if kind not in {"data", "file"} or mode not in {"one-way", "two-way"}:
        raise ValueError("invalid_rule_type_or_mode")
    sender, receiver = rule.get("from"), rule.get("to")
    if not isinstance(sender, str) or not isinstance(receiver, str):
        raise ValueError("invalid_rule_endpoint")
    if sender not in mapping or receiver not in mapping or sender == receiver:
        raise ValueError("invalid_rule_endpoint")
    sender, receiver = mapping[sender], mapping[receiver]
    protocol = rule.get("protocol")
    signature: dict[str, Any] = {"type": kind, "mode": mode, "from": sender, "to": receiver}
    if kind == "data":
        if not isinstance(protocol, str) or not protocol:
            raise ValueError("invalid_data_protocol")
        port = rule.get("port")
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise ValueError("invalid_data_port")
        signature.update(protocol=protocol, port=port)
    else:
        if rule.get("port") is not None:
            raise ValueError("unexpected_file_port")
        if not isinstance(protocol, dict) or set(protocol) != set(mapping):
            raise ValueError("invalid_file_protocol_sides")
        signature["protocol"] = {mapping[key]: _methods(value) for key, value in protocol.items()}
        checks = rule.get("checks")
        if not isinstance(checks, list) or not all(isinstance(check, str) for check in checks):
            raise ValueError("file_checks_missing_or_invalid")
        if len(set(checks)) != len(checks):
            raise ValueError("duplicate_file_checks")
        signature["checks"] = sorted(checks)
        if mode == "two-way":
            signature["from"], signature["to"] = sorted((sender, receiver))
    return json.dumps(signature, ensure_ascii=False, sort_keys=True)


def compare_project(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    issues = []
    if actual.get("schema_version") != "1.1":
        issues.append("expert_schema_version")
    if actual.get("status") != "completed":
        issues.append("result_not_completed")
    for key in ("rules_count", "file_users_count"):
        value = actual.get(key)
        if type(value) is not int or value != expected.get(key):
            issues.append(key)
    rules = actual.get("rules")
    if not isinstance(rules, list) or len(rules) > 512:
        issues.append("invalid_rule_array")
        return issues
    if type(actual.get("rules_count")) is int and len(rules) != actual["rules_count"]:
        issues.append("rule_count_array_mismatch")
    if "confirmed_rules_count" in actual and (
        type(actual["confirmed_rules_count"]) is not int
        or actual["confirmed_rules_count"] != expected.get("rules_count")
    ):
        issues.append("minimum_not_confirmed")
    if actual.get("capacity_exceeded") is True:
        issues.append("capacity_exceeded")
    for key, fields in (
        ("firewall", ("side",)),
        ("synonyx", ("location", "node_a_faces", "node_b_faces")),
    ):
        wanted, served = expected.get(key), actual.get(key)
        if not isinstance(wanted, dict) or not isinstance(served, dict):
            issues.append(key)
            continue
        if any(served.get(field) != wanted.get(field) for field in fields):
            issues.append(key)
    try:
        mapping = _networks(expected, actual)
        wanted_networks = {value: value for value in mapping.values()}
        expected_rules = expected.get("rules")
        if not isinstance(expected_rules, list):
            raise ValueError("invalid_reference_rules")
        wanted_signatures = Counter(
            _rule_signature(rule, wanted_networks) for rule in expected_rules
        )
        actual_signatures = Counter(_rule_signature(rule, mapping) for rule in rules)
        if wanted_signatures != actual_signatures:
            issues.append("rule_multiset_mismatch")
        device = actual.get("synonyx")
        if isinstance(device, dict):
            for field, trust in (("node_a_network_id", "open"), ("node_b_network_id", "closed")):
                if field in device:
                    linked = next(
                        (row for row in actual["networks"] if row["id"] == device[field]), None
                    )
                    if linked is None or linked.get("trust") != trust:
                        issues.append("node_network_orientation")
    except ReferenceReview:
        issues.append("reference_ambiguous_file_automation")
    except (ValueError, TypeError):
        issues.append("uncomparable_network_or_rule_structure")
    return sorted(set(issues))


def audit(benchmark_path: Path, results: Path) -> dict[str, Any]:
    benchmark, digest = read_json(benchmark_path)
    items = benchmark.get("items") if isinstance(benchmark, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise ValueError("Expected bounded benchmark items")
    seen = set()
    filenames = set()
    cases = []
    sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("Invalid benchmark case")
        identifier = item["id"]
        if not CASE_ID.fullmatch(identifier) or identifier in seen:
            raise ValueError("Unsafe or duplicate case ID")
        group = item.get("group")
        if group is not None and (
            not isinstance(group, str)
            or group
            not in {
                "full",
                "basic",
                "incomplete",
                "out_of_scope",
                "facts",
            }
        ):
            raise ValueError("Unknown benchmark group")
        seen.add(identifier)
        phases = [("initial", item.get("expected"))]
        follow_up = item.get("follow_up")
        if isinstance(follow_up, dict) and isinstance(follow_up.get("expected"), dict):
            phases.append(("follow_up", follow_up["expected"]))
        initial_session = None
        for phase, expected in phases:
            case: dict[str, Any] = {
                "id": identifier,
                "phase": phase,
                "group": item.get("group"),
                "structural_status": "manual_review",
                "issues": [],
                "model_dialogue_verified": False,
                "diagram_verified": False,
            }
            reference = (expected if isinstance(expected, dict)
                         and isinstance(expected.get("rules"), list) else None)
            if reference is not None:
                if len(reference["rules"]) > 512 or any(
                    type(reference.get(key)) is not int or reference[key] < 0
                    for key in ("rules_count", "file_users_count")
                ):
                    raise ValueError("Invalid reference counts")
            filename = identifier + ("-follow-up" if phase == "follow_up" else "") + ".json"
            if filename.casefold() in filenames:
                raise ValueError("Result filenames collide")
            filenames.add(filename.casefold())
            path = results / filename
            cases.append(case)
            if not path.exists():
                if reference is not None:
                    case["structural_status"] = "missing"
                continue
            if not path.resolve().is_relative_to(results.resolve()):
                raise ValueError("Result path leaves input directory")
            actual, result_digest = read_json(path)
            if not isinstance(actual, dict):
                raise ValueError("Expected expert JSON object")
            issues = compare_project(reference, actual) if reference is not None else []
            status = "pass" if reference is not None else "manual_review"
            session = actual.get("session_id")
            if not isinstance(session, str) or not session.strip():
                issues.append("missing_session_id")
            else:
                session = session.strip()
                sessions[session].append(case)
                if phase == "initial":
                    initial_session = session
                elif initial_session is None:
                    issues.append("reference_initial_session_not_verified")
                elif session != initial_session:
                    issues.append("follow_up_session_mismatch")
            if issues:
                status = ("manual_review" if all(issue.startswith("reference_") for issue in issues)
                          else "fail")
            case.update(structural_status=status, issues=issues, actual_sha256=result_digest)
    for captures in sessions.values():
        if len({case["id"] for case in captures}) > 1:
            for case in captures:
                case["issues"].append("session_reused_across_cases")
                case["structural_status"] = "fail"
    totals = Counter(case["structural_status"] for case in cases)
    return {
        "schema_version": 1,
        "benchmark_sha256": digest,
        "benchmark_cases": len(items),
        "phases": cases,
        "structural_totals": {
            key: totals[key] for key in ("pass", "fail", "missing", "manual_review")
        },
        "acceptance_complete": False,
        "unverified": [
            "extraction",
            "clarification_dialogue",
            "facts_and_citations",
            "vendor_wishes",
            "diagram_and_drawio",
            "actual_cost_and_latency",
            "injection_resistance",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError("Output already exists")
        report = audit(args.benchmark, args.results)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (OSError, ValueError, RecursionError):
        print("Benchmark audit failed: check private input and new output path.", file=sys.stderr)
        return 2
    totals = report["structural_totals"]
    print("Structural audit:", totals, "; manual acceptance still required.")
    return 1 if totals["fail"] or totals["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
