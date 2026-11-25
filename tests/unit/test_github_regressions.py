"""Offline regressions for GitHub scope, pagination and write failures."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import httpx
import pytest

from app.core.fingerprint import build_meta_comment
from app.integrations import github
from app.tools import issues


@pytest.fixture(autouse=True)
def offline_github(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Unexpected GitHub request")

    monkeypatch.setattr(github.httpx, "request", unexpected)
    monkeypatch.setattr(
        github, "_headers", lambda accept=None: {"Accept": accept or ""}
    )
    monkeypatch.setattr(issues, "gh_rest", unexpected)
    monkeypatch.setattr(issues, "gh_graphql", unexpected)
    monkeypatch.setattr(
        issues,
        "CONFIG",
        dataclasses.replace(
            issues.CONFIG,
            provider="gcp",
            target="prod",
            github_repo="test/repo",
            timezone="UTC",
            dry_run=False,
            auto_close_issues=True,
            max_issues_per_run=10,
        ),
    )
    monkeypatch.setattr(github, "CONFIG", issues.CONFIG)
    issues._ensured_labels.clear()
    yield
    issues._ensured_labels.clear()


def _issue(number, meta=None, **extra):
    return {
        "number": number,
        "title": "test issue",
        "state": "open",
        "body": build_meta_comment(
            {"provider": "gcp", "target": "prod"} if meta is None else meta
        ),
        **extra,
    }


def _create(context):
    return issues.create_ops_issue(
        "error", "failure", "evidence", "high", "wanted", "worker", "failure", context
    )


def test_scope_filters_lists_and_deduplication(monkeypatch):
    rows = [
        _issue(1, {"fingerprint": "wanted", "provider": "aws", "target": "prod"}),
        _issue(2, {"fingerprint": "wanted", "provider": "gcp", "target": "staging"}),
        _issue(3, {"fingerprint": "wanted", "project": "prod"}),
        _issue(4, {"fingerprint": "other"}),
        _issue(5),
    ]

    def fake_rest(method, path, **kwargs):
        assert method == "GET"
        assert path == "/repos/test/repo/issues"
        return True, rows

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    listed = issues.list_ops_issues("open", "all")
    assert [i["number"] for i in listed["issues"]] == [3, 4, 5]
    assert listed["issues"][-1]["provider"] == "gcp"
    assert issues.find_existing_ops_issue("wanted")["number"] == 3


@pytest.mark.parametrize(
    "meta",
    [
        {"provider": "aws", "target": "prod"},
        {"provider": "gcp", "target": "staging"},
        {"project": "staging"},
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_issue", ()),
        ("comment_on_issue", ("evidence",)),
        ("add_issue_labels", (["ops:stale"],)),
        ("close_issue", ("verified",)),
    ],
)
def test_read_and_updates_reject_other_scope(monkeypatch, meta, dry_run, tool, args):
    monkeypatch.setattr(
        issues, "CONFIG", dataclasses.replace(issues.CONFIG, dry_run=dry_run)
    )
    calls = []

    def fake_rest(method, path, **kwargs):
        calls.append((method, path))
        assert method == "GET"
        assert path == "/repos/test/repo/issues/7"
        return True, _issue(7, meta)

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    response = getattr(issues, tool)(7, *args)
    assert response["status"] == "scope_mismatch"
    assert "body" not in response
    assert calls == [("GET", "/repos/test/repo/issues/7")]


@pytest.mark.parametrize("meta", [{"project": "prod"}, {"target": "prod"}, {}])
def test_legacy_issue_metadata_remains_readable(monkeypatch, meta):
    monkeypatch.setattr(issues, "gh_rest", lambda *a, **kw: (True, _issue(7, meta)))
    assert issues.get_issue(7)["status"] == "ok"


def test_linked_prs_reject_an_issue_from_another_scope(monkeypatch):
    def fake_graphql(query, variables):
        assert "closedAt body" in query
        return True, {
            "repository": {"issue": _issue(7, {"provider": "aws", "target": "prod"})}
        }

    monkeypatch.setattr(issues, "gh_graphql", fake_graphql)
    response = issues.get_issue_linked_prs(7)
    assert response["status"] == "scope_mismatch"
    assert "linked_prs" not in response


def test_pagination_includes_issue_101_and_does_not_stop_at_a_pr(monkeypatch):
    first_page = [_issue(n, {"fingerprint": str(n)}) for n in range(1, 101)]
    first_page[49]["pull_request"] = {}
    second_page = [_issue(101, {"fingerprint": "wanted", "target": "prod"})]
    calls = []

    def fake_rest(method, path, *, params):
        assert method == "GET"
        assert path == "/repos/test/repo/issues"
        calls.append(params["page"])
        return True, first_page if params["page"] == 1 else second_page

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    assert issues.find_existing_ops_issue("wanted")["number"] == 101
    assert calls == [1, 2]
    calls.clear()
    response = issues.list_ops_issues("open", "all")
    assert response["count"] == 100
    assert response["issues"][-1]["number"] == 101
    assert calls == [1, 2]


@pytest.mark.parametrize("failed_state", ["open", "closed"])
@pytest.mark.parametrize(
    ("status", "http_status"), [("permission_denied", 403), ("not_found", 404)]
)
def test_page_failure_blocks_creation_instead_of_using_partial_results(
    monkeypatch, failed_state, status, http_status
):
    def fake_rest(method, path, *, params):
        assert method == "GET", "Incomplete reads must never create an issue"
        if params["state"] != failed_state:
            return True, []
        if params["page"] == 2:
            return False, {"status": status, "http_status": http_status}
        return True, [_issue(n, {"fingerprint": str(n)}) for n in range(1, 101)]

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    context = SimpleNamespace(state={})
    response = _create(context)
    assert response["status"] == status
    assert response["error"]["page"] == 2
    assert response["error"]["partial_count"] == 100
    assert context.state == {}


@pytest.mark.parametrize("payload", [{"unexpected": True}, [1]])
def test_invalid_issue_list_blocks_creation(monkeypatch, payload):
    def fake_rest(method, path, **kwargs):
        assert method == "GET"
        return True, payload

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    assert _create(SimpleNamespace(state={}))["status"] == "error"


@pytest.mark.parametrize("failed_step", ["comment", "label"])
@pytest.mark.parametrize("auto_close", [False, True])
def test_verification_write_failure_stops_later_steps(
    monkeypatch, failed_step, auto_close
):
    monkeypatch.setattr(
        issues,
        "CONFIG",
        dataclasses.replace(issues.CONFIG, auto_close_issues=auto_close),
    )
    monkeypatch.setattr(issues, "_ensure_labels", lambda labels: (True, []))
    writes = []

    def fake_rest(method, path, **kwargs):
        if method == "GET":
            return True, _issue(7)
        writes.append((method, path))
        suffix = "comments" if failed_step == "comment" else "labels"
        if path.endswith(suffix):
            return False, {"status": "error", "http_status": 503}
        return True, {}

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    response = issues.close_issue(7, "verified")
    assert response["status"] == "error"
    assert response["failed_step"] == failed_step
    assert response["error"]["http_status"] == 503
    assert all(method != "PATCH" for method, _ in writes)
    if failed_step == "comment":
        assert writes == [("POST", "/repos/test/repo/issues/7/comments")]
    else:
        assert response["completed_steps"] == ["comment"]


def test_failed_label_preparation_is_not_cached_or_followed_by_issue_writes(
    monkeypatch,
):
    calls = []

    def fake_rest(method, path, **kwargs):
        calls.append((method, path))
        assert method == "GET"
        if path.endswith("/issues/7"):
            return True, _issue(7)
        return False, {"status": "permission_denied", "http_status": 403}

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    for _ in range(2):
        response = issues.close_issue(7, "verified")
        assert response["status"] == "permission_denied"
        assert response["failed_step"] == "ensure_labels"
    assert issues._ensured_labels == set()
    assert len(calls) == 4


@pytest.mark.parametrize("auto_close", [False, True])
def test_verified_issue_only_closes_after_both_writes_succeed(monkeypatch, auto_close):
    monkeypatch.setattr(
        issues,
        "CONFIG",
        dataclasses.replace(issues.CONFIG, auto_close_issues=auto_close),
    )
    monkeypatch.setattr(issues, "_ensure_labels", lambda labels: (True, []))
    writes = []

    def fake_rest(method, path, **kwargs):
        if method == "GET":
            return True, _issue(7, {"project": "prod"})
        writes.append((method, path))
        return True, {}

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    response = issues.close_issue(7, "verified")
    assert response["status"] == ("closed" if auto_close else "verified_not_closed")
    expected = [
        ("POST", "/repos/test/repo/issues/7/comments"),
        ("POST", "/repos/test/repo/issues/7/labels"),
    ]
    if auto_close:
        expected.append(("PATCH", "/repos/test/repo/issues/7"))
    assert writes == expected


@pytest.mark.parametrize(
    "body",
    [
        '{"engines":{"node":">=22"},"padding":"' + "x" * 2200 + '"}',
        "22\n",
        "3.12\n",
        "FROM python:3.12\n",
        "",
    ],
)
def test_raw_accept_preserves_complete_text_including_valid_json(monkeypatch, body):
    monkeypatch.setattr(
        github.httpx, "request", lambda *a, **kw: httpx.Response(200, text=body)
    )
    assert github.gh_rest(
        "GET",
        "/repos/test/repo/contents/file",
        accept="application/vnd.github.raw+json",
    ) == (True, {"raw": body})


def _report(number, report_kind, title, **meta):
    return _issue(
        number,
        {
            "kind": "report",
            "report_kind": report_kind,
            "provider": "gcp",
            "target": "prod",
            **meta,
        },
        title=title,
        html_url=f"https://github.com/test/repo/issues/{number}",
    )


def test_publish_report_closes_only_the_same_series(monkeypatch):
    monkeypatch.setattr(
        issues,
        "CONFIG",
        dataclasses.replace(
            issues.CONFIG,
            dry_run=False,
            auto_close_issues=False,
            report_language="ja",
        ),
    )
    monkeypatch.setattr(issues, "_ensure_labels", lambda labels: (True, []))
    rows = [
        _report(3, "access", "[ops/report] 不審アクセス調査 2026-11-17"),
        _report(4, "security", "[ops/report] 不審アクセス日次調査 2026-11-18"),
        _report(5, "security", "[ops/report] 月次セキュリティ診断 2026-11"),
        _report(6, "infrastructure", "[ops/report] 週次インフラ見直し 2026-11-17"),
        _report(7, "security", "[ops/report] 週次インフラ見直し 2026-11-10"),
        _report(8, "weekly", "[ops/report] 週次運用レビュー"),
        _issue(
            9,
            {
                "kind": "error",
                "fingerprint": "abc",
                "provider": "gcp",
                "target": "prod",
            },
            title="[ops/error] boom",
        ),
        _report(
            10,
            "access",
            "[ops/report] 不審アクセス調査 other",
            target="staging",
        ),
    ]
    calls = []

    def fake_rest(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json"), kwargs.get("params")))
        if method == "GET" and path.endswith("/issues"):
            assert kwargs["params"]["state"] == "open"
            assert "ops:report" in kwargs["params"]["labels"]
            return True, rows
        if method == "GET":
            number = int(path.rsplit("/", 1)[-1])
            return True, next(row for row in rows if row["number"] == number)
        if method == "POST" and path.endswith("/issues"):
            return True, {
                "number": 20,
                "html_url": "https://github.com/test/repo/issues/20",
            }
        if method == "POST" and path.endswith("/comments"):
            return True, {}
        if method == "PATCH":
            return True, {}
        raise AssertionError((method, path))

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    context = SimpleNamespace(state={})
    access = issues.publish_report(
        "access", "不審アクセス調査 2026-11-24", "本文", context
    )
    assert access["status"] == "created" and access["close_status"] == "ok"
    assert {row["number"] for row in access["superseded"]} == {3, 4}
    created = next(
        item for item in calls if item[0] == "POST" and item[1].endswith("/issues")
    )
    assert created[2]["labels"] == ["ops-agent", "ops:report", "ops:security"]
    assert '"report_kind": "access"' in created[2]["body"]
    comments = [
        item
        for item in calls
        if item[0] == "POST" and str(item[1]).endswith("/comments")
    ]
    patches = [item for item in calls if item[0] == "PATCH"]
    assert [item[1] for item in patches] == [
        "/repos/test/repo/issues/3",
        "/repos/test/repo/issues/4",
    ]
    assert all(
        item[2] == {"state": "closed", "state_reason": "not_planned"}
        for item in patches
    )
    assert len(comments) == 2
    for comment in comments:
        assert "対応不要としてクローズ" in comment[2]["body"]
        assert "#20" in comment[2]["body"]
        assert "https://github.com/test/repo/issues/20" in comment[2]["body"]
    assert context.state == {}

    calls.clear()
    monthly = issues.publish_report(
        "security", "月次セキュリティ診断 2026-11", "本文", context
    )
    assert {row["number"] for row in monthly["superseded"]} == {5}
    calls.clear()
    infra = issues.publish_report(
        "infrastructure", "週次インフラ見直し 2026-11-24", "本文", context
    )
    assert {row["number"] for row in infra["superseded"]} == {6, 7}


def test_publish_report_dry_run_previews_series_closure_without_writes(monkeypatch):
    monkeypatch.setattr(
        issues,
        "CONFIG",
        dataclasses.replace(issues.CONFIG, dry_run=True, report_language="ja"),
    )
    rows = [_report(3, "weekly", "[ops/report] 週次運用レビュー")]

    def fake_rest(method, path, **kwargs):
        assert method == "GET"
        assert path.endswith("/issues")
        return True, rows

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    out = issues.publish_report("weekly", "週次", "本文", SimpleNamespace(state={}))
    assert out["status"] == "dry_run"
    assert out["action"] == "create_report"
    assert out["would_supersede"] == [3]


def test_publish_report_does_not_create_when_series_listing_fails(monkeypatch):
    def fake_rest(method, path, **kwargs):
        assert method == "GET"
        return False, {"status": "permission_denied", "http_status": 403, "page": 1}

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    out = issues.publish_report("weekly", "週次", "本文", SimpleNamespace(state={}))
    assert out["status"] == "permission_denied"
    assert out["error"]["page"] == 1


@pytest.mark.parametrize("failed_step", ["comment", "close"])
def test_superseded_report_is_not_closed_before_its_comment(monkeypatch, failed_step):
    monkeypatch.setattr(issues, "_ensure_labels", lambda labels: (True, []))
    rows = [_report(3, "weekly", "[ops/report] 週次運用レビュー")]
    writes = []

    def fake_rest(method, path, **kwargs):
        if method == "GET" and path.endswith("/issues"):
            return True, rows
        if method == "GET":
            return True, rows[0]
        if method == "POST" and path.endswith("/issues"):
            return True, {
                "number": 20,
                "html_url": "https://github.com/test/repo/issues/20",
            }
        writes.append((method, path))
        if failed_step == "comment" and path.endswith("/comments"):
            return False, {"status": "error", "http_status": 503}
        if failed_step == "close" and method == "PATCH":
            return False, {"status": "error", "http_status": 503}
        return True, {}

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    out = issues.publish_report("weekly", "週次", "本文", SimpleNamespace(state={}))
    assert out["status"] == "created"
    assert out["number"] == 20
    assert out["close_status"] == "incomplete"
    assert out["superseded"] == [
        {
            "number": 3,
            "status": "error",
            "failed_step": failed_step,
            "error": {"status": "error", "http_status": 503},
        }
    ]
    if failed_step == "comment":
        assert writes == [("POST", "/repos/test/repo/issues/3/comments")]
    else:
        assert writes == [
            ("POST", "/repos/test/repo/issues/3/comments"),
            ("PATCH", "/repos/test/repo/issues/3"),
        ]


def test_open_finding_gets_a_recurrence_comment_and_stays_open(monkeypatch):
    monkeypatch.setattr(
        issues,
        "CONFIG",
        dataclasses.replace(issues.CONFIG, dry_run=False, report_language="ja"),
    )
    existing = _issue(
        11,
        {
            "fingerprint": "wanted",
            "kind": "error",
            "provider": "gcp",
            "target": "prod",
        },
        updated_at="2020-01-01T00:00:00Z",
        html_url="https://github.com/test/repo/issues/11",
    )
    methods = []

    def fake_rest(method, path, **kwargs):
        methods.append((method, path))
        if method == "GET" and path.endswith("/issues"):
            return True, [existing] if kwargs["params"]["state"] == "open" else []
        if method == "POST" and path.endswith("/comments"):
            assert "再発を検知" in kwargs["json"]["body"]
            return True, {}
        raise AssertionError("fingerprint de-dupe must not create or close an issue")

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    out = _create(SimpleNamespace(state={}))
    assert out["status"] == "updated_existing"
    assert out["number"] == 11
    assert [method for method, _ in methods] == ["GET", "POST"]
    assert all(method != "PATCH" for method, _ in methods)


def test_supersede_comment_follows_the_report_language(monkeypatch):
    monkeypatch.setattr(
        issues,
        "CONFIG",
        dataclasses.replace(issues.CONFIG, dry_run=False, report_language="en"),
    )
    monkeypatch.setattr(issues, "_ensure_labels", lambda labels: (True, []))
    rows = [_report(3, "weekly", "[ops/report] weekly")]

    def fake_rest(method, path, **kwargs):
        if method == "GET" and path.endswith("/issues"):
            return True, rows
        if method == "GET":
            return True, rows[0]
        if method == "POST" and path.endswith("/issues"):
            return True, {
                "number": 21,
                "html_url": "https://github.com/test/repo/issues/21",
            }
        if method == "POST" and path.endswith("/comments"):
            assert "not planned" in kwargs["json"]["body"]
            assert "https://github.com/test/repo/issues/21" in kwargs["json"]["body"]
            return True, {}
        if method == "PATCH":
            assert kwargs["json"]["state_reason"] == "not_planned"
            return True, {}
        raise AssertionError((method, path))

    monkeypatch.setattr(issues, "gh_rest", fake_rest)
    out = issues.publish_report("weekly", "weekly", "body", SimpleNamespace(state={}))
    assert out["close_status"] == "ok"


def test_raw_accept_still_returns_http_errors_and_normal_json_is_parsed(monkeypatch):
    monkeypatch.setattr(
        github.httpx,
        "request",
        lambda *a, **kw: httpx.Response(403, json={"message": "forbidden"}),
    )
    ok, payload = github.gh_rest(
        "GET", "/file", accept="application/vnd.github.raw+json"
    )
    assert not ok
    assert payload["status"] == "permission_denied"
    monkeypatch.setattr(
        github.httpx,
        "request",
        lambda *a, **kw: httpx.Response(200, json={"number": 7}),
    )
    assert github.gh_rest("GET", "/file") == (True, {"number": 7})
