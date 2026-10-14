"""Repository and public-feed failures must stay unknown, not become findings."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import httpx
import pytest

from app.tools import repo, threat_intel

BASE = "/repos/example/repo"


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Unexpected external request")

    monkeypatch.setattr(httpx, "request", fail)
    monkeypatch.setattr(httpx, "get", fail)
    monkeypatch.setattr(threat_intel, "_kev_cache", None)


def mock_repo(monkeypatch, overrides=None, *, admin=False):
    responses = {
        BASE: (
            True,
            {
                "default_branch": "main",
                "permissions": {"admin": admin},
                "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
            },
        ),
        f"{BASE}/vulnerability-alerts": (True, {}),
        f"{BASE}/branches/main/protection": (
            True,
            {"required_pull_request_reviews": {}},
        ),
        f"{BASE}/rulesets": (True, []),
        f"{BASE}/actions/workflows": (True, {"workflows": []}),
        f"{BASE}/git/trees/main": (True, {"tree": []}),
        "/search/issues": (True, {"total_count": 0}),
    }
    responses.update(overrides or {})
    monkeypatch.setattr(repo, "repo_path", lambda: BASE)
    monkeypatch.setattr(repo, "CONFIG", SimpleNamespace(github_repo="example/repo"))

    def request(method, path, **kwargs):
        assert method == "GET"
        return responses[path]

    monkeypatch.setattr(repo, "gh_rest", request)


@pytest.mark.parametrize("http_status", [403, 429, 503])
def test_failed_security_probes_do_not_report_disabled_settings(
    monkeypatch, http_status
):
    error = {"status": "error", "http_status": http_status, "message": "Unavailable"}
    mock_repo(
        monkeypatch,
        {
            f"{BASE}/vulnerability-alerts": (False, error),
            f"{BASE}/branches/main/protection": (False, error),
            f"{BASE}/rulesets": (False, error),
        },
    )

    out = repo.get_repo_security_posture()

    assert out["status"] == "partial"
    assert out["findings"] == []
    assert out["summary"]["dependabot_alerts_enabled"] is None
    assert out["summary"]["branch_protection"] is None
    assert out["summary"]["rulesets"] is None
    assert {e["check"] for e in out["errors"]} == {
        "dependabot_alerts",
        "branch_protection",
        "rulesets",
    }
    assert all(e["http_status"] == http_status for e in out["errors"])


def test_permission_masked_404_is_unknown(monkeypatch):
    error = {"status": "not_found", "http_status": 404, "message": "Not Found"}
    mock_repo(
        monkeypatch,
        {
            f"{BASE}/vulnerability-alerts": (False, error),
            f"{BASE}/branches/main/protection": (False, error),
        },
    )

    out = repo.get_repo_security_posture()

    assert out["status"] == "partial"
    assert out["findings"] == []
    assert out["summary"]["dependabot_alerts_enabled"] is None
    assert out["summary"]["branch_protection"] is None


def test_admin_rights_do_not_prove_alert_endpoint_permissions(
    monkeypatch,
):
    mock_repo(
        monkeypatch,
        {
            f"{BASE}/vulnerability-alerts": (
                False,
                {
                    "status": "not_found",
                    "http_status": 404,
                    "message": "Not Found",
                },
            ),
            f"{BASE}/branches/main/protection": (
                False,
                {
                    "status": "not_found",
                    "http_status": 404,
                    "message": "Branch not protected",
                },
            ),
        },
        admin=True,
    )

    out = repo.get_repo_security_posture()

    assert out["status"] == "partial"
    assert {f["check_id"] for f in out["findings"]} == {
        "repo.branch_protection",
    }
    assert out["summary"]["dependabot_alerts_enabled"] is None
    assert out["summary"]["branch_protection"] is False
    assert out["errors"][0]["check"] == "dependabot_alerts"
    assert out["errors"][0]["http_status"] == 404


def test_confirmed_absent_protection_still_produces_finding(monkeypatch):
    mock_repo(
        monkeypatch,
        {
            f"{BASE}/branches/main/protection": (
                False,
                {
                    "status": "not_found",
                    "http_status": 404,
                    "message": "Branch not protected",
                },
            ),
        },
    )

    out = repo.get_repo_security_posture()

    assert out["status"] == "ok"
    assert {f["check_id"] for f in out["findings"]} == {"repo.branch_protection"}
    assert out["summary"]["dependabot_alerts_enabled"] is True


def test_missing_classic_protection_is_not_a_finding_when_rulesets_are_unknown(
    monkeypatch,
):
    mock_repo(
        monkeypatch,
        {
            f"{BASE}/branches/main/protection": (
                False,
                {
                    "status": "not_found",
                    "http_status": 404,
                    "message": "Branch not protected",
                },
            ),
            f"{BASE}/rulesets": (
                False,
                {"status": "permission_denied", "http_status": 403},
            ),
        },
    )

    out = repo.get_repo_security_posture()

    assert out["status"] == "partial"
    assert out["findings"] == []
    assert out["summary"]["branch_protection"] is False
    assert out["errors"][0]["check"] == "rulesets"


@pytest.mark.parametrize("payload", [None, [], "invalid"])
def test_malformed_repository_metadata_returns_error(monkeypatch, payload):
    mock_repo(monkeypatch, {BASE: (True, payload)})

    assert repo.get_repo_security_posture()["status"] == "error"


@pytest.mark.parametrize(
    ("path", "payload", "check"),
    [
        (f"{BASE}/rulesets", {}, "rulesets"),
        (f"{BASE}/rulesets", [None], "rulesets"),
        (f"{BASE}/actions/workflows", {"workflows": [None]}, "security_workflows"),
        (
            f"{BASE}/git/trees/main",
            {"tree": [{"type": "blob", "path": 1}]},
            "repository_tree",
        ),
        ("/search/issues", [], "dependabot_prs"),
    ],
)
def test_malformed_secondary_probe_is_partial(monkeypatch, path, payload, check):
    mock_repo(monkeypatch, {path: (True, payload)})

    out = repo.get_repo_security_posture()

    assert out["status"] == "partial"
    assert out["findings"] == []
    assert check in {error["check"] for error in out["errors"]}


def test_raw_json_version_pin_is_preserved(monkeypatch):
    mock_repo(
        monkeypatch,
        {
            f"{BASE}/git/trees/main": (
                True,
                {"tree": [{"type": "blob", "path": "package.json"}]},
            ),
            f"{BASE}/contents/package.json": (
                True,
                {"raw": '{"engines":{"node":">=22"}}'},
            ),
        },
    )

    out = repo.get_repo_security_posture()

    assert out["status"] == "ok"
    assert out["summary"]["version_pins"]["package.json"] == '"engines":{"node":">=22"}'


def test_failed_workflow_run_is_reported_as_partial(monkeypatch):
    mock_repo(
        monkeypatch,
        {
            f"{BASE}/actions/workflows": (
                True,
                {"workflows": [{"id": 7, "name": "Security scan"}]},
            ),
            f"{BASE}/actions/workflows/7/runs": (
                False,
                {"status": "permission_denied", "http_status": 403},
            ),
        },
    )

    out = repo.get_repo_security_posture()

    assert out["status"] == "partial"
    assert out["summary"]["security_workflows"][0]["latest_conclusion"] is None
    assert out["errors"][0]["check"] == "workflow_runs:7"


def test_malformed_raw_content_is_reported_as_partial(monkeypatch):
    mock_repo(
        monkeypatch,
        {
            f"{BASE}/git/trees/main": (
                True,
                {"tree": [{"type": "blob", "path": "Dockerfile"}]},
            ),
            f"{BASE}/contents/Dockerfile": (True, {"raw": []}),
        },
    )

    out = repo.get_repo_security_posture()

    assert out["status"] == "partial"
    assert out["summary"]["dockerfiles"] == []
    assert out["errors"][0]["check"] == "content:Dockerfile"


@pytest.mark.parametrize("tool", ["kev", "eol"])
def test_invalid_json_feed_returns_error(monkeypatch, tool):
    response = httpx.Response(
        200,
        content=b"not JSON",
        request=httpx.Request("GET", "https://example.test/feed"),
    )
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: response)

    out = (
        threat_intel.fetch_cisa_kev(30)
        if tool == "kev"
        else threat_intel.check_eol_status("python", "3.11")
    )

    assert out["status"] == "error"
    assert threat_intel._kev_cache is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"vulnerabilities": {}},
        {"vulnerabilities": [None]},
        {"vulnerabilities": [{"dateAdded": 20261014}]},
    ],
)
def test_malformed_kev_feed_returns_error_without_caching(monkeypatch, payload):
    response = httpx.Response(
        200, json=payload, request=httpx.Request("GET", "https://example.test/feed")
    )
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: response)

    assert threat_intel.fetch_cisa_kev(30)["status"] == "error"
    assert threat_intel._kev_cache is None


@pytest.mark.parametrize(
    "payload", [None, {}, "invalid", [None], [{}], [{"cycle": []}], [{"cycle": True}]]
)
def test_malformed_eol_feed_returns_error(monkeypatch, payload):
    response = httpx.Response(
        200, json=payload, request=httpx.Request("GET", "https://example.test/feed")
    )
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: response)

    assert threat_intel.check_eol_status("python", "3.11")["status"] == "error"


def test_valid_eol_feed_keeps_existing_result(monkeypatch):
    response = httpx.Response(
        200,
        json=[
            {"cycle": "3.13", "eol": False},
            {"cycle": "3.11", "eol": "2099-01-01", "latest": "3.11.14"},
        ],
        request=httpx.Request("GET", "https://example.test/feed"),
    )
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: response)

    out = threat_intel.check_eol_status("python", "3.11.1")

    assert out["status"] == "ok"
    assert out["cycle"] == "3.11"
    assert out["is_eol"] is False
    assert out["newer_cycles"] == ["3.13"]


def test_valid_kev_feed_keeps_filtering_and_cache(monkeypatch):
    response = httpx.Response(
        200,
        json={
            "catalogVersion": "2026.10.14",
            "vulnerabilities": [
                {"cveID": "CVE-2026-1", "dateAdded": "2026-10-13", "product": "Python"},
                {"cveID": "CVE-2025-1", "dateAdded": "2025-10-13", "product": "Python"},
            ],
        },
        request=httpx.Request("GET", "https://example.test/feed"),
    )
    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        return response

    monkeypatch.setattr(httpx, "get", request)
    monkeypatch.setattr(
        threat_intel, "utcnow", lambda: dt.datetime(2026, 10, 14, tzinfo=dt.UTC)
    )
    monkeypatch.setattr(threat_intel, "effective_stack_keywords", lambda: ("python",))

    out = threat_intel.fetch_cisa_kev(30)

    assert out["status"] == "ok"
    assert out["recent_count"] == 1
    assert out["stack_matches"][0]["cve"] == "CVE-2026-1"
    assert threat_intel.fetch_cisa_kev(30) == out
    assert len(calls) == 1
