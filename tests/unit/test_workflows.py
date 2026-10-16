"""Fixed collection must be scoped, bounded, and independent of model output."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace

import pytest

from app import tools
from app.specialists import all_jobs
from app.workflows import MAX_EVIDENCE_CHARACTERS, collection_steps, prepare_job


def _job(name):
    return next((specialist, job) for specialist, job in all_jobs() if job.name == name)


def _fake_collectors(monkeypatch, job, params, overrides=None):
    seen = []
    overrides = overrides or {}
    for step in collection_steps(job.name, params):
        name = step.name

        def fake(_name=name, **kwargs):
            seen.append((_name, kwargs))
            result = overrides.get(_name)
            if isinstance(result, Exception):
                raise result
            if result is not None:
                return result
            if _name == "get_run_context":
                return {
                    "target": "example-project",
                    "provider": "gcp",
                    "dry_run": True,
                    "capabilities": [
                        "investigate_access",
                        "list_services",
                        "audit_network",
                    ],
                    "unsupported_capabilities": [],
                }
            return {"status": "ok", "complete": True, "source": _name}

        monkeypatch.setattr(tools, name, fake)
    return seen


@pytest.mark.parametrize(
    "name",
    [
        "error_watch",
        "security_audit",
        "pr_review",
        "access_watch",
        "infrastructure_review",
    ],
)
def test_fixed_collectors_bind_real_signatures_and_are_in_specialist_scope(name):
    specialist, job = _job(name)
    params = {key: {"hours": 24, "days": 7, "kev_days": 30}[key] for key in job.params}
    names = {tool.__name__ for tool in specialist.tools}
    for step in collection_steps(job.name, params):
        assert step.name in names
        inspect.signature(getattr(tools, step.name)).bind(**step.arguments)


@pytest.mark.parametrize(
    "name",
    [
        "error_watch",
        "security_audit",
        "pr_review",
        "access_watch",
        "infrastructure_review",
    ],
)
def test_collects_once_and_removes_fixed_collectors_from_model_tools(monkeypatch, name):
    specialist, job = _job(name)
    params = {key: {"hours": 24, "days": 7, "kev_days": 30}[key] for key in job.params}
    seen = _fake_collectors(monkeypatch, job, params)
    prepared = prepare_job(specialist, job, params)
    steps = collection_steps(job.name, params)
    assert seen == [(s.name, dict(s.arguments)) for s in steps]
    analysis_names = {tool.__name__ for tool in prepared.analysis_specialist.tools}
    assert not analysis_names.intersection(s.name for s in steps)
    assert "get_collected_evidence" in analysis_names
    assert len(analysis_names) < len(specialist.tools)
    evidence = prepared.analysis_specialist.tools[0]()
    assert evidence["status"] == "ok"
    assert evidence["context"]["target"] == "example-project"
    assert evidence["parameters"] == params
    assert len(evidence["sha256"]) == 64
    evidence["checks"].clear()
    assert prepared.analysis_specialist.tools[0]()["checks"]


def test_partial_and_failed_sources_are_not_a_clean_audit(monkeypatch):
    specialist, job = _job("infrastructure_review")
    _fake_collectors(
        monkeypatch,
        job,
        {},
        {
            "list_optimization_recommendations": {
                "status": "ok",
                "sources": [{"complete": False}],
            },
            "audit_iam": RuntimeError("password=this-must-not-be-returned"),
            "audit_network": {"status": "permission_denied"},
        },
    )
    out = prepare_job(specialist, job, {}).evidence
    assert out["status"] == "partial"
    assert out["checks"]["audit_iam"]["status"] == "error"
    assert "this-must-not-be-returned" not in json.dumps(out)
    assert out["checks"]["audit_network"]["status"] == "permission_denied"


def test_evidence_is_bounded_and_sensitive_strings_are_redacted(monkeypatch):
    specialist, job = _job("infrastructure_review")
    _fake_collectors(
        monkeypatch,
        job,
        {},
        {
            "audit_iam": {
                "status": "ok",
                "note": "password=very-private-value",
                "count": 2**62,
            },
            "audit_network": {"status": "ok", "items": ["x" * 20_000] * 10},
        },
    )
    out = prepare_job(specialist, job, {}).evidence
    encoded = json.dumps(out, ensure_ascii=False)
    assert len(encoded) < MAX_EVIDENCE_CHARACTERS
    assert "very-private-value" not in encoded
    assert out["checks"]["audit_iam"]["count"] == 2**62
    assert out["checks"]["audit_network"]["complete"] is False
    assert out["status"] == "partial"


def test_missing_context_stops_all_cloud_and_github_collectors(monkeypatch, caplog):
    specialist, job = _job("pr_review")
    seen = _fake_collectors(
        monkeypatch,
        job,
        {"days": 7},
        {"get_run_context": {"status": "ok", "target": ""}},
    )
    with caplog.at_level("INFO", logger="app.workflows"):
        prepared = prepare_job(specialist, job, {"days": 7})
    assert seen == [("get_run_context", {})]
    assert prepared.evidence["status"] == "partial"
    assert all(x["status"] == "error" for x in prepared.evidence["checks"].values())
    records = [json.loads(record.message) for record in caplog.records]
    collected = [record for record in records if record["event"] == "ops_collection"]
    assert [record["executed"] for record in collected] == [True, False, False, False]
    assert [record["executed"] for record in prepared.evidence["collection_calls"]] == [
        True,
        False,
        False,
        False,
    ]
    assert records[-1]["sha256"] == prepared.evidence["sha256"]


@pytest.mark.parametrize(
    "params",
    [
        {"hours": True},
        {"hours": "24"},
        {"hours": 0},
        {"hours": 169},
        {"hours": 24, "target": "other"},
    ],
)
def test_invalid_arguments_fail_before_any_collection(monkeypatch, params):
    specialist, job = _job("access_watch")
    seen = _fake_collectors(monkeypatch, job, {"hours": 24})
    with pytest.raises(ValueError):
        prepare_job(specialist, job, params)
    assert not seen


def test_unregistered_or_out_of_scope_workflow_is_rejected(monkeypatch):
    specialist, job = _job("access_watch")
    seen = _fake_collectors(monkeypatch, job, {"hours": 24})
    with pytest.raises(ValueError):
        prepare_job(replace(specialist, tools=[]), job, {"hours": 24})
    other = _job("pr_review")[1]
    with pytest.raises(ValueError):
        prepare_job(specialist, other, {"days": 7})
    assert not seen
