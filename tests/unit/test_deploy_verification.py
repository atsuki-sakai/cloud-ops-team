"""Deployment verification: serving revision, attributed metrics and verdicts (offline)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.core.deploy_verification import evaluate_metrics, evaluate_rollout
from app.core.service_checks import DEPLOY_DEFAULTS
from app.providers.gcp import rollouts as gcp_rollouts

CFG = dict(DEPLOY_DEFAULTS)


def metrics(
    before_requests=1000,
    before_5xx=2,
    after_requests=500,
    after_5xx=1,
    p95=(200.0, 210.0),
):
    return {
        "status": "ok",
        "before": {"requests": before_requests, "5xx": before_5xx, "p95_ms": p95[0]},
        "after": {"requests": after_requests, "5xx": after_5xx, "p95_ms": p95[1]},
    }


def rollout(**overrides):
    row = {
        "service": "web",
        "revision": "web-00042-abc",
        "create_time": "2026-11-25T00:00:00Z",
        "ready": True,
        "traffic": [
            {"revision": "web-00042-abc", "percent": 100, "tag": "", "uri": ""}
        ],
        "metrics": metrics(),
    }
    row.update(overrides)
    return row


PASS = [{"id": "top-page", "version": "v1", "outcome": "pass"}]


def test_too_few_requests_leave_metrics_unevaluated_instead_of_passing():
    result = evaluate_metrics(metrics(after_requests=5, after_5xx=5), CFG)
    assert result["verdict"] == "insufficient_samples"
    assert "new revision" in result["reason"]
    verdict = evaluate_rollout(rollout(metrics=metrics(after_requests=5)), PASS, CFG)
    assert verdict["verdict"] == "verified"
    assert verdict["metrics_evaluated"] is False
    assert any("unevaluated" in reason for reason in verdict["reasons"])


def test_error_rate_regression_needs_both_relative_and_absolute_increase():
    # 0.2% -> 0.4% doubles but stays under the +1 point floor: not a regression.
    assert evaluate_metrics(metrics(after_5xx=2), CFG)["verdict"] == "ok"
    # 0.2% -> 4%: regression.
    result = evaluate_metrics(metrics(after_5xx=20), CFG)
    assert result["verdict"] == "regressed" and result["regressions"] == ["error_rate"]
    # A large ratio from a couple of errors is not enough evidence.
    assert (
        evaluate_metrics(metrics(before_5xx=0, after_requests=40, after_5xx=2), CFG)[
            "verdict"
        ]
        == "ok"
    )


def test_latency_regression_uses_factor_and_minimum_increase():
    assert evaluate_metrics(metrics(p95=(100.0, 180.0)), CFG)["verdict"] == "ok"
    slow = evaluate_metrics(metrics(p95=(200.0, 900.0)), CFG)
    assert slow["regressions"] == ["latency_p95"]
    missing = evaluate_metrics(metrics(p95=(None, None)), CFG)
    assert missing["latency"] == "unevaluated"


@pytest.mark.parametrize(
    ("row", "scenarios", "verdict"),
    [
        (rollout(ready=False), PASS, "not_ready"),
        (
            rollout(
                traffic=[
                    {"revision": "web-00041-old", "percent": 100},
                    {
                        "revision": "web-00042-abc",
                        "percent": 0,
                        "tag": "canary",
                        "uri": "https://canary---web.a.run.app",
                    },
                ]
            ),
            [],
            "not_serving",
        ),
        (
            rollout(),
            [{"id": "top-page", "outcome": "fail", "failure": "http_status"}],
            "failed",
        ),
        (rollout(metrics=metrics(after_5xx=50)), PASS, "regressed"),
        (rollout(), [], "unverified"),
        (
            rollout(
                traffic=[
                    {"revision": "web-00041-old", "percent": 90},
                    {"revision": "web-00042-abc", "percent": 10},
                ]
            ),
            PASS,
            "unverified",
        ),
        (rollout(), [{"id": "top-page", "outcome": "inconclusive"}], "unverified"),
        (rollout(superseded=True), [], "superseded"),
        (rollout(), PASS, "verified"),
    ],
)
def test_rollout_verdicts(row, scenarios, verdict):
    result = evaluate_rollout(row, scenarios, CFG)
    assert result["verdict"] == verdict
    assert result["fingerprint"] == "deploy_verification:web:web-00042-abc"
    assert result["needs_analysis"] is (
        verdict in {"failed", "regressed", "not_ready", "not_serving", "unverified"}
    )


def test_tagged_candidate_url_is_kept_separate_from_production():
    row = rollout(
        traffic=[
            {"revision": "web-00041-old", "percent": 100},
            {
                "revision": "web-00042-abc",
                "percent": 0,
                "tag": "candidate",
                "uri": "https://candidate---web.a.run.app",
            },
        ]
    )
    result = evaluate_rollout(row, [], CFG)
    assert result["traffic_percent"] == 0
    assert result["candidate_urls"] == [
        {"tag": "candidate", "uri": "https://candidate---web.a.run.app"}
    ]
    assert "candidate" in result["reasons"][0]


def test_verify_recent_deployments_runs_production_scenarios_only_for_serving_revisions(
    monkeypatch, tmp_path
):
    import json

    from app.tools import service_checks

    path = tmp_path / "checks.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "allowed_hosts": ["www.example.com"],
                "scenarios": [
                    {
                        "id": "web-top",
                        "service": "web",
                        "steps": [{"url": "https://www.example.com/"}],
                    },
                    {
                        "id": "api-top",
                        "service": "api",
                        "steps": [{"url": "https://www.example.com/api"}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(service_checks, "_suite_path", lambda: str(path))
    monkeypatch.setattr(
        service_checks, "control_check", lambda url: {"status": "not_configured"}
    )
    ran = []

    def fake_run(scenario, suite, attempts, control):
        ran.append(scenario.id)
        return {
            "id": scenario.id,
            "version": scenario.version,
            "outcome": "pass",
            "attempts_made": 1,
        }

    monkeypatch.setattr(service_checks, "run_scenario", fake_run)
    monkeypatch.setattr(
        service_checks,
        "list_recent_rollouts",
        lambda minutes: {
            "status": "ok",
            "complete": True,
            "rollouts": [
                rollout(),
                rollout(
                    service="api",
                    revision="api-00007-xyz",
                    traffic=[{"revision": "api-00006-old", "percent": 100}],
                ),
            ],
        },
    )
    result = service_checks.verify_recent_deployments(30)
    assert ran == ["web-top"]  # api's new revision serves no traffic
    verdicts = {row["service"]: row["verdict"] for row in result["rollouts"]}
    assert verdicts == {"web": "verified", "api": "not_serving"}
    assert result["window_minutes"] == 30


def test_verify_recent_deployments_never_claims_success_when_state_is_unreadable(
    monkeypatch,
):
    from app.tools import service_checks

    monkeypatch.setattr(
        service_checks,
        "list_recent_rollouts",
        lambda minutes: {"status": "permission_denied"},
    )
    result = service_checks.verify_recent_deployments(60)
    assert result["status"] == "permission_denied" and result["complete"] is False
    assert "nothing is verified" in result["message"]


# --------------------------------------------------------------------------- GCP provider


def test_gcp_rollouts_use_serving_state_and_per_revision_metrics(monkeypatch):
    now = dt.datetime(2026, 11, 25, 1, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(gcp_rollouts, "utcnow", lambda: now)
    service = {
        "name": "projects/p/locations/r/services/web",
        "uri": "https://web-abc.a.run.app",
        "updateTime": "2026-11-25T00:40:00Z",
        "latestReadyRevision": "projects/p/locations/r/services/web/revisions/web-00042-abc",
        "trafficStatuses": [
            {
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                "revision": "web-00041-old",
                "percent": 50,
            },
            {
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                "revision": "web-00042-abc",
                "percent": 50,
                "tag": "green",
                "uri": "https://green---web-abc.a.run.app",
            },
        ],
    }
    old_service = {
        **service,
        "name": "projects/p/locations/r/services/batch",
        "updateTime": "2026-11-20T00:00:00Z",
    }
    revisions = {
        "revisions": [
            {
                "name": "projects/p/locations/r/services/web/revisions/web-00042-abc",
                "createTime": "2026-11-25T00:40:00Z",
                "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}],
            },
            {
                "name": "projects/p/locations/r/services/web/revisions/web-00041-old",
                "createTime": "2026-11-20T00:00:00Z",
                "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}],
            },
        ]
    }
    queries = []

    def fake_list(url, key, params):
        if url.endswith("/services"):
            return True, [service, old_service]
        queries.append(params)
        latency = "request_latencies" in params["filter"]
        start = params["interval.startTime"]
        if latency:
            return True, [
                {
                    "resource": {"labels": {"revision_name": "web-00042-abc"}},
                    "points": [{"value": {"doubleValue": 640.0}}],
                },
                {
                    "resource": {"labels": {"revision_name": "web-00041-old"}},
                    "points": [{"value": {"doubleValue": 210.0}}],
                },
            ]
        rows = [
            {
                "resource": {"labels": {"revision_name": "web-00041-old"}},
                "metric": {"labels": {"response_code_class": "2xx"}},
                "points": [{"value": {"int64Value": "900"}}],
            },
            {
                "resource": {"labels": {"revision_name": "web-00041-old"}},
                "metric": {"labels": {"response_code_class": "5xx"}},
                "points": [{"value": {"int64Value": "1"}}],
            },
        ]
        if start == "2026-11-25T00:40:00Z":
            rows += [
                {
                    "resource": {"labels": {"revision_name": "web-00042-abc"}},
                    "metric": {"labels": {"response_code_class": "2xx"}},
                    "points": [{"value": {"int64Value": "300"}}],
                },
                {
                    "resource": {"labels": {"revision_name": "web-00042-abc"}},
                    "metric": {"labels": {"response_code_class": "5xx"}},
                    "points": [{"value": {"int64Value": "30"}}],
                },
            ]
        return True, rows

    monkeypatch.setattr(gcp_rollouts, "gcp_list", fake_list)
    monkeypatch.setattr(gcp_rollouts, "gcp_get", lambda url, params: (True, revisions))
    result = gcp_rollouts.list_recent_rollouts(60)
    assert result["status"] == "ok" and result["services_checked"] == 1
    [row] = result["rollouts"]
    assert row["revision"] == "web-00042-abc" and row["ready"] is True
    assert {t["revision"]: t["percent"] for t in row["traffic"]} == {
        "web-00041-old": 50,
        "web-00042-abc": 50,
    }
    after, before = row["metrics"]["after"], row["metrics"]["before"]
    # The new revision is measured alone after the deploy; the baseline excludes it.
    assert (
        after["revisions"] == ["web-00042-abc"]
        and after["requests"] == 330
        and after["5xx"] == 30
    )
    assert before["revisions"] == ["web-00041-old"] and before["requests"] == 901
    assert after["p95_ms"] == 640.0 and before["p95_ms"] == 210.0
    assert before["start"] == "2026-11-25T00:20:00Z"  # baseline = 20 min after-window
    assert all('service_name="web"' in query["filter"] for query in queries)
    verdict = evaluate_rollout(row, [], CFG)
    assert verdict["verdict"] == "regressed"
    assert set(verdict["metrics"]["regressions"]) == {"error_rate", "latency_p95"}


def test_gcp_rollouts_report_partial_revision_access(monkeypatch):
    now = dt.datetime(2026, 11, 25, 1, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(gcp_rollouts, "utcnow", lambda: now)
    monkeypatch.setattr(
        gcp_rollouts,
        "gcp_list",
        lambda url, key, params: (
            True,
            [{"name": "services/web", "updateTime": "2026-11-25T00:50:00Z"}],
        ),
    )
    monkeypatch.setattr(
        gcp_rollouts,
        "gcp_get",
        lambda url, params: (False, {"status": "permission_denied"}),
    )
    result = gcp_rollouts.list_recent_rollouts(30)
    assert result["status"] == "partial" and result["complete"] is False
    assert result["errors"] == [{"service": "web", "status": "permission_denied"}]
