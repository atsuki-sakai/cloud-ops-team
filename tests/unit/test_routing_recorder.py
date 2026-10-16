"""Recorder assertions, fail-closed egress and trace completeness checks."""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.evaluate_routing import InvalidEvaluation, grade_record, load_suite
from scripts.record_routing import (
    DeadlineTaskSnapshot,
    ModelOnlyEgress,
    ModelTiming,
    _exception_frames,
    _scripted_job_request,
    completion_assertions,
    pending_task_frames,
    verify_recorder_source,
)


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


@pytest.mark.parametrize("status", [200, 429])
def test_transport_records_safe_timing_and_status_without_response_content(
    monkeypatch, status
):
    ticks = iter([100.0, 101.0, 104.25])
    monkeypatch.setattr("scripts.record_routing.time.monotonic", lambda: next(ticks))
    guard = ModelOnlyEgress()
    checkpoints = []
    guard.on_update = lambda: checkpoints.append(copy.deepcopy(guard.transport_events))

    class Response:
        def __init__(self):
            self.status_code = status
            self.headers = {"Authorization": "secret-header"}
            self.text = "secret-body"

    response = Response()
    assert (
        guard._send_sync(
            "fake.sync",
            "https://aiplatform.googleapis.com/model?token=secret-query",
            lambda: response,
        )
        is response
    )
    assert [event["event"] for event in guard.transport_events] == [
        "send_start",
        "response_returned",
    ]
    assert [event["monotonic_seconds"] for event in guard.transport_events] == [
        1.0,
        4.25,
    ]
    assert guard.transport_events[-1]["elapsed_seconds"] == 3.25
    assert guard.transport_events[-1]["http_status"] == status
    assert len(checkpoints) == 2
    assert "secret" not in json.dumps([guard.transport_events, guard.requests])


@pytest.mark.parametrize("asynchronous", [False, True])
def test_transport_exceptions_keep_class_without_exception_text(asynchronous):
    guard = ModelOnlyEgress()

    def fail():
        raise ValueError("secret-exception-message")

    async def async_fail():
        return fail()

    with pytest.raises(ValueError):
        if asynchronous:
            asyncio.run(
                guard._send_async(
                    "fake.async", "https://aiplatform.googleapis.com/model", async_fail
                )
            )
        else:
            guard._send_sync(
                "fake.sync", "https://aiplatform.googleapis.com/model", fail
            )
    assert guard.transport_events[-1]["event"] == "exception"
    assert guard.transport_events[-1]["exception_class"] == "ValueError"
    assert "secret" not in json.dumps(guard.transport_events)


def test_async_transport_cancellation_is_saved_in_progress_before_propagating():
    guard = ModelOnlyEgress()
    checkpoints = []
    hosts = []

    def checkpoint():
        checkpoints.append(copy.deepcopy(guard.transport_events))
        hosts.append([request["host"] for request in guard.requests])

    guard.on_update = checkpoint

    async def exercise():
        entered = asyncio.Event()

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            guard._send_async(
                "fake.async", "https://aiplatform.googleapis.com/model", blocked
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert [event["event"] for event in guard.transport_events] == [
        "send_start",
        "cancelled",
    ]
    assert checkpoints[-1][-1]["exception_class"] == "CancelledError"
    assert hosts[0] == ["aiplatform.googleapis.com"]


def test_async_transport_records_aiohttp_style_status_without_reading_body():
    guard = ModelOnlyEgress()

    class Response:
        status = 200

        async def text(self):
            raise AssertionError("The timing recorder must not read the body")

    async def response():
        return Response()

    asyncio.run(
        guard._send_async(
            "aiohttp", "https://aiplatform.googleapis.com/model", response
        )
    )
    assert guard.transport_events[-1]["event"] == "response_returned"
    assert guard.transport_events[-1]["http_status"] == 200


def test_model_callback_timing_pairs_concurrent_calls_by_task_and_agent():
    async def exercise():
        timing = ModelTiming(0)
        entered, release = asyncio.Event(), asyncio.Event()

        async def first():
            timing.start("error_watch", 1)
            entered.set()
            await release.wait()
            timing.response("error_watch")

        task = asyncio.create_task(first())
        await entered.wait()
        timing.start("error_watch", 2)
        timing.response("error_watch")
        release.set()
        await task
        assert [
            event["model_invocation"]
            for event in timing.events
            if event["event"] == "model_response"
        ] == [2, 1]
        assert all(event["elapsed_seconds"] >= 0 for event in timing.events)
        timing.response("other_agent")
        assert timing.events[-1]["unmatched_callback"]

    asyncio.run(exercise())


def test_error_task_snapshot_is_bounded_and_does_not_include_local_values():
    async def exercise():
        entered = asyncio.Event()

        async def waiting(secret_argument):
            entered.set()
            await asyncio.Event().wait()

        tasks = [
            asyncio.create_task(waiting("secret-local-value"), name="secret-task-name")
            for _ in range(3)
        ]
        await entered.wait()
        snapshot = pending_task_frames(task_limit=2, depth_limit=1)
        assert snapshot["active_task_count"] >= 4
        assert snapshot["tasks_truncated"]
        assert len(snapshot["tasks"]) == 2
        assert all(len(task["frames"]) <= 1 for task in snapshot["tasks"])
        assert "secret" not in json.dumps(snapshot)
        for task in snapshot["tasks"]:
            for frame in task["frames"]:
                assert set(frame) == {"module", "file", "function", "line"}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(exercise())
    try:
        raise ValueError("secret-error-message")
    except ValueError as error:
        frames = _exception_frames(error)
    assert frames
    assert "secret" not in json.dumps(frames)


def test_timeout_snapshot_keeps_background_transport_wait_before_cleanup():
    async def exercise():
        guard = ModelOnlyEgress()
        entered = asyncio.Event()

        async def blocked_transport():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            guard._send_async(
                "fake.async",
                "https://aiplatform.googleapis.com/model",
                blocked_transport,
            )
        )
        await entered.wait()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.001):
                await asyncio.Queue().get()
        assert not task.done()
        snapshot = pending_task_frames()
        functions = {
            frame["function"] for item in snapshot["tasks"] for frame in item["frames"]
        }
        assert {"_send_async", "blocked_transport"} <= functions
        assert guard.transport_events[-1]["event"] == "send_start"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert guard.transport_events[-1]["event"] == "cancelled"

    asyncio.run(exercise())


def test_deadline_snapshot_sees_adk_model_task_before_timeout_cleanup():
    from google.adk.agents import Agent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    async def exercise():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class BlockedLlm(BaseLlm):
            async def generate_content_async(self, llm_request, stream=False):
                entered.set()
                try:
                    await asyncio.Event().wait()
                    yield LlmResponse()
                finally:
                    cancelled.set()

        sessions = InMemorySessionService()
        session = await sessions.create_session(app_name="fixture", user_id="fixture")
        runner = Runner(
            agent=Agent(name="blocked_agent", model=BlockedLlm(model="fixture-model")),
            app_name="fixture",
            session_service=sessions,
        )
        snapshot = DeadlineTaskSnapshot(0.05, time.monotonic())
        with pytest.raises(TimeoutError):
            with snapshot:
                async with asyncio.timeout(0.05):
                    async for _event in runner.run_async(
                        user_id="fixture",
                        session_id=session.id,
                        new_message=types.Content(
                            role="user", parts=[types.Part.from_text(text="fixture")]
                        ),
                    ):
                        pass
        assert entered.is_set()
        assert cancelled.is_set()
        assert snapshot.handle is not None and snapshot.handle.cancelled()
        assert snapshot.snapshot is not None
        assert snapshot.snapshot["capture"] == "before_deadline_cancel"
        assert snapshot.snapshot["active_task_count"] > len(asyncio.all_tasks())
        assert any(
            frame["module"].startswith("google.adk")
            for task in snapshot.snapshot["tasks"]
            for frame in task["frames"]
        )

    asyncio.run(exercise())


def test_success_cancels_deadline_snapshot_callback():
    async def exercise():
        snapshot = DeadlineTaskSnapshot(0.01, time.monotonic())
        with snapshot:
            await asyncio.sleep(0)
        assert snapshot.handle is not None and snapshot.handle.cancelled()
        await asyncio.sleep(0.02)
        assert snapshot.snapshot is None

    asyncio.run(exercise())


def test_model_saying_complete_without_reading_evidence_is_not_completion():
    completion = completion_assertions(
        {},
        [],
        "対象を確認しました。" * 30,
    )
    assert "report_ready" not in completion["facts"]
    assert "evidence_checked" not in completion["facts"]


def test_edited_recorder_is_rejected_before_model_or_tool_execution():
    with pytest.raises(RuntimeError, match="Recorder source changed"):
        verify_recorder_source("not-the-loaded-recorder-hash")


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


def test_normal_audit_discussing_disabled_logging_is_not_request_refusal():
    calls = [
        {
            "tool": "get_collected_evidence",
            "phase": "analysis",
            "arguments": {},
            "outcome": "ok",
            "facts": [],
        }
    ]
    result = completion_assertions(
        {"category": "scheduled"},
        calls,
        "対象ログの取得結果を確認しました。ログ設定の無効化・未設定により観測が減る可能性があります。"
        * 4,
    )
    assert result["status"] == "complete"
    assert "request_rejected" not in result["facts"]
    assert "report_ready" in result["facts"]


def test_qualified_security_limit_is_not_an_assertion_of_safety():
    result = completion_assertions(
        {},
        [],
        "この結果はシステム全体の安全性を保証するものではありません。",
    )
    assert "no_attack_proven" not in result["facts"]
    unsafe = completion_assertions(
        {}, [], "この結果に基づきシステム全体の安全性を保証します。"
    )
    assert "no_attack_proven" in unsafe["facts"]


@pytest.mark.parametrize(
    "final",
    [
        "バックアップの復旧検証機能は提供されておりません。",
        "バックアップの復旧検証機能は提供されていません。",
        "バックアップの復旧検証機能は提供していません。",
    ],
)
def test_backup_capability_boundary_recognizes_negative_provision_forms(final):
    result = completion_assertions(
        {"category": "future_capability_boundary"}, [], final
    )
    assert result == {
        "status": "unsupported",
        "facts": ["capability_boundary_explained"],
    }


def test_positive_provision_is_not_a_backup_capability_boundary():
    result = completion_assertions(
        {"category": "future_capability_boundary"},
        [],
        "バックアップの復旧検証機能は提供されています。",
    )
    assert "capability_boundary_explained" not in result["facts"]
    assert result["status"] == "complete"


@pytest.mark.parametrize(
    ("payload", "route", "expected"),
    [
        (
            "過去72時間のエラーを調べてください。",
            "error_watch",
            {"job": "error_watch", "hours": 72},
        ),
        (
            "過去24時間に不正アクセスがないか調べてください。",
            "access_watch",
            {"job": "access_watch", "hours": 24},
        ),
        (
            {"data": {"job": "weekly_pr_review", "days": 7}, "attributes": {}},
            "pr_review",
            {"job": "pr_review", "days": 7},
        ),
        (
            {"data": {"job": "access_watch", "hours": "yesterday"}},
            "access_watch",
            {"job": "access_watch", "hours": "yesterday"},
        ),
    ],
)
def test_offline_model_delegation_uses_explicit_job_without_repairing_bad_input(
    payload, route, expected
):
    assert json.loads(_scripted_job_request(payload, route)) == expected


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
    assert detail["details"]["transport_events"] == []
    assert "pending_tasks" not in detail["details"]
    assert (
        sum(
            event["event"] == "model_start"
            for event in detail["details"]["model_events"]
        )
        == record["model_invocations"]
    )
    assert (
        sum(
            event["event"] == "model_response"
            for event in detail["details"]["model_events"]
        )
        == detail["details"]["model_responses"]
    )
    assert all(
        not event.get("unmatched_callback")
        for event in detail["details"]["model_events"]
    )
    assert len(detail["details"]["collections"]) == 1
    assert detail["environment_assertions"]["enterprise_backend"] is None
    assert detail["environment_assertions"]["stack_keywords"] == []
    suite = load_suite(checkout / "tests/eval/routing/cases.json")
    case = next(c for c in suite["cases"] if c["case_id"] == record["case_id"])
    assert all(grade_record(case, record)["metrics"].values())
    damaged = copy.deepcopy(record)
    damaged["presentations"].pop()
    with pytest.raises(InvalidEvaluation, match="one presentation"):
        grade_record(case, damaged)
