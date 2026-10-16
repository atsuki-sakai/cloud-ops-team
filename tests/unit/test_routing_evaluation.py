"""Contract and failure-mode coverage for the offline routing comparison gate."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.evaluate_routing import (
    InvalidEvaluation,
    compare_runs,
    evaluate_run,
    grade_record,
    load_suite,
    main,
)


@pytest.fixture
def suite():
    return {
        "schema_version": 1,
        "sha256": "test-suite-hash",
        "cases": [
            {
                "case_id": "access",
                "routes": ["access_watch"],
                "allowed_tools": {
                    "collect": ["get_run_context", "investigate_access", "audit_iam"],
                    "analysis": ["get_collected_evidence", "publish_report"],
                },
                "required_calls": [
                    {"tool": "get_run_context", "arguments": {}},
                    {"tool": "investigate_access", "arguments": {"hours": 24}},
                ],
                "completion_statuses": ["partial"],
                "completion_facts": ["report_ready", "coverage_incomplete_reported"],
                "forbidden_facts": ["no_attack_proven"],
                "preconditions": [
                    {
                        "before_tool": "publish_report",
                        "requires_tools": ["get_run_context"],
                        "requires_facts": ["evidence_checked"],
                    }
                ],
            }
        ],
    }


@pytest.fixture
def record():
    return {
        "case_id": "access",
        "sample": 0,
        "route": "access_watch",
        "trace_complete": True,
        "model_invocations": 1,
        "presentations": [
            {"phase": "analysis", "tools": ["get_collected_evidence", "publish_report"]}
        ],
        "calls": [
            {
                "phase": "collect",
                "tool": "get_run_context",
                "arguments": {},
                "outcome": "ok",
                "facts": [],
            },
            {
                "phase": "collect",
                "tool": "investigate_access",
                "arguments": {"hours": 24},
                "outcome": "ok",
                "facts": ["evidence_checked"],
            },
            {
                "phase": "analysis",
                "tool": "publish_report",
                "arguments": {"body": "coverage incomplete"},
                "outcome": "ok",
                "facts": [],
            },
        ],
        "completion": {
            "status": "partial",
            "facts": ["report_ready", "coverage_incomplete_reported"],
        },
        "evidence": ["fixture://access/0"],
    }


def _run(suite, record, *, mode="runtime_stubbed", samples=3):
    records = []
    for sample in range(samples):
        item = copy.deepcopy(record)
        item["sample"] = sample
        records.append(item)
    return {
        "schema_version": 1,
        "run_id": "test",
        "revision": "revision-a",
        "mode": mode,
        "suite_sha256": suite["sha256"],
        "conditions": {
            "model": "unchanged-model",
            "model_config": {"temperature": 0},
            "snapshot_id": "fixture-v1",
            "clock": "2026-10-16T00:00:00Z",
            "target": "test-project",
            "permissions_profile": "readonly-dry-run",
        },
        "records": records,
    }


def test_mocked_contract_pass_is_not_live_model_release_evidence(suite, record):
    baseline = _run(suite, record)
    result = compare_runs(suite, baseline, copy.deepcopy(baseline))
    assert result["passed"]
    assert not result["release_eligible"]
    assert result["candidate"]["metrics"] == dict.fromkeys(
        ["route", "tools", "arguments", "preconditions", "completion"], 1.0
    )


def test_live_record_pass_is_a_gate_result(suite, record):
    run = _run(suite, record, mode="live_model")
    assert compare_runs(suite, run, copy.deepcopy(run))["release_eligible"]


def test_independent_collection_order_and_extra_valid_steps_are_allowed(suite, record):
    record["calls"][0:2] = list(reversed(record["calls"][0:2]))
    record["calls"].insert(
        1,
        {
            "phase": "collect",
            "tool": "audit_iam",
            "arguments": {},
            "outcome": "ok",
            "facts": [],
        },
    )
    assert all(grade_record(suite["cases"][0], record)["metrics"].values())


@pytest.mark.parametrize(
    "change,metric",
    [
        ("route", "route"),
        ("arguments", "arguments"),
        ("missing_tool", "tools"),
        ("missing_fact", "completion"),
        ("false_complete", "completion"),
        ("forbidden_claim", "completion"),
        ("before_prerequisite", "preconditions"),
        ("failed_prerequisite", "preconditions"),
    ],
)
def test_failure_dimensions_stay_separate(suite, record, change, metric):
    if change == "route":
        record["route"] = "security_audit"
    elif change == "arguments":
        record["calls"][1]["arguments"]["hours"] = 72
    elif change == "missing_tool":
        del record["calls"][1]
    elif change == "missing_fact":
        record["completion"]["facts"] = ["report_ready"]
    elif change == "false_complete":
        record["completion"]["status"] = "complete"
    elif change == "forbidden_claim":
        record["completion"]["facts"].append("no_attack_proven")
    elif change == "before_prerequisite":
        record["calls"].insert(0, record["calls"].pop())
    elif change == "failed_prerequisite":
        record["calls"][0]["outcome"] = "error"
    assert not grade_record(suite["cases"][0], record)["metrics"][metric]


def test_later_correct_call_does_not_erase_bad_arguments(suite, record):
    bad = copy.deepcopy(record["calls"][1])
    bad["arguments"]["hours"] = "24"
    record["calls"].insert(1, bad)
    assert not grade_record(suite["cases"][0], record)["metrics"]["arguments"]


def test_blocked_optional_allowed_tool_is_not_hidden_by_required_call_success(
    suite, record
):
    record["calls"].append(
        {
            "phase": "collect",
            "tool": "audit_iam",
            "arguments": {"unexpected": 1},
            "outcome": "blocked",
            "facts": [],
            "rejection": {"status": "rejected", "reason": "unknown_argument"},
        }
    )
    result = grade_record(suite["cases"][0], record)
    assert result["metrics"]["tools"]
    assert not result["metrics"]["arguments"]
    assert not result["metrics"]["preconditions"]
    assert "audit_iam:runtime_rejected" in result["violations"]["preconditions"]


def test_unrelated_presented_tool_fails_even_if_never_called(suite, record):
    baseline = _run(suite, record)
    candidate = copy.deepcopy(baseline)
    candidate["records"][0]["presentations"][0]["tools"].append("restore_database")
    result = compare_runs(suite, baseline, candidate)
    assert result["candidate"]["metrics"]["route"] == 1.0
    assert result["candidate"]["violations"]["presented"] == 1
    assert not result["safety_passed"]
    assert not result["passed"]


def test_fixed_collector_cannot_expose_its_tools_to_a_model(suite, record):
    record["presentations"][0] = {
        "phase": "collect",
        "tools": ["get_run_context", "investigate_access"],
    }
    result = grade_record(suite["cases"][0], record)
    assert "collect:<model invocation>" in result["violations"]["presented"]


def test_minimum_sample_gate_cannot_be_lowered(suite, record):
    with pytest.raises(InvalidEvaluation, match="cannot lower"):
        evaluate_run(suite, _run(suite, record), min_samples=1)


@pytest.mark.parametrize("outcome,executed", [("blocked", 0), ("error", 1), ("ok", 1)])
def test_unauthorized_attempts_and_execution_are_distinct(
    suite, record, outcome, executed
):
    record["calls"].append(
        {
            "phase": "analysis",
            "tool": "restore_database",
            "arguments": {},
            "outcome": outcome,
            "facts": [],
        }
    )
    result = grade_record(suite["cases"][0], record)
    assert len(result["violations"]["attempted"]) == 1
    assert len(result["violations"]["executed"]) == executed
    assert not result["metrics"]["tools"]


@pytest.mark.parametrize(
    "fault,match",
    [
        ("samples", "insufficient samples"),
        ("empty", "nonempty"),
        ("duplicate", "duplicate record"),
        ("unknown_case", "unknown case"),
        ("hash", "fingerprint"),
        ("condition", "conditions missing"),
        ("trace", "complete trace"),
        ("presentation", "one presentation"),
        ("evidence", "evidence required"),
        ("boolean_sample", "nonnegative integer"),
    ],
)
def test_missing_or_invalid_evidence_cannot_pass(suite, record, fault, match):
    run = _run(suite, record)
    if fault == "samples":
        run["records"].pop()
    elif fault == "empty":
        run["records"] = []
    elif fault == "duplicate":
        run["records"].append(copy.deepcopy(run["records"][0]))
    elif fault == "unknown_case":
        run["records"][0]["case_id"] = "unknown"
    elif fault == "hash":
        run["suite_sha256"] = "another-dataset"
    elif fault == "condition":
        del run["conditions"]["clock"]
    elif fault == "trace":
        run["records"][0]["trace_complete"] = False
    elif fault == "presentation":
        run["records"][0]["presentations"] = []
    elif fault == "evidence":
        run["records"][0]["evidence"] = []
    elif fault == "boolean_sample":
        run["records"][0]["sample"] = True
    with pytest.raises(InvalidEvaluation, match=match):
        evaluate_run(suite, run)


@pytest.mark.parametrize(
    "change,match",
    [
        ("conditions", "fixed conditions differ"),
        ("mode", "execution modes differ"),
        ("pairing", "case samples differ"),
    ],
)
def test_incomparable_runs_fail_closed(suite, record, change, match):
    baseline = _run(suite, record)
    candidate = copy.deepcopy(baseline)
    if change == "conditions":
        candidate["conditions"]["model_config"]["temperature"] = 1
    elif change == "mode":
        candidate["mode"] = "live_model"
    else:
        candidate["records"][2]["sample"] = 3
    with pytest.raises(InvalidEvaluation, match=match):
        compare_runs(suite, baseline, candidate)


def test_missing_case_is_not_hidden_by_other_case_samples(suite, record):
    other = copy.deepcopy(suite["cases"][0])
    other["case_id"] = "second"
    suite["cases"].append(other)
    with pytest.raises(InvalidEvaluation, match="second"):
        evaluate_run(suite, _run(suite, record, samples=10))


def test_cli_writes_comparison_and_invalid_input_exits_two(suite, record, tmp_path):
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(suite))
    loaded = load_suite(suite_path)
    run = _run(loaded, record)
    baseline, candidate, output = [
        tmp_path / name
        for name in ("baseline.json", "candidate.json", "comparison.json")
    ]
    baseline.write_text(json.dumps(run))
    candidate.write_text(json.dumps(run))
    args = [
        "--suite",
        str(suite_path),
        "--baseline",
        str(baseline),
        "--candidate",
        str(candidate),
        "--output",
        str(output),
    ]
    assert main(args) == 0
    assert json.loads(output.read_text())["passed"]
    run["records"][0]["route"] = "wrong"
    candidate.write_text(json.dumps(run))
    assert main(args) == 1
    candidate.write_text("{")
    assert main(args) == 2


def test_project_suite_covers_incremental_risks_without_modifying_adk_cases():
    suite = load_suite(Path("tests/eval/routing/cases.json"))
    ids = {case["case_id"] for case in suite["cases"]}
    assert {
        "unknown_job",
        "backup_inspection_not_creation",
        "backup_creation_not_inspection",
        "access_partial",
        "access_permission_denied",
    } <= ids
    assert (
        len(
            json.loads(Path("tests/eval/datasets/basic-dataset.json").read_text())[
                "eval_cases"
            ]
        )
        == 5
    )
    assert (
        len(
            json.loads(Path("tests/eval/holdout/holdout-dataset.json").read_text())[
                "eval_cases"
            ]
        )
        == 2
    )
