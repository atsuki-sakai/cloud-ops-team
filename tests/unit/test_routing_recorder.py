"""Recorder assertions, fail-closed egress and trace completeness checks."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.evaluate_routing import InvalidEvaluation, grade_record, load_suite
from scripts.record_routing import ModelOnlyEgress, completion_assertions


@pytest.mark.parametrize(
    "url",
    [
        "https://logging.googleapis.com/v2/entries:list",
        "https://api.github.com/repos/example/fixture/issues",
        "https://compute.googleapis.com/compute/v1/projects/fixture-project",
        "http://aiplatform.googleapis.com/model",
        "https://aiplatform.googleapis.com.attacker.example/model",
        "http://169.254.169.254/computeMetadata/v1/",
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/example:signBlob",
    ],
)
def test_non_model_egress_is_rejected_before_request(url):
    with pytest.raises(PermissionError):
        ModelOnlyEgress().check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://aiplatform.googleapis.com/v1/projects/model-project/locations/global/publishers/google/models/model:generateContent",
        "https://us-central1-aiplatform.googleapis.com/v1/model:generateContent",
        "https://oauth2.googleapis.com/token",
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/example:generateAccessToken",
    ],
)
def test_only_model_and_required_adc_authentication_hosts_are_allowed(url):
    guard = ModelOnlyEgress()
    guard.check_url(url)
    assert len(guard.requests) == 1


def test_model_saying_complete_without_reading_evidence_is_not_completion():
    completion = completion_assertions(
        {},
        [],
        "対象を確認しました。" * 30,
    )
    assert "report_ready" not in completion["facts"]
    assert "evidence_checked" not in completion["facts"]


def test_fixed_collection_alone_does_not_prove_model_read_the_evidence():
    completion = completion_assertions(
        {},
        [
            {
                "tool": "investigate_access",
                "phase": "collect",
                "arguments": {"hours": 24},
                "outcome": "ok",
                "facts": [],
            }
        ],
        "対象を確認しました。" * 30,
    )
    assert not completion["facts"]


def test_natural_language_cannot_regress_to_unrestricted_specialist_tools():
    suite = load_suite(Path("tests/eval/routing/cases.json"))
    cases = {case["case_id"]: case for case in suite["cases"]}
    for natural, scheduled in [
        ("natural_error_similar", "scheduled_error_watch"),
        ("natural_access_similar", "scheduled_access_watch"),
    ]:
        assert (
            cases[natural]["allowed_tools"]["analysis"]
            == cases[scheduled]["allowed_tools"]["analysis"]
        )
        assert "get_run_context" not in cases[natural]["allowed_tools"]["analysis"]


def test_partial_data_requires_partial_description_in_report():
    calls = [
        {
            "tool": "get_collected_evidence",
            "phase": "analysis",
            "arguments": {},
            "outcome": "ok",
            "facts": [],
        }
    ]
    case = {"fixture": "access-permission-denied-v1"}
    missing = completion_assertions(case, calls, "対象を確認しました。" * 30)
    assert "permission_gap_reported" not in missing["facts"]
    described = completion_assertions(
        case, calls, "権限不足で未取得です。対象の安全性は未確認です。" * 10
    )
    assert {"coverage_incomplete_reported", "permission_gap_reported"} <= set(
        described["facts"]
    )


def test_offline_recorder_captures_fixed_calls_schema_and_detects_missing_trace(
    tmp_path,
):
    checkout = Path(__file__).resolve().parents[2]
    output = tmp_path / "probe.json"
    process = subprocess.run(
        [
            sys.executable,
            str(checkout / "scripts/record_routing.py"),
            "--checkout",
            str(checkout),
            "--revision",
            "test-working-tree",
            "--mode",
            "runtime_stubbed",
            "--samples",
            "1",
            "--case",
            "scheduled_access_watch",
            "--timeout",
            "20",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    run = json.loads(output.read_text())
    record = run["records"][0]
    assert record["trace_complete"]
    assert record["route"] == "access_watch"
    assert {item["tool"] for item in record["calls"] if item["phase"] == "collect"} == {
        "get_run_context",
        "investigate_access",
    }
    assert record["model_invocations"] == len(record["presentations"]) > 0
    detail = json.loads(Path(record["evidence"][0]).read_text())
    assert detail["details"]["network_requests"] == []
    assert len(detail["details"]["collections"]) == 1
    suite = load_suite(checkout / "tests/eval/routing/cases.json")
    case = next(c for c in suite["cases"] if c["case_id"] == record["case_id"])
    assert all(grade_record(case, record)["metrics"].values())
    damaged = copy.deepcopy(record)
    damaged["presentations"].pop()
    with pytest.raises(InvalidEvaluation, match="one presentation"):
        grade_record(case, damaged)
