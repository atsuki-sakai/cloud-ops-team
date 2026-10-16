"""Review regressions with synthetic secrets and no network or model calls."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from app.core.fingerprint import parse_meta_comment
from app.core.redact import redact, redact_obj
from app.providers.gcp import observability as obs
from app.tools import issues


@pytest.mark.parametrize("key", ["password", "client_secret", "api-key", "TOKEN"])
@pytest.mark.parametrize("secret", ["SyntheticSecret", 'space and "quote"\\tail', "x"])
def test_json_secret_values_are_removed_without_breaking_json(key, secret):
    raw = json.dumps({"nested": {key: secret}, "ordinary": "keep this"})
    clean = redact(raw)
    assert json.loads(clean) == {
        "nested": {key: "[REDACTED]"},
        "ordinary": "keep this",
    }
    assert redact(clean) == clean


def test_repr_and_structured_secret_keys_are_redacted():
    assert "SyntheticSecret" not in redact("{'password': 'SyntheticSecret'}")
    value = {"nested": [{"password": "x", "client_secret": 123456}], "units": 2**62}
    assert redact_obj(value) == {
        "nested": [{"password": "[REDACTED]", "client_secret": "[REDACTED]"}],
        "units": 2**62,
    }


@pytest.mark.parametrize(
    "secret",
    [
        ["first secret", "SECOND_SECRET"],
        {"a": "FIRST_SECRET", "b": "SECOND_SECRET"},
        "prefix " + "private word " * 100,
    ],
)
def test_secret_containers_and_truncated_json_are_fully_masked(secret):
    raw = json.dumps({"password": secret, "ordinary": "keep"})
    assert json.loads(redact(raw)) == {"password": "[REDACTED]", "ordinary": "keep"}
    # _message_of can bound a structured error before _compact redacts it.
    entry = {"jsonPayload": {"error": {"password": secret}}}
    assert "private word" not in obs._compact(entry)["message"]
    assert "SECOND_SECRET" not in obs._compact(entry)["message"]
    assert "FIRST_SECRET" not in obs._compact(entry)["message"]
    cut = redact(raw[:30])
    assert "SECOND_SECRET" not in cut and "FIRST_SECRET" not in cut


def test_nested_json_log_and_group_contain_no_secret(monkeypatch):
    entry = {
        "resource": {"type": "cloud_run_revision", "labels": {"service_name": "api"}},
        "severity": "ERROR",
        "timestamp": "2026-10-16T00:00:00Z",
        "jsonPayload": {"error": {"password": "SyntheticSecret"}},
    }
    monkeypatch.setattr(obs, "_list_entries", lambda *a, **kw: (True, [entry]))
    out = obs.summarize_error_groups(24)
    assert "SyntheticSecret" not in json.dumps(out)
    assert out["groups"][0]["sample_message"]


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("report", [True, False])
def test_issue_and_report_outbound_fields_are_redacted(monkeypatch, dry_run, report):
    monkeypatch.setattr(
        issues,
        "CONFIG",
        dataclasses.replace(
            issues.CONFIG, dry_run=dry_run, target="prod", github_repo="example/test"
        ),
    )
    monkeypatch.setattr(
        issues, "find_existing_ops_issue", lambda _: {"status": "not_found"}
    )
    monkeypatch.setattr(issues, "_ensure_labels", lambda _: (True, []))
    writes = []

    def post(method, path, *, json):
        assert not dry_run
        assert method == "POST"
        writes.append(json)
        return True, {"number": 7, "html_url": "https://example.invalid/7"}

    monkeypatch.setattr(issues, "gh_rest", post)
    context = SimpleNamespace(state={})
    title = "failure token=SyntheticSecret"
    body = '{"password": "SyntheticSecret"}'
    if report:
        out = issues.publish_report("weekly", title, body, context)
    else:
        out = issues.create_ops_issue(
            "error",
            title,
            body,
            "high",
            "1234567890123456",
            "api",
            "/reset/token=SyntheticSecret",
            context,
        )
    assert "SyntheticSecret" not in json.dumps([out, writes])
    if not report:
        meta = parse_meta_comment(out["body_preview"] if dry_run else writes[0]["body"])
        assert meta["fingerprint"] == "1234567890123456"
        assert meta["service"] == "api" and meta["target"] == "prod"
        assert meta["search_text"] == ""


@pytest.mark.parametrize("field", ["service", "fingerprint"])
def test_unsafe_identifiers_fail_before_github_access(monkeypatch, field):
    def forbidden(*args, **kwargs):
        pytest.fail("Unsafe identifiers must not reach GitHub")

    monkeypatch.setattr(issues, "gh_rest", forbidden)
    arguments = {
        "kind": "error",
        "title": "failure",
        "body": "evidence",
        "severity": "high",
        "fingerprint": "safe-fingerprint",
        "service": "api",
        "search_text": "request failed",
        "tool_context": SimpleNamespace(state={}),
    }
    arguments[field] = "password=SyntheticSecret"
    out = issues.create_ops_issue(**arguments)
    assert out["status"] == "invalid_input"
    assert "SyntheticSecret" not in json.dumps(out)


@pytest.mark.parametrize(
    "search", ["token=SyntheticSecret", "[REDACTED]", "mail ***@example.com"]
)
def test_unsafe_legacy_search_cannot_prove_zero_errors(monkeypatch, search):
    def forbidden(*args, **kwargs):
        pytest.fail("An unsafe or masked search must not query logs")

    monkeypatch.setattr(obs, "_list_entries", forbidden)
    out = obs.count_matching_errors("api", search, "2026-10-15T00:00:00Z")
    assert out["status"] == "error" and out["complete"] is False
    assert "count" not in out and "SyntheticSecret" not in json.dumps(out)


def test_safe_search_is_kept_as_an_exact_literal(monkeypatch):
    monkeypatch.setattr(
        issues, "find_existing_ops_issue", lambda _: {"status": "not_found"}
    )
    monkeypatch.setattr(
        issues, "CONFIG", dataclasses.replace(issues.CONFIG, dry_run=True)
    )
    out = issues.create_ops_issue(
        "error",
        "failure",
        "evidence",
        "high",
        "deadbeefdeadbeef",
        "api",
        "request failed",
        SimpleNamespace(state={}),
    )
    assert parse_meta_comment(out["body_preview"])["search_text"] == "request failed"


@pytest.mark.parametrize("auto_close", [False, True])
def test_already_closed_issue_gets_verification_without_another_close(
    monkeypatch, auto_close
):
    monkeypatch.setattr(
        issues,
        "CONFIG",
        dataclasses.replace(issues.CONFIG, dry_run=False, auto_close_issues=auto_close),
    )
    monkeypatch.setattr(
        issues, "_get_scoped_issue", lambda _: (True, {"state": "closed"})
    )
    monkeypatch.setattr(issues, "_ensure_labels", lambda _: (True, []))
    writes = []

    def post(method, path, *, json):
        assert method == "POST"
        writes.append((path, json))
        return True, {}

    monkeypatch.setattr(issues, "gh_rest", post)
    assert issues.close_issue(7, "verified")["status"] == "verified_already_closed"
    assert len(writes) == 2
    assert "OPS_AUTO_CLOSE_ISSUES=false" not in writes[0][1]["body"]


def test_weekly_fixed_collection_includes_closed_unverified_findings(monkeypatch):
    from app import tools, workflows
    from app.specialists import all_jobs

    now = dt.datetime(2026, 10, 16, tzinfo=dt.UTC)
    monkeypatch.setattr(workflows, "utcnow", lambda: now)
    specialist, job = next((s, j) for s, j in all_jobs() if j.name == "pr_review")
    rows = [
        {"number": 1, "kind": "error", "state": "open"},
        {
            "number": 2,
            "kind": "error",
            "state": "closed",
            "closed_at": "2026-10-14T00:00:00Z",
        },
        {
            "number": 3,
            "kind": "security",
            "state": "closed",
            "closed_at": "2026-10-14T00:00:00Z",
            "labels": ["ops:verified"],
        },
        {"number": 4, "kind": "report", "state": "open", "body": "x" * 100_000},
        {
            "number": 5,
            "kind": "error",
            "state": "closed",
            "closed_at": "2026-09-01T00:00:00Z",
        },
    ]
    seen = []
    for step in workflows.collection_steps(job.name, {"days": 7}):

        def fake(_name=step.name, **kwargs):
            seen.append((_name, kwargs))
            if _name == "get_run_context":
                return {"target": "prod", "provider": "gcp", "dry_run": True}
            if _name == "list_ops_issues":
                assert kwargs == {"state": "all", "kind": "all"}
                return {
                    "status": "ok",
                    "complete": True,
                    "issues": rows,
                    "count": len(rows),
                }
            return {"status": "ok", "complete": True}

        monkeypatch.setattr(tools, step.name, fake)
    prepared = workflows.prepare_job(specialist, job, {"days": 7})
    evidence = prepared.evidence
    candidates = evidence["checks"]["list_ops_issues"]
    assert candidates["status"] == "ok" and candidates["count"] == 2
    assert [i["number"] for i in candidates["issues"]] == [1, 2]
    assert evidence["status"] == "ok"
    assert len([name for name, _ in seen if name == "list_ops_issues"]) == 1
    # Legacy list APIs retain explicit state semantics; only the workflow selects candidates.
    assert {tool.__name__ for tool in prepared.analysis_specialist.tools}.isdisjoint(
        {"list_ops_issues"}
    )
