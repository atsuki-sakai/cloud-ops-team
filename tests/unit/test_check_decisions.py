"""Incident transitions, model-free healthy runs and verdict-preserving reports (offline)."""

from __future__ import annotations

import dataclasses
import json

import pytest
from google.adk.agents import BaseAgent
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app import tools
from app.core.check_decisions import service_check_decision, service_incidents
from app.routing import ScheduledRouter
from app.specialists import SPECIALISTS, all_jobs
from app.workflows import collection_steps, prepare_job


def scenario(id_, outcome, severity="high"):
    return {
        "id": id_,
        "version": "abc",
        "fingerprint": f"service_check:{id_}",
        "outcome": outcome,
        "severity": severity,
        "attempts_made": 1,
    }


def incidents(*fingerprints, status="ok"):
    return {
        "status": status,
        "issues": [
            {"number": 10 + index, "fingerprint": fp, "state": "open"}
            for index, fp in enumerate(fingerprints)
        ],
    }


def test_transitions_against_open_incidents():
    checks = {
        "run_service_checks": {
            "status": "ok",
            "scenarios": [
                scenario("new", "fail"),
                scenario("still", "fail"),
                scenario("back", "pass"),
                scenario("wobbly", "flaky"),
                scenario("blip", "flaky"),
                scenario("fine", "pass"),
                scenario("unknown", "inconclusive"),
                scenario("paused", "maintenance"),
            ],
        },
        "list_ops_issues": incidents(
            "service_check:still",
            "service_check:back",
            "service_check:wobbly",
            "service_check:removed",
        ),
    }
    decision = service_check_decision(checks)
    states = {item["scenario"]: item["transition"] for item in decision["transitions"]}
    assert states == {
        "new": "new_incident",
        "still": "ongoing",
        "back": "recovered",
        "wobbly": "unstable",
        "blip": "transient",
        "fine": "healthy",
        "unknown": "unverified",
        "paused": "not_checked",
        "removed": "orphaned",
    }
    issue = {
        item["scenario"]: item.get("issue_number") for item in decision["transitions"]
    }
    assert issue["back"] == 11 and issue["new"] is None
    assert decision["analysis_required"] is True


def test_all_healthy_needs_no_model_but_unreadable_incidents_are_reported():
    healthy = {
        "run_service_checks": {"status": "ok", "scenarios": [scenario("a", "pass")]},
        "list_ops_issues": incidents(),
    }
    decision = service_check_decision(healthy)
    assert (
        decision["analysis_required"] is False and decision["reason"] == "all_healthy"
    )
    blind = {**healthy, "list_ops_issues": {"status": "error"}}
    assert service_check_decision(blind)["incidents_known"] is False
    failing_blind = {
        "run_service_checks": {"status": "ok", "scenarios": [scenario("a", "flaky")]},
        "list_ops_issues": {"status": "error"},
    }
    assert service_check_decision(failing_blind)["analysis_required"] is True


def test_incident_filter_keeps_only_open_service_check_issues():
    result = service_incidents(
        {
            "status": "ok",
            "issues": [
                {"fingerprint": "service_check:a", "state": "open"},
                {"fingerprint": "0123456789abcdef", "state": "open"},
                {"fingerprint": "service_check:b", "state": "closed"},
            ],
        }
    )
    assert [row["fingerprint"] for row in result["issues"]] == ["service_check:a"]


# --------------------------------------------------------------------------- workflow


def _job(name):
    return next((s, j) for s, j in all_jobs() if j.name == name)


def _fake(monkeypatch, job, params, results):
    for step in collection_steps(job.name, params):

        def fake(_name=step.name, **kwargs):
            if _name == "get_run_context":
                return {
                    "target": "example-project",
                    "provider": "gcp",
                    "capabilities": [],
                }
            return results[_name]

        monkeypatch.setattr(tools, step.name, fake)


def test_healthy_service_check_returns_a_fixed_report_without_a_model(monkeypatch):
    specialist, job = _job("service_check")
    _fake(
        monkeypatch,
        job,
        {"attempts": 2},
        {
            "run_service_checks": {
                "status": "ok",
                "run_id": "r1",
                "suite_version": "s1",
                "control": {"status": "ok"},
                "scenarios": [scenario("top-page", "pass", "info")],
            },
            "list_ops_issues": incidents("0123456789abcdef"),
        },
    )
    prepared = prepare_job(specialist, job, {"attempts": 2})
    assert prepared.evidence["decision"]["analysis_required"] is False
    report = prepared.fixed_report or ""
    assert "top-page" in report and "pass" in report
    assert prepared.evidence["checks"]["list_ops_issues"]["issues"] == []


def test_failing_service_check_goes_to_analysis_with_the_code_verdict(monkeypatch):
    specialist, job = _job("service_check")
    _fake(
        monkeypatch,
        job,
        {"attempts": 2},
        {
            "run_service_checks": {
                "status": "ok",
                "scenarios": [
                    scenario("top-page", "fail") | {"failure": "http_status"}
                ],
            },
            "list_ops_issues": incidents(),
        },
    )
    prepared = prepare_job(specialist, job, {"attempts": 2})
    assert prepared.fixed_report is None
    assert "new_incident" in (prepared.verdict_summary or "")
    names = {tool.__name__ for tool in prepared.analysis_specialist.tools}
    assert "run_service_checks" not in names and "list_ops_issues" not in names
    assert {"create_ops_issue", "comment_on_issue", "get_log_context"} <= names


def test_deploy_verification_without_rollouts_is_model_free(monkeypatch):
    specialist, job = _job("deploy_verification")
    _fake(
        monkeypatch,
        job,
        {"minutes": 60},
        {
            "verify_recent_deployments": {
                "status": "ok",
                "rollouts": [],
                "window_minutes": 60,
            }
        },
    )
    prepared = prepare_job(specialist, job, {"minutes": 60})
    assert prepared.fixed_report is not None
    names = {tool.__name__ for tool in prepared.analysis_specialist.tools}
    assert "verify_recent_deployments" not in names


def test_missing_context_never_skips_analysis_or_reports_healthy(monkeypatch):
    specialist, job = _job("service_check")
    _fake(monkeypatch, job, {"attempts": 2}, {})
    monkeypatch.setattr(tools, "get_run_context", lambda: {"status": "error"})
    prepared = prepare_job(specialist, job, {"attempts": 2})
    assert prepared.fixed_report is None
    assert prepared.evidence["decision"]["analysis_required"] is True


# --------------------------------------------------------------------------- agents


def content(text):
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


class ScriptedAgent(BaseAgent):
    mode_: str = "ok"

    async def _run_async_impl(self, ctx):
        if self.mode_ == "raise":
            raise RuntimeError("model unavailable")
        if self.mode_ == "error_event":
            yield Event(author=self.name, error_code="MODEL_ERROR")
            return
        yield Event(
            author=self.name,
            content=types.Content(
                role="model", parts=[types.Part.from_text(text="全シナリオ正常です")]
            ),
        )


async def run_router(factory, text):
    router = ScheduledRouter(
        name="cloud_ops_team",
        specialists=tuple(SPECIALISTS),
        job_runner_factory=factory,
    )
    service = InMemorySessionService()
    session = await service.create_session(app_name="app", user_id="u")
    runner = Runner(agent=router, app_name="app", session_service=service)
    return [
        event
        async for event in runner.run_async(
            user_id="u", session_id=session.id, new_message=content(text)
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "raise", "error_event"])
async def test_analysis_cannot_replace_or_erase_the_code_verdict(mode):
    from app.agent import VerdictGuardAgent

    def factory(specialist, job, params):
        return VerdictGuardAgent(
            name=specialist.name,
            analysis=ScriptedAgent(name=specialist.name, mode_=mode),
            verdict_summary="| top-page | fail | new_incident |",
            failure_report="| top-page | fail | new_incident |\n\nAI 調査は失敗。原因は未確認。",
        )

    events = await run_router(factory, '{"job":"service_check"}')
    final = events[-1]
    assert final.error_code is None
    text = final.content.parts[0].text
    assert text.startswith("| top-page | fail | new_incident |")
    if mode == "ok":
        assert "全シナリオ正常です" in text  # appended, never replacing the table
    else:
        assert "原因は未確認" in text


@pytest.mark.asyncio
async def test_fixed_report_agent_runs_without_any_model(monkeypatch):
    from app import agent as agent_module

    monkeypatch.setattr(
        agent_module,
        "_model",
        lambda: (_ for _ in ()).throw(AssertionError("no model for healthy runs")),
    )
    _, job = _job("service_check")
    _fake(
        monkeypatch,
        job,
        {"attempts": 2},
        {
            "run_service_checks": {
                "status": "ok",
                "scenarios": [scenario("a", "pass")],
            },
            "list_ops_issues": incidents(),
        },
    )
    events = await run_router(
        agent_module.build_scheduled_agent, '{"job":"service_check"}'
    )
    assert events[-1].error_code is None
    assert "| a |" in events[-1].content.parts[0].text


@pytest.mark.asyncio
async def test_guarded_real_llm_agent_keeps_tool_roundtrip(monkeypatch):
    from app import agent as agent_module
    from tests.unit.test_routing import ToolRoundtripModel

    model = ToolRoundtripModel(model="offline-test")
    monkeypatch.setattr(agent_module, "_model", lambda: model)

    def read_evidence(tool_context: ToolContext) -> dict:
        """Read this job's fixed evidence."""
        return {"status": "ok", "observed": 42}

    def factory(specialist, job, params):
        analysis = agent_module.build_specialist(
            dataclasses.replace(specialist, tools=[read_evidence]), phase="analysis"
        )
        return agent_module.VerdictGuardAgent(
            name=specialist.name,
            analysis=analysis,
            verdict_summary="VERDICT",
            failure_report="VERDICT\n\nfailed",
        )

    events = await run_router(factory, json.dumps({"job": "deploy_verification"}))
    assert events[-1].error_code is None
    assert events[-1].content.parts[0].text.startswith("VERDICT")
    assert events[-1].content.parts[0].text.endswith("Evidence checked")
    assert len(model.requests) == 2
