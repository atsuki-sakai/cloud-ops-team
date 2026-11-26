"""Service checks: scenario contract, deterministic judgement and accuracy rules (offline)."""

from __future__ import annotations

import copy
import datetime as dt
import json

import httpx
import pytest

from app.core import synthetic
from app.core.service_checks import ScenarioError, parse_suite
from app.core.synthetic import run_scenario, run_suite


def suite_data(**overrides):
    data = {
        "version": 1,
        "allowed_hosts": ["www.example.com", "*.example.net"],
        "defaults": {"attempts": 3, "attempt_interval_seconds": 0},
        "scenarios": [
            {
                "id": "top-page",
                "service": "web",
                "critical": True,
                "steps": [
                    {
                        "url": "https://www.example.com/",
                        "expect": {
                            "status": [200],
                            "text_contains": ["予約する"],
                            "text_absent": ["エラーが発生しました"],
                            "max_latency_ms": 5000,
                        },
                    },
                    {
                        "url": "https://api.example.net/health",
                        "expect": {"json": [{"path": "status", "equals": "ok"}]},
                    },
                ],
            }
        ],
    }
    data.update(overrides)
    return data


def http_factory(handler):
    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def healthy(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.example.net":
        return httpx.Response(200, json={"status": "ok"})
    return httpx.Response(200, text="<html>予約する</html>")


# --------------------------------------------------------------------------- contract


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda d: d["scenarios"][0]["steps"][0].update(
                url="http://www.example.com/"
            ),
            "https",
        ),
        (
            lambda d: d["scenarios"][0]["steps"][0].update(url="https://evil.test/"),
            "allowed_hosts",
        ),
        (lambda d: d["scenarios"][0]["steps"][0].update(method="POST"), "read-only"),
        (lambda d: d["scenarios"][0].update(side_effects="booking"), "not enabled"),
        (
            lambda d: d["scenarios"][0]["steps"][0].update(
                headers={"Authorization": "x"}
            ),
            "headers allows only",
        ),
        (lambda d: d["scenarios"][0].update(extra=True), "unsupported fields"),
        (lambda d: d["scenarios"].append(copy.deepcopy(d["scenarios"][0])), "unique"),
        (lambda d: d["scenarios"][0].update(attempts=9), "attempts"),
        (lambda d: d.update(allowed_hosts=["example.com/path"]), "host name"),
        (
            lambda d: d["scenarios"][0]["steps"][0].update(
                url="https://user:pw@www.example.com/"
            ),
            "credential",
        ),
    ],
)
def test_scenario_file_rejects_unsafe_or_ambiguous_definitions(mutate, message):
    data = suite_data()
    mutate(data)
    with pytest.raises(ScenarioError, match=message):
        parse_suite(data)


def browser_suite(steps, **scenario):
    return suite_data(
        scenarios=[
            {
                "id": "search",
                "service": "web",
                "runner": "browser",
                "attempts": 1,
                "steps": steps,
                **scenario,
            }
        ]
    )


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([{"action": "click", "selector": "#go"}], "goto"),
        (
            [
                {"action": "goto", "url": "https://www.example.com/"},
                {"action": "click", "x": 10, "y": 10},
            ],
            "expect_target",
        ),
        (
            [
                {"action": "goto", "url": "https://www.example.com/"},
                {"action": "click", "x": 5000, "y": 10, "expect_target": "#go"},
            ],
            "x must be",
        ),
        (
            [
                {"action": "goto", "url": "https://www.example.com/"},
                {"action": "fill", "selector": "#password", "value": "abc"},
            ],
            "credential",
        ),
        (
            [
                {"action": "goto", "url": "https://www.example.com/"},
                {"action": "press", "key": "Control+A"},
            ],
            "key must be",
        ),
        (
            [
                {"action": "goto", "url": "https://www.example.com/"},
                {"action": "expect"},
            ],
            "at least one",
        ),
    ],
)
def test_browser_scenarios_require_targets_for_coordinates_and_no_credentials(
    steps, message
):
    with pytest.raises(ScenarioError, match=message):
        parse_suite(browser_suite(steps))


def test_scenario_version_is_stable_and_changes_with_any_content():
    first = parse_suite(suite_data()).scenarios[0]
    again = parse_suite(suite_data()).scenarios[0]
    changed = suite_data()
    changed["scenarios"][0]["steps"][0]["expect"]["max_latency_ms"] = 4000
    assert first.version == again.version and len(first.version) == 16
    assert parse_suite(changed).scenarios[0].version != first.version
    assert first.fingerprint == "service_check:top-page"


def test_browser_conditions_are_recorded_with_defaults():
    suite = parse_suite(
        browser_suite(
            [{"action": "goto", "url": "https://www.example.com/"}],
            browser={"viewport": {"width": 390, "height": 844}, "is_mobile": True},
        )
    )
    conditions = suite.scenarios[0].conditions()
    assert conditions["viewport"] == {"width": 390, "height": 844}
    assert conditions["is_mobile"] is True
    assert conditions["locale"] == "ja-JP" and conditions["timezone"] == "Asia/Tokyo"


# --------------------------------------------------------------------------- http judgement


def run(suite_json, handler, **kwargs):
    suite = parse_suite(suite_json)
    return run_scenario(
        suite.scenarios[0],
        suite,
        kwargs.pop("attempts", 3),
        kwargs.pop("control", {"status": "not_configured"}),
        sleep=lambda _: None,
        http_client=http_factory(handler),
        **kwargs,
    )


def test_pass_requires_every_expected_result_not_just_http_200():
    assert run(suite_data(), healthy)["outcome"] == "pass"

    def error_page(request):
        if request.url.host == "api.example.net":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, text="予約する エラーが発生しました")

    result = run(suite_data(), error_page)
    assert result["outcome"] == "fail"
    assert result["failure"] == "content_mismatch"
    assert result["attempts_made"] == 3
    assert result["severity"] == "high"  # critical scenario


@pytest.mark.parametrize(
    ("handler", "failure"),
    [
        (lambda r: httpx.Response(503, text="予約する"), "http_status"),
        (
            lambda r: (
                httpx.Response(200, json={"status": "degraded"})
                if r.url.host == "api.example.net"
                else httpx.Response(200, text="予約する")
            ),
            "json_mismatch",
        ),
        (
            lambda r: (
                httpx.Response(302, headers={"location": "https://login.evil.test/"})
                if r.url.host == "www.example.com"
                else httpx.Response(200, text="x")
            ),
            "outside_allowlist",
        ),
    ],
)
def test_failures_are_classified(handler, failure):
    result = run(suite_data(), handler)
    assert result["outcome"] == "fail"
    assert result["failure"] == failure
    assert result["failed_step"]["index"] in (0, 1)


def test_flaky_keeps_the_failure_before_the_successful_retry():
    calls = {"n": 0}

    def once_broken(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return healthy(request)

    result = run(suite_data(), once_broken)
    assert result["outcome"] == "flaky"
    assert result["severity"] == "low"
    assert [attempt["passed"] for attempt in result["attempts"]] == [False, True]
    assert result["attempts"][0]["failure"] == "http_status"
    assert result["attempts"][0]["failed_step"]["observed"]["status_code"] == 500


def test_network_failure_is_inconclusive_only_when_the_control_also_fails():
    def unreachable(request):
        raise httpx.ConnectError(
            "[Errno 8] nodename nor servname provided", request=request
        )

    down = run(suite_data(), unreachable, control={"status": "failed"})
    assert down["outcome"] == "inconclusive" and down["failure"] == "dns_error"
    up = run(suite_data(), unreachable, control={"status": "ok"})
    assert up["outcome"] == "fail"


def test_tls_and_timeouts_are_distinguished():
    def bad_tls(request):
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] expired", request=request
        )

    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    assert run(suite_data(), bad_tls)["failure"] == "tls_error"
    assert run(suite_data(), slow)["failure"] == "timeout"


def test_maintenance_window_skips_explicitly():
    now = dt.datetime(2026, 12, 1, 1, 0, tzinfo=dt.UTC)
    data = suite_data(
        maintenance=[
            {
                "start": "2026-12-01T00:00:00Z",
                "end": "2026-12-01T02:00:00Z",
                "scenarios": ["top-page"],
                "reason": "DB upgrade",
            }
        ]
    )

    def never(request):
        raise AssertionError("must not be called during maintenance")

    result = run(data, never, now=now)
    assert result["outcome"] == "maintenance"
    assert result["maintenance"]["reason"] == "DB upgrade"


def test_run_budget_marks_remaining_scenarios_not_run(monkeypatch):
    data = suite_data()
    second = copy.deepcopy(data["scenarios"][0])
    second["id"] = "second"
    data["scenarios"].append(second)
    suite = parse_suite(data)
    clock = iter([0.0, 0.0, 0.0] + [10_000.0] * 100)
    monkeypatch.setattr(synthetic.time, "monotonic", lambda: next(clock))
    result = run_suite(
        suite, 1, sleep=lambda _: None, http_client=http_factory(healthy)
    )
    outcomes = [row["outcome"] for row in result["scenarios"]]
    assert "not_run" in outcomes
    assert result["summary"]["not_run"] >= 1


# --------------------------------------------------------------------------- browser judgement


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def filter(self, visible):
        assert visible is True
        return self

    def click(self, timeout):
        self.page.actions.append(("click", self.selector))

    def fill(self, value, timeout):
        self.page.actions.append(("fill", self.selector, value))

    def get_attribute(self, name, timeout):
        return self.page.types.get(self.selector)

    def wait_for(self, state, timeout):
        if self.selector not in self.page.visible:
            raise TimeoutError("Timeout 1000ms exceeded")

    def count(self):
        return 1 if self.selector in self.page.visible else 0

    def inner_text(self, timeout):
        return self.page.body


class FakeMouse:
    def __init__(self, page):
        self.page = page

    def click(self, x, y):
        self.page.actions.append(("mouse", x, y))

    def wheel(self, dx, dy):
        self.page.actions.append(("wheel", dx, dy))


class FakePage:
    def __init__(
        self, *, probe="ok", visible=(), types=None, url="https://www.example.com/"
    ):
        self.url = url
        self.probe = probe
        self.visible = set(visible)
        self.types = types or {}
        self.actions = []
        self.body = "検索結果 3 件"
        self.mouse = FakeMouse(self)
        self.handlers = {}

    def goto(self, url, wait_until, timeout):
        self.actions.append(("goto", url))
        self.url = url
        return type("Response", (), {"status": 200})()

    def evaluate(self, script, args):
        return self.probe

    def wait_for_url(self, predicate, timeout):
        if not predicate(self.url):
            raise TimeoutError("Timeout waiting for URL")

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_text(self, text):
        return FakeLocator(self, f"text={text}")

    def title(self):
        return "検索"

    def on(self, event, handler):
        self.handlers[event] = handler


def browser_scenario(steps):
    suite = parse_suite(browser_suite(steps))
    return suite.scenarios[0], suite


def test_coordinate_click_stops_when_the_point_is_not_the_recorded_target():
    scenario, suite = browser_scenario(
        [
            {"action": "goto", "url": "https://www.example.com/"},
            {"action": "click", "x": 100, "y": 200, "expect_target": "#search"},
        ]
    )
    page = FakePage(probe="mismatch")
    with pytest.raises(synthetic.StepFailure) as info:
        synthetic.run_browser_steps(page, scenario, suite)
    assert info.value.failure == "target_mismatch"
    assert ("mouse", 100, 200) not in page.actions  # never clicks a covering dialog

    page = FakePage(probe="ok", visible={"text=検索結果"})
    scenario, suite = browser_scenario(
        [
            {"action": "goto", "url": "https://www.example.com/"},
            {"action": "click", "x": 100, "y": 200, "expect_target": "#search"},
            {
                "action": "expect",
                "expect": {"text_visible": ["検索結果"], "text_contains": ["3 件"]},
            },
        ]
    )
    steps = synthetic.run_browser_steps(page, scenario, suite)
    assert [step.passed for step in steps] == [True, True, True]
    assert ("mouse", 100, 200) in page.actions


def test_url_expectation_waits_for_navigation_and_counts_the_wait(monkeypatch):
    scenario, suite = browser_scenario(
        [
            {"action": "goto", "url": "https://www.example.com/"},
            {"action": "click", "selector": "#next"},
            {
                "action": "expect",
                "expect": {
                    "url_prefix": "https://www.example.com/done",
                    "max_latency_ms": 50,
                },
            },
        ]
    )
    page = FakePage()
    clock = iter([0.0, 0.0, 1.0, 1.0, 2.0])

    def navigates_later(predicate, timeout):
        page.url = "https://www.example.com/done"
        return

    page.wait_for_url = navigates_later
    monkeypatch.setattr(synthetic.time, "monotonic", lambda: next(clock, 10.0))
    with pytest.raises(synthetic.StepFailure) as info:
        synthetic.run_browser_steps(page, scenario, suite)
    # The URL was reached, but only after the latency budget.
    assert info.value.failure == "latency_exceeded"

    page = FakePage()
    with pytest.raises(synthetic.StepFailure) as info:
        synthetic.run_browser_steps(page, scenario, suite)
    assert info.value.failure == "redirect_mismatch"


def test_password_fields_are_never_filled_and_missing_elements_fail():
    scenario, suite = browser_scenario(
        [
            {"action": "goto", "url": "https://www.example.com/"},
            {"action": "fill", "selector": "#q", "value": "京都"},
        ]
    )
    page = FakePage(types={"#q": "password"})
    with pytest.raises(synthetic.StepFailure) as info:
        synthetic.run_browser_steps(page, scenario, suite)
    assert info.value.failure == "checker_error"
    assert not any(action[0] == "fill" for action in page.actions)

    scenario, suite = browser_scenario(
        [
            {"action": "goto", "url": "https://www.example.com/"},
            {"action": "expect", "expect": {"selector_visible": [".result"]}},
        ]
    )
    with pytest.raises(synthetic.StepFailure) as info:
        synthetic.run_browser_steps(FakePage(), scenario, suite)
    assert info.value.failure == "element_missing"


class FakeBrowser:
    version = "131.0"

    def __init__(self, page):
        self.page = page

    def new_context(self, **kwargs):
        self.context_args = kwargs
        return self

    def new_page(self):
        return self.page

    def close(self):
        pass


class FakePlaywright:
    def __init__(self, browser=None, launch_error=None):
        self.browser, self.launch_error = browser, launch_error
        self.chromium = self

    def launch(self, headless):
        if self.launch_error:
            raise self.launch_error
        return self.browser

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_server_errors_seen_by_the_page_fail_a_scenario_that_looks_fine():
    scenario, suite = browser_scenario(
        [{"action": "goto", "url": "https://www.example.com/"}]
    )
    page = FakePage()

    original_goto = page.goto

    def goto(url, wait_until, timeout):
        response = original_goto(url, wait_until, timeout)
        page.handlers["response"](
            type(
                "R", (), {"status": 502, "url": "https://api.example.net/search?q=x"}
            )()
        )
        return response

    page.goto = goto
    browser = FakeBrowser(page)
    result = synthetic.run_browser_attempt(
        scenario, suite, 1, lambda: FakePlaywright(browser)
    )
    assert result.failure == "server_error_response"
    assert result.page_errors["server_errors"] == [
        {"url": "https://api.example.net/search", "status": 502}
    ]
    assert browser.context_args["viewport"] == {"width": 1280, "height": 800}


def test_checker_faults_are_not_site_failures():
    scenario, suite = browser_scenario(
        [{"action": "goto", "url": "https://www.example.com/"}]
    )
    launch = synthetic.run_browser_attempt(
        scenario,
        suite,
        1,
        lambda: FakePlaywright(launch_error=RuntimeError("no chromium")),
    )
    assert launch.failure == "browser_launch_failed"

    def missing():
        raise ImportError("playwright")

    monkey = synthetic._load_playwright
    try:
        synthetic._load_playwright = missing
        result = run_scenario(
            scenario, suite, 3, {"status": "ok"}, sleep=lambda _: None
        )
    finally:
        synthetic._load_playwright = monkey
    assert result["outcome"] == "checker_error"
    assert result["attempts_made"] == 1  # no pointless retries


# --------------------------------------------------------------------------- tools


def test_run_service_checks_reports_not_configured(monkeypatch, tmp_path):
    from app.tools import service_checks

    monkeypatch.setattr(
        service_checks, "_suite_path", lambda: str(tmp_path / "none.json")
    )
    result = service_checks.run_service_checks(2)
    assert result["status"] == "not_configured" and result["complete"] is False


def test_run_service_checks_logs_each_verdict_and_refuses_concurrent_runs(
    monkeypatch, tmp_path, caplog
):
    from app.tools import service_checks

    path = tmp_path / "checks.json"
    path.write_text(json.dumps(suite_data()), encoding="utf-8")
    monkeypatch.setattr(service_checks, "_suite_path", lambda: str(path))
    monkeypatch.setattr(
        service_checks,
        "run_suite",
        lambda suite, attempts: run_suite(
            suite, attempts, sleep=lambda _: None, http_client=http_factory(healthy)
        ),
    )
    caplog.set_level("INFO", logger="app.tools.service_checks")
    result = service_checks.run_service_checks(2)
    assert result["status"] == "ok" and result["summary"]["pass"] == 1
    assert len(result["run_id"]) == 32 and result["suite_version"]
    logged = [
        json.loads(r.message)
        for r in caplog.records
        if "ops_service_check" in r.message
    ]
    assert logged[0]["outcome"] == "pass" and logged[0]["scenario"] == "top-page"

    assert service_checks._RUN_LOCK.acquire(blocking=False)
    try:
        busy = service_checks.run_service_checks(2)
    finally:
        service_checks._RUN_LOCK.release()
    assert busy["status"] == "partial" and "in progress" in busy["message"]


def test_service_check_fingerprint_is_accepted_by_issue_deduplication(monkeypatch):
    from app.tools import issues

    monkeypatch.setattr(
        issues, "find_existing_ops_issue", lambda fingerprint: {"status": "not_found"}
    )

    class Context:
        def __init__(self):
            self.state = {}

    result = issues.create_ops_issue(
        kind="error",
        title="top-page が失敗",
        body="本文",
        severity="high",
        fingerprint="service_check:top-page",
        service="web",
        search_text="",
        tool_context=Context(),
    )
    assert result["status"] == "dry_run"
    deploy = issues.create_ops_issue(
        kind="error",
        title="deploy",
        body="本文",
        severity="medium",
        fingerprint="deploy_verification:web:web-00042-abc",
        service="web",
        search_text="",
        tool_context=Context(),
    )
    assert deploy["status"] == "dry_run"
