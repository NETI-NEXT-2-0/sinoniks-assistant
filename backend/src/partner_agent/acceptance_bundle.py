"""Offline acceptance manifest and evidence inventory. Never calls a provider or grades answers."""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import UUID
from xml.etree.ElementTree import ParseError, fromstring

from partner_agent.benchmark_audit import MAX_BYTES, read_json
from partner_agent.runtime.accounting_math import priced_usage

SHA = re.compile(r"[0-9a-f]{40}")
SESSION_PLAN = (
    ("F01", 1), ("F02", 1), ("F04", 1), ("F07", 1), ("N01", 4), ("N05", 4),
    ("O01", 3), ("Q01", 1), ("Q10", 1), ("U01", 2), ("U02-separate", 3),
    ("U02-shared", 3), ("U03", 7), ("U04-three", 1), ("U04-open", 2), ("U05", 1), ("U06", 4),
)
PROJECT_CASES = frozenset({"F01", "F02", "F04", "F07", "N01", "N05", "U01",
                           "U02-separate", "U02-shared", "U03", "U04-open", "U05", "U06"})
ROUTE_KEYS = {"model_id", "provider_variant", "snapshot", "input_usd_per_million",
              "output_usd_per_million", "context_window_tokens", "max_output_tokens"}


def money(value: Any) -> Decimal:
    if (not isinstance(value, (str, int, float)) or isinstance(value, bool)
            or len(str(value)) > 80):
        raise ValueError("Invalid monetary value")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid monetary value") from None
    exponent = result.as_tuple().exponent
    if (not result.is_finite() or result < 0 or result > 1_000_000
            or not isinstance(exponent, int) or exponent < -18):
        raise ValueError("Invalid monetary value")
    return result


def routes_from(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"routes"}:
        raise ValueError("Provide only public route configuration, never credentials")
    rows = value["routes"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
        raise ValueError("Expected one to four declared routes")
    routes = []
    identities = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != ROUTE_KEYS:
            raise ValueError("Invalid route keys")
        for key in ("model_id", "provider_variant", "snapshot"):
            if (not isinstance(row[key], str) or not 1 <= len(row[key]) <= 200
                    or any(ord(char) < 32 for char in row[key])):
                raise ValueError("Invalid route identity")
        identity = (row["model_id"], row["provider_variant"], row["snapshot"])
        if identity in identities:
            raise ValueError("Duplicate route")
        identities.add(identity)
        for key in ("context_window_tokens", "max_output_tokens"):
            if type(row[key]) is not int or not 1 <= row[key] <= 2_000_000:
                raise ValueError("Invalid route token limit")
        if row["max_output_tokens"] > row["context_window_tokens"]:
            raise ValueError("Output exceeds context")
        inbound = money(row["input_usd_per_million"])
        outbound = money(row["output_usd_per_million"])
        if inbound > Decimal("0.10"):
            raise ValueError("Ineligible declared input tariff")
        routes.append({**row, "input_usd_per_million": str(inbound),
                       "output_usd_per_million": str(outbound)})
    return routes


def prepare(output: Path, git_sha: str, config: Any) -> dict[str, Any]:
    if not SHA.fullmatch(git_sha) or output.exists():
        raise ValueError("Supply a full SHA and a new output directory")
    routes = routes_from(config)
    per_attempt = max((money(row["input_usd_per_million"]) * row["context_window_tokens"]
                       + money(row["output_usd_per_million"]) * row["max_output_tokens"])
                      / 1_000_000 for row in routes)
    plan = {"schema_version": 1, "git_sha": git_sha, "routes": routes,
            "max_attempts": 60, "planned_attempts": sum(n for _, n in SESSION_PLAN),
            "budget_ceiling_usd": str((60 * per_attempt).quantize(
                Decimal("0.01"), rounding=ROUND_CEILING,
            )), "budget_approved": False, "makes_external_requests": False,
            "sessions": [{"case_id": name, "planned_attempts": count}
                         for name, count in SESSION_PLAN]}
    records = {"schema_version": 1, "captures": [
        {"case_id": name, "session_id": None, "runtime_git_sha": None,
         "review_status": "not_checked", "snapshot": None, "ledger": None,
         "expert": None, "drawio": None} for name, _ in SESSION_PLAN
    ]}
    contents = {"plan.json": plan, "captures.json": records}
    output.mkdir(parents=True)
    for name, content in contents.items():
        with (output / name).open("x", encoding="utf-8") as handle:
            json.dump(content, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
    return plan


def artifact(root: Path, value: Any) -> tuple[Path, bytes, str]:
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or Path(value).is_absolute():
        raise ValueError("Invalid relative artifact path")
    path = (root / value).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Artifact leaves capture directory")
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Artifact exceeds size limit")
    return path, raw, hashlib.sha256(raw).hexdigest()


def collect(plan_path: Path, captures_path: Path) -> dict[str, Any]:
    plan, plan_hash = read_json(plan_path)
    records, records_hash = read_json(captures_path)
    if (not isinstance(plan, dict) or plan.get("schema_version") != 1
            or not isinstance(plan.get("git_sha"), str) or not SHA.fullmatch(plan["git_sha"])
            or plan.get("max_attempts") != 60):
        raise ValueError("Invalid acceptance plan")
    routes = routes_from({"routes": plan.get("routes")})
    if (not isinstance(records, dict) or set(records) != {"schema_version", "captures"}
            or records["schema_version"] != 1 or not isinstance(records["captures"], list)
            or len(records["captures"]) > 100):
        raise ValueError("Invalid captures")
    captures = records["captures"]
    known_cases = {name for name, _ in SESSION_PLAN}
    by_case = {}
    for row in captures:
        if (not isinstance(row, dict) or set(row) != {"case_id", "session_id", "runtime_git_sha",
                "review_status", "snapshot", "ledger", "expert", "drawio"}
                or not isinstance(row["case_id"], str)
                or (row["case_id"] not in known_cases
                    and not re.fullmatch(r"EXTRA-[A-Za-z0-9_-]{1,30}", row["case_id"]))):
            raise ValueError("Invalid capture keys or case")
        if row["case_id"] in by_case:
            raise ValueError("Duplicate case")
        if row["review_status"] not in ("pass", "fail", "not_checked"):
            raise ValueError("Invalid review status")
        by_case[row["case_id"]] = row
    issues = [f"{case}:missing_capture" for case in sorted(known_cases - by_case.keys())]
    outputs: list[dict[str, Any]] = []
    sessions: dict[str, list[str]] = {}
    calls: dict[str, Any] = {}
    turn_ids: set[str] = set()
    timings: list[int] = []
    unknown_cost = False
    unknown_timing = False
    known_cost = Decimal(0)
    for case, row in by_case.items():
        problems = []
        hashes = {}
        try:
            if str(UUID(row["session_id"])) != row["session_id"]:
                raise ValueError()
        except (TypeError, ValueError, AttributeError):
            problems.append("missing_or_invalid_session")
        else:
            sessions.setdefault(row["session_id"], []).append(case)
        if row["runtime_git_sha"] != plan["git_sha"]:
            problems.append("runtime_sha_unconfirmed")
        if row["review_status"] != "pass":
            problems.append("manual_review_" + str(row["review_status"]))
        required = {"snapshot", "ledger"} | ({"expert"} if case in PROJECT_CASES else set())
        if case in PROJECT_CASES - {"N01", "U04-open"}:
            required.add("drawio")
        data = {}
        for kind in ("snapshot", "ledger", "expert", "drawio"):
            if row[kind] is None:
                if kind in required:
                    problems.append("missing_" + kind)
                continue
            try:
                path, raw, digest = artifact(captures_path.parent, row[kind])
                if kind == "drawio":
                    xml_text = raw.decode("utf-8")
                    native = fromstring(xml_text) if "<!DOCTYPE" not in xml_text.upper() else None
                    if (native is None or native.tag != "mxfile"
                            or not 1 <= len(native.findall("diagram")) <= 86):
                        raise ValueError("Invalid native XML")
                else:
                    content, parsed_hash = read_json(path)
                    if parsed_hash != digest:
                        raise ValueError("Artifact changed while reading")
                    if not isinstance(content, dict):
                        raise ValueError("Expected object")
                    session = content.get("chat", {}).get("id") if kind == "snapshot" \
                        else content.get("session_id")
                    if session != row["session_id"]:
                        problems.append(kind + "_session_mismatch")
                    data[kind] = content
                hashes[kind] = digest
            except (OSError, ValueError, ParseError, TypeError, AttributeError):
                problems.append("invalid_" + kind)
        ledger = data.get("ledger", {})
        entries, turns = ledger.get("calls"), ledger.get("turns")
        if (not isinstance(entries, list) or len(entries) > 10000
                or not isinstance(turns, list) or not turns or len(turns) > 10000):
            problems.append("incomplete_ledger")
            unknown_cost = unknown_timing = True
            entries, turns = [], []
        if ledger.get("legacy_untracked_turns") != 0:
            problems.append("untracked_turns")
            unknown_cost = True
        summary = ledger.get("summary")
        if not isinstance(summary, dict) or summary.get("calls_count") != len(entries):
            problems.append("ledger_call_count_mismatch")
            unknown_cost = True
        local_turns = {turn.get("turn_id") for turn in turns
                       if isinstance(turn, dict) and isinstance(turn.get("turn_id"), str)}
        for call in entries:
            if not isinstance(call, dict) or not isinstance(call.get("call_id"), str):
                problems.append("invalid_call")
                unknown_cost = True
                continue
            if call["call_id"] in calls:
                problems.append("duplicate_call_id")
                unknown_cost = True
                continue
            calls[call["call_id"]] = call
            if len(calls) > 10000:
                raise ValueError("Too many recorded attempts")
            if call.get("turn_id") not in local_turns:
                problems.append("call_without_turn")
            tariff = call.get("tariff", {})
            if not isinstance(tariff, dict):
                problems.append("invalid_tariff")
                unknown_cost = True
                continue
            route = next((route for route in routes if call.get("model_id") == route["model_id"]
                          and call.get("provider") == route["provider_variant"]
                          and tariff.get("snapshot") == route["snapshot"]), None)
            if (route is None or tariff.get("declared") is not True
                    or tariff.get("contest_mode") is not True
                    or tariff.get("eligible_input_price") is not True
                    or tariff.get("catalog_id") != call.get("model_id")
                    or tariff.get("provider_variant") != call.get("provider")):
                problems.append("undeclared_or_ineligible_route")
                unknown_cost = True
                continue
            try:
                if any(money(tariff.get(key)) != money(route[key]) for key in (
                    "input_usd_per_million", "output_usd_per_million",
                )):
                    raise ValueError()
                usage = call.get("usage", {})
                if any(type(usage.get(key)) is not int or not 0 <= usage[key] <= 10_000_000
                       for key in ("input_tokens", "output_tokens")):
                    raise ValueError()
                priced = priced_usage(usage, route["input_usd_per_million"],
                                      route["output_usd_per_million"])
                amount = money(priced["estimated_cost_usd"])
                known_cost += amount
                if (usage["input_tokens"] > route["context_window_tokens"]
                        or usage["output_tokens"] > route["max_output_tokens"]):
                    problems.append("token_limits_exceeded")
                if money(call.get("estimated_cost_usd")) != amount:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError, InvalidOperation):
                problems.append("unknown_or_inconsistent_cost")
                unknown_cost = True
            if call.get("status") not in {"succeeded", "failed", "cancelled"}:
                problems.append("unfinished_call")
                unknown_cost = True
        for turn in turns:
            if (not isinstance(turn, dict) or not isinstance(turn.get("turn_id"), str)
                    or turn["turn_id"] in turn_ids):
                problems.append("invalid_or_duplicate_turn")
                unknown_timing = True
                continue
            turn_ids.add(turn["turn_id"])
            elapsed = turn.get("full_elapsed_ms")
            if (type(elapsed) is not int or elapsed < 0 or turn.get("timing_scope") != "full_result"
                    or turn.get("status") not in {"succeeded", "failed", "cancelled"}):
                unknown_timing = True
                problems.append("missing_full_turn_time")
            else:
                timings.append(elapsed)
        issues.extend(case + ":" + problem for problem in dict.fromkeys(problems))
        outputs.append({"case_id": case, "artifact_sha256": hashes,
                        "issues": list(dict.fromkeys(problems))})
    for shared in sessions.values():
        if len(shared) > 1:
            for case in shared:
                issues.append(case + ":session_reused")
                next(row for row in outputs if row["case_id"] == case)["issues"].append(
                    "session_reused",
                )
    if not calls:
        issues.append("no_provider_attempts_recorded")
    if len(calls) > 60:
        issues.append("attempt_ceiling_exceeded")
    ceiling = money(plan.get("budget_ceiling_usd"))
    if known_cost > ceiling:
        issues.append("declared_cost_ceiling_exceeded")
    if known_cases - by_case.keys():
        unknown_cost = unknown_timing = True
    mean = sum(timings) / len(timings) if timings and not unknown_timing else None
    if mean is not None and mean > 15000:
        issues.append("full_time_limit_exceeded")
    return {"schema_version": 1, "status": "incomplete" if issues else "ready_for_review",
            "acceptance_complete": False, "model_quality_automatically_graded": False,
            "runtime_sha_proof": "operator_confirmation_only", "git_sha": plan["git_sha"],
            "plan_sha256": plan_hash, "captures_sha256": records_hash, "cases": outputs,
            "issues": issues,
            "issue_counts": dict(Counter(issue.split(":")[-1] for issue in issues)),
            "recorded_attempts": len(calls), "recorded_turns": len(turn_ids),
            "known_declared_cost_usd": str(known_cost),
            "declared_cost_usd": None if unknown_cost else str(known_cost),
            "actual_provider_billing_verified": False, "mean_full_elapsed_ms": mean,
            "full_time_limit_met": mean <= 15000 if mean is not None else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_args = sub.add_parser("prepare")
    prepare_args.add_argument("--git-sha", required=True)
    prepare_args.add_argument("--config", type=Path, required=True)
    prepare_args.add_argument("--output", type=Path, required=True)
    collect_args = sub.add_parser("collect")
    collect_args.add_argument("--plan", type=Path, required=True)
    collect_args.add_argument("--captures", type=Path, required=True)
    collect_args.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            config, _ = read_json(args.config)
            result = prepare(args.output, args.git_sha, config)
            print(f"Prepared {len(result['sessions'])} sessions; no external requests or approval.")
            return 0
        if args.output.exists():
            raise ValueError("Output already exists")
        result = collect(args.plan, args.captures)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        print("Acceptance inventory:", result["status"], "; manual/model review remains required.")
        return 1 if result["issues"] else 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print("Acceptance inventory failed: check private inputs and new output path.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
