"""Evidence completeness must not turn missing costs, timings or manual reviews into a pass."""

import json
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest

from partner_agent.acceptance_bundle import BASE_CASES, artifact, collect, main, prepare

SHA = "b" * 40
ROUTE = {"model_id": "qwen/qwen3-30b-a3b-instruct-2507", "provider_variant": "dekallm",
         "snapshot": "synthetic-snapshot", "input_usd_per_million": "0.09",
         "output_usd_per_million": "0.30", "context_window_tokens": 262144,
         "max_output_tokens": 3000}


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def captured(tmp_path):
    folder = tmp_path / "captures"
    prepare(folder, SHA, {"routes": [ROUTE]})
    records = json.loads((folder / "captures.json").read_text())
    for record in records["captures"]:
        name, session, turn, call = record["case_id"], str(uuid4()), str(uuid4()), str(uuid4())
        record.update(session_id=session, runtime_git_sha=SHA, review_status="pass")
        for kind in ("snapshot", "ledger", "expert", "drawio"):
            record[kind] = f"{name}-{kind}.{'drawio' if kind == 'drawio' else 'json'}"
        write(folder / record["snapshot"], {"chat": {"id": session}})
        write(folder / record["expert"], {"session_id": session, "rules": []})
        (folder / record["drawio"]).write_text(
            '<mxfile><diagram><mxGraphModel><root /></mxGraphModel></diagram></mxfile>',
        )
        write(folder / record["ledger"], {
            "session_id": session, "legacy_untracked_turns": 0, "summary": {"calls_count": 1},
            "calls": [{"call_id": call, "turn_id": turn, "model_id": ROUTE["model_id"],
                       "provider": ROUTE["provider_variant"], "status": "succeeded",
                       "usage": {"input_tokens": 1000, "output_tokens": 100},
                       "estimated_cost_usd": "0.00012", "tariff": {
                           **ROUTE, "catalog_id": ROUTE["model_id"], "declared": True,
                           "contest_mode": True, "eligible_input_price": True,
                       }}],
            "turns": [{"turn_id": turn, "status": "succeeded", "full_elapsed_ms": 15000,
                       "timing_scope": "full_result"}],
        })
    write(folder / "captures.json", records)
    return folder, records


def report(folder):
    return collect(folder / "plan.json", folder / "captures.json")


def test_plan_has_seventeen_independent_sessions_and_no_authorization(tmp_path):
    plan = prepare(tmp_path / "run", SHA, {"routes": [ROUTE]})
    assert len(plan["sessions"]) == 17 and plan["planned_attempts"] == 40
    assert plan["max_attempts"] == 60 and plan["budget_ceiling_usd"] == "1.47"
    assert not plan["budget_approved"] and not plan["makes_external_requests"]
    assert {item["case_id"] for item in plan["sessions"]} >= {"U04-three", "U04-open"}
    incomplete = collect(tmp_path / "run/plan.json", tmp_path / "run/captures.json")
    assert incomplete["status"] == "incomplete"
    assert incomplete["declared_cost_usd"] is None
    assert incomplete["mean_full_elapsed_ms"] is None


@pytest.mark.parametrize("change", ["secret", "expensive", "nan", "duplicate", "bool_tokens"])
def test_invalid_public_configuration_never_creates_the_output(tmp_path, change):
    config = {"routes": [deepcopy(ROUTE)]}
    if change == "secret":
        config["api_key"] = "private-key"
    elif change == "expensive":
        config["routes"][0]["input_usd_per_million"] = "0.100001"
    elif change == "nan":
        config["routes"][0]["output_usd_per_million"] = "NaN"
    elif change == "duplicate":
        config["routes"].append(deepcopy(ROUTE))
    else:
        config["routes"][0]["context_window_tokens"] = True
    with pytest.raises(ValueError):
        prepare(tmp_path / "run", SHA, config)
    assert not (tmp_path / "run").exists()


def test_full_inventory_is_only_ready_for_review_not_a_quality_grade(tmp_path):
    folder, _ = captured(tmp_path)
    result = report(folder)
    assert result["status"] == "ready_for_review" and not result["acceptance_complete"]
    assert not result["model_quality_automatically_graded"]
    assert not result["actual_provider_billing_verified"]
    assert result["recorded_attempts"] == result["recorded_turns"] == 17
    assert Decimal(result["declared_cost_usd"]) == Decimal("0.00204")
    assert result["mean_full_elapsed_ms"] == 15000 and result["full_time_limit_met"]


def test_step_records_screenshot_hash_and_time_from_its_session_ledger(tmp_path):
    folder, records = captured(tmp_path)
    first = records["captures"][0]
    ledger = json.loads((folder / first["ledger"]).read_text())
    first["steps"] = [{
        "step_id": "first-result", "turn_id": ledger["turns"][0]["turn_id"],
        "revision": 1, "prompt": "Build a preliminary topology",
        "expected": "Known systems shown", "observed": "Known systems visible",
        "review_status": "pass", "screenshots": ["result.png"],
    }]
    screenshot = folder / "result.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nexample")
    write(folder / "captures.json", records)
    case = report(folder)["cases"][0]
    step, = case["steps"]
    assert step["full_elapsed_ms"] == 15000
    assert step["screenshots"] == [
        {"path": screenshot.name, "sha256": artifact(folder, screenshot.name)[2]},
    ]
    assert case["runtime_git_sha"] == SHA and case["expert_review_status"] == "pass"
    screenshot.write_bytes(b"example")
    first["steps"][0]["turn_id"] = str(uuid4())
    write(folder / "captures.json", records)
    assert set(report(folder)["cases"][0]["issues"]) == {
        "step_without_recorded_turn", "invalid_step_screenshot",
    }


def test_collection_flags_protocol_mismatch_between_exports(tmp_path):
    folder, records = captured(tmp_path)
    first = records["captures"][0]
    rule = {"id": 1, "rule_id": "rule", "from": "plant", "to": "office",
            "mode": "one-way", "type": "data", "protocol": "UDP", "port": 514}
    write(folder / first["expert"], {"session_id": first["session_id"], "rules": [rule]})
    row = {"number": 1, "id": "rule", "from_network_id": "plant",
           "to_network_id": "office", "two_way": False, "protocol": "UDP",
           "destination_port": 515}
    native = Element("mxfile", {"data-rule-rows": json.dumps([row])})
    SubElement(native, "diagram")
    (folder / first["drawio"]).write_bytes(tostring(native))
    assert report(folder)["cases"][0]["issues"] == ["export_protocol_mismatch"]


def test_collection_requires_known_file_checks_in_unplaced_preview(tmp_path):
    folder, records = captured(tmp_path)
    first = records["captures"][0]
    directions = [{"from_network_id": "office", "to_network_id": "plant",
                   "checks_by_network": {"office": [{"kind": "size", "details": None}],
                                         "plant": None}}]
    rule = {"type": "file", "rule_id": "file-rule", "directions": directions, "checks": ["size"]}
    write(folder / first["expert"], {"session_id": first["session_id"], "rules": [rule]})
    native = Element("mxfile", {"data-rule-rows": "[]", "data-placement-pending": "true"})
    SubElement(native, "diagram")
    path = folder / first["drawio"]
    path.write_bytes(tostring(native))
    assert report(folder)["cases"][0]["issues"] == ["export_file_checks_mismatch"]
    native.set("data-preview-file-checks", json.dumps({"file-rule": directions}))
    path.write_bytes(tostring(native))
    assert not report(folder)["cases"][0]["issues"]


@pytest.mark.parametrize("change", ["unknown_usage", "false_cost", "old_tariff", "orphan_call",
                                   "unknown_attempts", "false_call_count",
                                   "wrong_sha", "manual", "missing", "card_only", "slow"])
def test_missing_or_inconsistent_evidence_stays_visible(tmp_path, change):
    folder, records = captured(tmp_path)
    first = records["captures"][0]
    ledger_path = folder / first["ledger"]
    ledger = json.loads(ledger_path.read_text())
    if change == "unknown_usage":
        ledger["calls"][0]["usage"]["output_tokens"] = None
    elif change == "false_cost":
        ledger["calls"][0]["estimated_cost_usd"] = "0"
    elif change == "old_tariff":
        ledger["calls"][0]["tariff"]["snapshot"] = "other"
    elif change == "orphan_call":
        ledger["calls"][0]["turn_id"] = str(uuid4())
    elif change == "unknown_attempts":
        ledger["legacy_untracked_turns"] = 1
    elif change == "false_call_count":
        ledger["summary"]["calls_count"] = 2
    elif change == "wrong_sha":
        first["runtime_git_sha"] = "c" * 40
    elif change == "manual":
        first["review_status"] = "not_checked"
    elif change == "missing":
        first["snapshot"] = "absent.json"
    elif change == "card_only":
        ledger["turns"][0].update(full_elapsed_ms=None, card_elapsed_ms=1,
                                  elapsed_ms=1, timing_scope="answer_and_card")
    else:
        ledger["turns"][0]["full_elapsed_ms"] = 15001 * 17
    write(ledger_path, ledger)
    write(folder / "captures.json", records)
    result = report(folder)
    assert result["status"] == "incomplete" and result["issues"]
    if change in ("unknown_usage", "false_cost", "old_tariff", "unknown_attempts",
                  "false_call_count"):
        assert result["declared_cost_usd"] is None
    if change == "false_cost":
        assert Decimal(result["known_declared_cost_usd"]) == Decimal("0.00204")
    if change == "card_only":
        assert result["mean_full_elapsed_ms"] is None and result["full_time_limit_met"] is None
    if change == "slow":
        assert result["full_time_limit_met"] is False


def test_reused_sessions_and_call_ids_are_not_silently_accepted(tmp_path):
    folder, records = captured(tmp_path)
    first, second = records["captures"][:2]
    second["session_id"] = first["session_id"]
    one = json.loads((folder / first["ledger"]).read_text())
    two = json.loads((folder / second["ledger"]).read_text())
    two["calls"][0]["call_id"] = one["calls"][0]["call_id"]
    write(folder / second["ledger"], two)
    write(folder / "captures.json", records)
    result = report(folder)
    assert all("session_reused" in case["issues"] for case in result["cases"][:2])
    assert "duplicate_call_id" in result["cases"][1]["issues"]
    assert result["declared_cost_usd"] is None


def test_extra_attempts_are_counted_and_attempt_ceiling_is_checked(tmp_path):
    folder, records = captured(tmp_path)
    first = records["captures"][0]
    ledger_path = folder / first["ledger"]
    ledger = json.loads(ledger_path.read_text())
    for _ in range(44):
        ledger["calls"].append({**ledger["calls"][0], "call_id": str(uuid4()), "status": "failed"})
    ledger["summary"]["calls_count"] = 45
    write(ledger_path, ledger)
    result = report(folder)
    assert result["recorded_attempts"] == 61
    assert "attempt_ceiling_exceeded" in result["issues"]


def test_artifact_paths_cannot_leave_capture_directory(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()
    (tmp_path / "outside.json").write_text("{}")
    with pytest.raises(ValueError):
        artifact(folder, "../outside.json")
    with pytest.raises(ValueError):
        artifact(folder, str((tmp_path / "outside.json").resolve()))


@pytest.mark.parametrize("xml", [b"<mxfile />", b"not XML", '<mxfile/>'.encode("utf-16"),
                                b'<!DOCTYPE mxfile [<!ENTITY x "test">]><mxfile>&x;</mxfile>'])
def test_native_artifact_must_be_bounded_utf8_xml_without_a_doctype(tmp_path, xml):
    folder, records = captured(tmp_path)
    first = records["captures"][0]
    (folder / first["drawio"]).write_bytes(xml)
    result = report(folder)
    assert "F01:invalid_drawio" in result["issues"]
    assert result["status"] == "incomplete"


def test_cli_refuses_to_overwrite_existing_evidence(tmp_path):
    config = tmp_path / "public-config.json"
    write(config, {"routes": [ROUTE]})
    output = tmp_path / "run"
    args = ["prepare", "--git-sha", SHA, "--config", str(config), "--output", str(output)]
    assert main(args) == 0
    original = (output / "captures.json").read_bytes()
    assert main(args) == 2 and (output / "captures.json").read_bytes() == original


def benchmark(tmp_path):
    path = tmp_path / "private-benchmark.json"
    rows = [{"id": name, "query": "Synthetic input " + name,
             "expected": {"rules_count": 987654}, "explanation": "DO_NOT_COPY_ORACLE",
             "expected_behavior": "DO_NOT_COPY_ORACLE", "must_ask": ["DO_NOT_COPY_ORACLE"]}
            for name in sorted(BASE_CASES)]
    next(row for row in rows if row["id"] == "N05")["follow_up"] = {
        "user_answer": "Synthetic additional user facts", "expected": "DO_NOT_COPY_ORACLE",
    }
    write(path, {"items": rows, "scoring_note": "DO_NOT_COPY_ORACLE"})
    return path


def test_private_input_pack_never_copies_expected_answers_or_explanations(tmp_path):
    source = benchmark(tmp_path)
    raw = source.read_bytes()
    folder = tmp_path / "run"
    plan = prepare(folder, SHA, {"routes": [ROUTE]}, benchmark=source)
    inputs = json.loads((folder / "inputs.json").read_text())
    assert len(inputs["inputs"]) == 10
    assert inputs["inputs"][-1]["case_id"] in BASE_CASES
    followup = next(row for row in inputs["inputs"] if row["phase"] == "follow_up")
    assert followup == {"case_id": "N05", "phase": "follow_up",
                        "text": "Synthetic additional user facts"}
    all_outputs = "\n".join(path.read_text() for path in folder.iterdir())
    assert "DO_NOT_COPY_ORACLE" not in all_outputs
    assert "987654" not in (folder / "inputs.json").read_text()
    assert source.read_bytes() == raw
    assert len(plan["benchmark_sha256"]) == len(plan["input_prompts_sha256"]) == 64
    with (folder / "inputs.json").open("a") as handle:
        handle.write(" ")
    assert "input_pack_changed" in report(folder)["issues"]


@pytest.mark.parametrize("change", ["missing", "duplicate", "query", "follow_up"])
def test_bad_inputs_are_not_recovered_from_the_answer_key(tmp_path, change):
    path = benchmark(tmp_path)
    data = json.loads(path.read_text())
    if change == "missing":
        data["items"].pop()
    elif change == "duplicate":
        data["items"][0]["id"] = data["items"][1]["id"]
    elif change == "query":
        data["items"][0].pop("query")
    else:
        next(row for row in data["items"] if row["id"] == "N05")["follow_up"].pop("user_answer")
    write(path, data)
    with pytest.raises(ValueError):
        prepare(tmp_path / "run", SHA, {"routes": [ROUTE]}, benchmark=path)
    assert not (tmp_path / "run").exists()
