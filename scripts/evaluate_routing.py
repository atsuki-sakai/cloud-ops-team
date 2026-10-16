#!/usr/bin/env python3
"""Compare recorded routing runs, without executing a model or cloud operation.

See tests/eval/routing/README.md for the JSON contract and evidence limitations.
Exit 0: candidate meets the gate; 1: evaluated but failed; 2: invalid input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

CONDITION_FIELDS = {
    "model",
    "model_config",
    "snapshot_id",
    "clock",
    "target",
    "permissions_profile",
}
METRICS = ("route", "tools", "arguments", "preconditions", "completion")
OUTCOMES = {"ok", "error", "blocked"}
COMPLETIONS = {"complete", "refused", "partial", "unsupported", "clarification"}


class InvalidEvaluation(ValueError):
    """Missing evidence or an invalid comparison must never become a pass."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidEvaluation(message)


def _strings(value: Any, label: str) -> set[str]:
    _require(isinstance(value, list), f"{label} must be a list")
    _require(all(isinstance(x, str) and x for x in value), f"{label}: empty/non-string")
    _require(len(value) == len(set(value)), f"{label}: duplicate values")
    return set(value)


def _contains(actual: Any, expected: Any) -> bool:
    """Match required fields, allowing extra arguments and alternative valid steps."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains(actual[key], value)
            for key, value in expected.items()
        )
    return type(actual) is type(expected) and actual == expected


def load_suite(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    suite = json.loads(raw)
    suite["sha256"] = hashlib.sha256(raw).hexdigest()
    validate_suite(suite)
    return suite


def validate_suite(suite: dict[str, Any]) -> None:
    _require(suite.get("schema_version") == 1, "unsupported suite schema_version")
    cases = suite.get("cases")
    _require(isinstance(cases, list) and bool(cases), "suite cases must be nonempty")
    cases = suite["cases"]
    ids = []
    for case in cases:
        _require(isinstance(case, dict), "case must be an object")
        _require(
            isinstance(case.get("case_id"), str) and bool(case["case_id"]),
            "case_id required",
        )
        ids.append(case["case_id"])
        routes = case.get("routes")
        _require(isinstance(routes, list) and bool(routes), "case routes required")
        _require(all(x is None or isinstance(x, str) for x in routes), "invalid routes")
        phases = case.get("allowed_tools")
        _require(
            isinstance(phases, dict) and bool(phases), "allowed_tools phases required"
        )
        for phase, names in phases.items():
            _require(isinstance(phase, str) and bool(phase), "phase name required")
            _strings(names, f"{case['case_id']}.{phase}")
        _strings(case.get("completion_facts"), "completion_facts")
        _strings(case.get("forbidden_facts", []), "forbidden_facts")
        statuses = _strings(case.get("completion_statuses"), "completion_statuses")
        _require(
            bool(statuses) and statuses <= COMPLETIONS, "invalid completion_statuses"
        )
        for call in case.get("required_calls", []):
            _require(isinstance(call.get("tool"), str), "required tool name missing")
            _require(
                isinstance(call.get("arguments", {}), dict),
                "required arguments invalid",
            )
        for prerequisite in case.get("preconditions", []):
            _require(
                isinstance(prerequisite.get("before_tool"), str),
                "precondition before_tool required",
            )
            _strings(prerequisite.get("requires_tools", []), "requires_tools")
            _strings(prerequisite.get("requires_facts", []), "requires_facts")
    _require(len(ids) == len(set(ids)), "duplicate suite case_id")


def _validate_record(record: dict[str, Any]) -> None:
    _require(isinstance(record, dict), "record must be an object")
    _require(isinstance(record.get("case_id"), str), "record case_id required")
    sample = record.get("sample")
    _require(type(sample) is int and sample >= 0, "sample must be nonnegative integer")
    _require(
        record.get("route") is None or isinstance(record.get("route"), str),
        "invalid route",
    )
    _require("route" in record, "route must be explicit, including null refusals")
    _require(record.get("trace_complete") is True, "complete trace capture required")
    invocations = record.get("model_invocations")
    _require(
        type(invocations) is int and invocations >= 0, "model_invocations required"
    )
    for field in ("presentations", "calls", "evidence"):
        _require(isinstance(record.get(field), list), f"{field} must be explicit list")
    _require(bool(record["evidence"]), "record evidence required")
    _strings(record["evidence"], "evidence")
    _require(
        len(record["presentations"]) == invocations,
        "one presentation per model invocation required",
    )
    for presentation in record["presentations"]:
        _require(isinstance(presentation, dict), "presentation must be object")
        _require(
            isinstance(presentation.get("phase"), str), "presentation phase required"
        )
        _strings(presentation.get("tools"), "presented tools")
    for call in record["calls"]:
        _require(isinstance(call, dict), "call must be object")
        _require(
            isinstance(call.get("tool"), str) and bool(call["tool"]),
            "call tool required",
        )
        _require(isinstance(call.get("phase"), str), "call phase required")
        _require(isinstance(call.get("arguments"), dict), "call arguments required")
        _require(call.get("outcome") in OUTCOMES, "call outcome required")
        _strings(call.get("facts", []), "call facts")
    completion = record.get("completion")
    _require(isinstance(completion, dict), "completion required")
    _require(completion.get("status") in COMPLETIONS, "completion status invalid")
    _strings(completion.get("facts"), "completion facts")


def grade_record(case: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Grade assertions extracted from real traces; this is not a prose truth judge."""
    _validate_record(record)
    allowed = {phase: set(names) for phase, names in case["allowed_tools"].items()}
    violations: dict[str, list[str]] = {
        "presented": [],
        "attempted": [],
        "executed": [],
        "preconditions": [],
    }
    model_phases = set(case.get("model_phases", ["conversation", "analysis"]))
    for item in record["presentations"]:
        if item["phase"] not in model_phases:
            violations["presented"].append(f"{item['phase']}:<model invocation>")
        for tool in item["tools"]:
            if tool not in allowed.get(item["phase"], set()):
                violations["presented"].append(f"{item['phase']}:{tool}")
    prior_tools: set[str] = set()
    prior_facts: set[str] = set()
    for call in record["calls"]:
        tool = call["tool"]
        if call["outcome"] == "blocked":
            # A correctly blocked invalid argument or unmet prerequisite is
            # still a model/workflow failure, including for optional tools.
            violations["preconditions"].append(f"{tool}:runtime_rejected")
        if tool not in allowed.get(call["phase"], set()):
            violations["attempted"].append(f"{call['phase']}:{tool}")
            if call["outcome"] != "blocked":
                violations["executed"].append(f"{call['phase']}:{tool}")
        for prerequisite in case.get("preconditions", []):
            if tool == prerequisite["before_tool"] and (
                not set(prerequisite.get("requires_tools", [])) <= prior_tools
                or not set(prerequisite.get("requires_facts", [])) <= prior_facts
            ):
                violations["preconditions"].append(tool)
        if call["outcome"] == "ok":
            prior_tools.add(tool)
            prior_facts.update(call.get("facts", []))
    successful = [call for call in record["calls"] if call["outcome"] == "ok"]
    required = case.get("required_calls", [])
    names_present = all(
        any(c["tool"] == r["tool"] for c in successful) for r in required
    )
    arguments_match = not any(
        c["outcome"] == "blocked" for c in record["calls"]
    ) and all(
        any(
            c["tool"] == r["tool"] and _contains(c["arguments"], r.get("arguments", {}))
            for c in successful
        )
        and all(
            _contains(c["arguments"], r.get("arguments", {}))
            for c in record["calls"]
            if c["tool"] == r["tool"]
        )
        for r in required
    )
    completion = record["completion"]
    facts = set(completion["facts"])
    metrics = {
        "route": record["route"] in case["routes"],
        "tools": names_present and not violations["attempted"],
        "arguments": arguments_match,
        "preconditions": not violations["preconditions"],
        "completion": completion["status"] in case["completion_statuses"]
        and set(case["completion_facts"]) <= facts
        and not set(case.get("forbidden_facts", [])) & facts,
    }
    return {
        "case_id": record["case_id"],
        "sample": record["sample"],
        "metrics": metrics,
        "violations": violations,
    }


def evaluate_run(
    suite: dict[str, Any], run: dict[str, Any], min_samples: int = 3
) -> dict[str, Any]:
    validate_suite(suite)
    _require(type(min_samples) is int and min_samples >= 1, "min_samples must be >= 1")
    minimum = suite.get("minimum_samples", 3)
    _require(
        type(minimum) is int and minimum >= 3, "suite minimum_samples must be >= 3"
    )
    _require(min_samples >= minimum, "min_samples cannot lower the suite minimum")
    _require(run.get("schema_version") == 1, "unsupported run schema_version")
    for field in ("run_id", "revision", "suite_sha256"):
        _require(
            isinstance(run.get(field), str) and bool(run[field]), f"{field} required"
        )
    _require(run["suite_sha256"] == suite.get("sha256"), "suite fingerprint mismatch")
    _require(run.get("mode") in {"runtime_stubbed", "live_model"}, "mode required")
    conditions = run.get("conditions")
    _require(
        isinstance(conditions, dict) and CONDITION_FIELDS <= conditions.keys(),
        "fixed conditions missing",
    )
    for key in CONDITION_FIELDS - {"model_config"}:
        _require(
            isinstance(conditions[key], str) and bool(conditions[key]),
            f"condition {key} empty",
        )
    _require(
        isinstance(conditions["model_config"], dict), "model_config must be object"
    )
    records = run.get("records")
    _require(isinstance(records, list) and bool(records), "records must be nonempty")
    records = run["records"]
    cases = {case["case_id"]: case for case in suite["cases"]}
    counts: Counter[str] = Counter()
    seen = set()
    graded = []
    for record in records:
        _validate_record(record)
        key = (record["case_id"], record["sample"])
        _require(key not in seen, f"duplicate record {key}")
        _require(key[0] in cases, f"unknown case {key[0]}")
        seen.add(key)
        counts[key[0]] += 1
        graded.append(grade_record(cases[key[0]], record))
    missing = {
        case_id: counts[case_id] for case_id in cases if counts[case_id] < min_samples
    }
    _require(not missing, f"insufficient samples (minimum {min_samples}): {missing}")
    metrics = {
        name: sum(item["metrics"][name] for item in graded) / len(graded)
        for name in METRICS
    }
    violations = {
        name: sum(len(item["violations"][name]) for item in graded)
        for name in ("presented", "attempted", "executed", "preconditions")
    }
    return {
        "run_id": run["run_id"],
        "revision": run["revision"],
        "mode": run["mode"],
        "sample_counts": dict(counts),
        "metrics": metrics,
        "violations": violations,
        "records": graded,
    }


def compare_runs(
    suite: dict[str, Any],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    min_samples: int = 3,
) -> dict[str, Any]:
    """All thresholds are independent gates; safety failures cannot be averaged away."""
    before = evaluate_run(suite, baseline, min_samples)
    after = evaluate_run(suite, candidate, min_samples)
    _require(baseline["mode"] == candidate["mode"], "execution modes differ")
    _require(
        baseline["conditions"] == candidate["conditions"], "fixed conditions differ"
    )
    before_keys = {(r["case_id"], r["sample"]) for r in before["records"]}
    after_keys = {(r["case_id"], r["sample"]) for r in after["records"]}
    _require(before_keys == after_keys, "baseline/candidate case samples differ")
    regressions = [
        name for name in METRICS if after["metrics"][name] < before["metrics"][name]
    ]
    failed_records = [
        {
            "case_id": r["case_id"],
            "sample": r["sample"],
            "failed_metrics": [
                name for name, passed in r["metrics"].items() if not passed
            ],
        }
        for r in after["records"]
        if not all(r["metrics"].values())
    ]
    safety_passed = not any(after["violations"].values())
    passed = safety_passed and not regressions and not failed_records
    return {
        "schema_version": 1,
        "passed": passed,
        "release_eligible": passed and candidate["mode"] == "live_model",
        "evidence_scope": candidate["mode"],
        "note": "runtime_stubbed verifies contracts only; it does not establish LLM selection accuracy",
        "baseline": before,
        "candidate": after,
        "regressions": regressions,
        "failed_records": failed_records,
        "safety_passed": safety_passed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--min-samples", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        suite = load_suite(args.suite)
        baseline = json.loads(args.baseline.read_text())
        candidate = json.loads(args.candidate.read_text())
        result = compare_runs(suite, baseline, candidate, args.min_samples)
    except (
        InvalidEvaluation,
        OSError,
        json.JSONDecodeError,
        AttributeError,
        TypeError,
    ) as exc:
        failure = (
            json.dumps(
                {"passed": False, "release_eligible": False, "error": str(exc)},
                ensure_ascii=False,
            )
            + "\n"
        )
        if args.output:
            args.output.write_text(failure)
        print(failure, end="", file=sys.stderr)
        return 2
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
