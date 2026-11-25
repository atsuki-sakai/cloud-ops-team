"""Deterministic execution and judgement of service check scenarios.

Nothing here asks a model what to click or whether a run passed. A scenario
passes only when every step's expected outcome is observed: a finished
process, an HTTP 200 or a clicked button is never enough on its own.

Accuracy rules applied to every scenario:

- Failures are confirmed by retrying (``attempts``); a failure followed by a
  pass is ``flaky`` and the pre-retry failure is kept, never discarded.
- Failure classes separate site problems (status, content, latency, server
  errors seen by the page) from network reachability (DNS, connect, timeout)
  and from checker faults (browser missing or failing to start).
- An optional control URL outside the target tells a checker network outage
  apart from a site outage, so an outage of the checker is ``inconclusive``
  rather than an incident.
- Maintenance windows skip a scenario explicitly instead of hiding failures.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.redact import redact
from app.core.service_checks import CheckSuite, Scenario, safe_url
from app.core.timeutil import iso_utc, utcnow

USER_AGENT = "cloud-ops-team-service-check/1.0"
MAX_BODY_BYTES = 2_000_000

# Failure classes. Network classes are judged against the control check.
NETWORK = frozenset({"dns_error", "connect_error", "timeout"})
CHECKER = frozenset({"browser_unavailable", "browser_launch_failed", "checker_error"})
SITE = frozenset(
    {
        "http_status",
        "content_mismatch",
        "json_mismatch",
        "header_mismatch",
        "latency_exceeded",
        "redirect_mismatch",
        "tls_error",
        "element_missing",
        "target_mismatch",
        "outside_allowlist",
        "server_error_response",
        "console_errors",
        "navigation_error",
    }
)
OUTCOMES = (
    "pass",
    "flaky",
    "fail",
    "inconclusive",
    "checker_error",
    "maintenance",
    "not_run",
)


@dataclass
class StepResult:
    index: int
    action: str
    name: str
    passed: bool
    elapsed_ms: int
    failure: str = ""
    detail: str = ""
    observed: dict[str, Any] = field(default_factory=dict)

    def compact(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "index": self.index,
            "action": self.action,
            "passed": self.passed,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.name:
            out["name"] = self.name
        if self.failure:
            out["failure"] = self.failure
            out["detail"] = redact(self.detail)[:300]
        if self.observed:
            out["observed"] = self.observed
        return out


@dataclass
class AttemptResult:
    number: int
    started_at: str
    steps: list[StepResult]
    elapsed_ms: int
    failure: str = ""
    detail: str = ""
    page_errors: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.failure

    @property
    def failed_step(self) -> StepResult | None:
        return next((step for step in self.steps if not step.passed), None)

    def compact(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "attempt": self.number,
            "started_at": self.started_at,
            "passed": self.passed,
            "elapsed_ms": self.elapsed_ms,
            "steps_passed": sum(1 for step in self.steps if step.passed),
            "steps_total": len(self.steps),
        }
        if self.failure:
            out["failure"] = self.failure
            out["detail"] = redact(self.detail)[:300]
            failed = self.failed_step
            if failed is not None:
                out["failed_step"] = failed.compact()
        if self.page_errors:
            out["page_errors"] = self.page_errors
        return out


class StepFailure(Exception):
    def __init__(self, failure: str, detail: str, observed: dict | None = None):
        super().__init__(detail)
        self.failure = failure
        self.detail = detail
        self.observed = observed or {}


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


# --------------------------------------------------------------------------- expectations


def _json_path(document: Any, path: str) -> tuple[bool, Any]:
    current = document
    for piece in path.split("."):
        if isinstance(current, list) and piece.isdigit():
            index = int(piece)
            if index >= len(current):
                return False, None
            current = current[index]
        elif isinstance(current, Mapping) and piece in current:
            current = current[piece]
        else:
            return False, None
    return True, current


def _check_text(text: str, expect: Mapping[str, Any]) -> None:
    for needle in expect.get("text_contains", []):
        if needle not in text:
            raise StepFailure("content_mismatch", f"expected text not found: {needle}")
    for needle in expect.get("text_absent", []):
        if needle in text:
            raise StepFailure("content_mismatch", f"unexpected text present: {needle}")


def _check_latency(elapsed: int, expect: Mapping[str, Any]) -> None:
    limit = expect.get("max_latency_ms")
    if limit and elapsed > limit:
        raise StepFailure(
            "latency_exceeded",
            f"took {elapsed} ms (limit {limit} ms)",
            {"latency_ms": elapsed, "limit_ms": limit},
        )


def check_http_response(
    response: httpx.Response,
    body: bytes,
    elapsed: int,
    expect: Mapping[str, Any],
    suite: CheckSuite,
) -> dict[str, Any]:
    """Judge one response against the step's expectations; raise on mismatch."""
    final_url = str(response.url)
    observed = {
        "status_code": response.status_code,
        "final_url": safe_url(final_url),
        "redirects": len(response.history),
        "bytes": len(body),
        "latency_ms": elapsed,
    }
    if not suite.host_allowed(final_url):
        raise StepFailure(
            "outside_allowlist", "redirected outside allowed_hosts", observed
        )
    if response.status_code not in expect.get("status", [200]):
        raise StepFailure(
            "http_status",
            f"status {response.status_code} not in expected set",
            observed,
        )
    prefix = expect.get("url_prefix")
    if prefix and not final_url.startswith(prefix):
        raise StepFailure(
            "redirect_mismatch", "final URL does not match url_prefix", observed
        )
    content_type = response.headers.get("content-type", "")
    if expect.get("content_type") and expect["content_type"] not in content_type:
        raise StepFailure("header_mismatch", "unexpected content-type", observed)
    for name, needle in expect.get("headers_contain", {}).items():
        if needle not in response.headers.get(name, ""):
            raise StepFailure(
                "header_mismatch", f"header {name} lacks expected value", observed
            )
    if expect.get("min_bytes") and len(body) < expect["min_bytes"]:
        raise StepFailure(
            "content_mismatch", "response body is smaller than min_bytes", observed
        )
    text = body.decode(response.charset_encoding or "utf-8", errors="replace")
    try:
        _check_text(text, expect)
    except StepFailure as exc:
        exc.observed = observed
        raise
    if expect.get("json"):
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise StepFailure(
                "json_mismatch", "response is not JSON", observed
            ) from exc
        for rule in expect["json"]:
            found, value = _json_path(document, rule["path"])
            if rule.get("exists") is False:
                if found:
                    raise StepFailure(
                        "json_mismatch", f"{rule['path']} must be absent", observed
                    )
                continue
            if not found:
                raise StepFailure(
                    "json_mismatch", f"{rule['path']} is missing", observed
                )
            if "equals" in rule and value != rule["equals"]:
                raise StepFailure(
                    "json_mismatch", f"{rule['path']} has an unexpected value", observed
                )
    _check_latency(elapsed, expect)
    return observed


# --------------------------------------------------------------------------- http runner


def _classify_transport(exc: Exception) -> tuple[str, str]:
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", type(exc).__name__
    if isinstance(exc, httpx.TooManyRedirects):
        return "redirect_mismatch", "too many redirects"
    if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
        return "tls_error", "TLS handshake or certificate verification failed"
    if any(
        word in lowered
        for word in (
            "name or service not known",
            "nodename nor servname",
            "getaddrinfo",
            "name resolution",
            "no address associated",
        )
    ):
        return "dns_error", "host name could not be resolved"
    return "connect_error", type(exc).__name__


def run_http_attempt(
    scenario: Scenario,
    suite: CheckSuite,
    number: int,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> AttemptResult:
    started = time.monotonic()
    started_at = iso_utc(utcnow())
    steps: list[StepResult] = []
    with client_factory(
        follow_redirects=True,
        max_redirects=5,
        timeout=scenario.timeout_seconds,
        headers={"user-agent": USER_AGENT},
    ) as client:
        for index, step in enumerate(scenario.steps):
            step_start = time.monotonic()
            try:
                with client.stream(
                    step["method"], step["url"], headers=dict(step["headers"])
                ) as response:
                    body = b""
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BODY_BYTES:
                            break
                elapsed = _ms(step_start)
                observed = check_http_response(
                    response, body, elapsed, step["expect"], suite
                )
                steps.append(
                    StepResult(
                        index, "request", step["name"], True, elapsed, observed=observed
                    )
                )
            except StepFailure as exc:
                steps.append(
                    StepResult(
                        index,
                        "request",
                        step["name"],
                        False,
                        _ms(step_start),
                        exc.failure,
                        exc.detail,
                        exc.observed,
                    )
                )
                return AttemptResult(
                    number, started_at, steps, _ms(started), exc.failure, exc.detail
                )
            except httpx.HTTPError as exc:
                failure, detail = _classify_transport(exc)
                steps.append(
                    StepResult(
                        index,
                        "request",
                        step["name"],
                        False,
                        _ms(step_start),
                        failure,
                        detail,
                    )
                )
                return AttemptResult(
                    number, started_at, steps, _ms(started), failure, detail
                )
    total = _ms(started)
    if total > scenario.max_total_ms:
        return AttemptResult(
            number,
            started_at,
            steps,
            total,
            "latency_exceeded",
            f"scenario took {total} ms (limit {scenario.max_total_ms} ms)",
        )
    return AttemptResult(number, started_at, steps, total)


# --------------------------------------------------------------------------- browser runner

_POINT_PROBE = """([x, y, selector]) => {
  const target = document.querySelector(selector);
  if (!target) return "missing";
  const hit = document.elementFromPoint(x, y);
  if (!hit) return "nothing";
  return target === hit || target.contains(hit) ? "ok" : "mismatch";
}"""


def _classify_browser(exc: Exception) -> tuple[str, str]:
    text = str(exc)
    name = type(exc).__name__
    if "ERR_NAME_NOT_RESOLVED" in text:
        return "dns_error", "host name could not be resolved"
    if "ERR_CERT" in text or "SSL" in text:
        return "tls_error", "TLS certificate error"
    if "ERR_CONNECTION" in text or "ERR_ADDRESS" in text or "ERR_INTERNET" in text:
        return "connect_error", "connection failed"
    if "Timeout" in name or "Timeout" in text:
        return "timeout", "timed out"
    return "navigation_error", name


def _visible(locator: Any) -> Any:
    """First visible match: hidden duplicates (templates, <title>) must not decide."""
    return locator.filter(visible=True).first


def _browser_expect(page: Any, expect: Mapping[str, Any], timeout_ms: int) -> None:
    prefix = expect.get("url_prefix")
    if prefix:
        # A click starts navigation asynchronously; wait for the expected URL
        # instead of judging the page that is still being left.
        try:
            page.wait_for_url(lambda url: url.startswith(prefix), timeout=timeout_ms)
        except Exception as exc:
            raise StepFailure(
                "redirect_mismatch", "page URL did not reach url_prefix"
            ) from exc
    title = expect.get("title_contains")
    if title and title not in (page.title() or ""):
        raise StepFailure(
            "content_mismatch", "page title does not contain the expected text"
        )
    for selector in expect.get("selector_visible", []):
        try:
            _visible(page.locator(selector)).wait_for(
                state="visible", timeout=timeout_ms
            )
        except Exception as exc:
            raise StepFailure("element_missing", f"not visible: {selector}") from exc
    for text in expect.get("text_visible", []):
        try:
            # <title> and hidden templates also contain text: only visible matches count.
            _visible(page.get_by_text(text)).wait_for(
                state="visible", timeout=timeout_ms
            )
        except Exception as exc:
            raise StepFailure("element_missing", f"text not visible: {text}") from exc
    for selector in expect.get("selector_absent", []):
        if page.locator(selector).filter(visible=True).count() > 0:
            raise StepFailure(
                "content_mismatch", f"unexpected element present: {selector}"
            )
    for rule in expect.get("min_count", []):
        count = page.locator(rule["selector"]).filter(visible=True).count()
        if count < rule["count"]:
            raise StepFailure(
                "content_mismatch",
                f"{rule['selector']} matched {count} (expected at least {rule['count']})",
                {"count": count},
            )
    if expect.get("text_contains") or expect.get("text_absent"):
        _check_text(page.locator("body").inner_text(timeout=timeout_ms), expect)


def run_browser_steps(
    page: Any, scenario: Scenario, suite: CheckSuite
) -> list[StepResult]:
    """Replay the recorded steps on a Playwright-compatible page.

    Raises ``StepFailure`` with the step results so far attached as ``steps``.
    """
    timeout_ms = scenario.timeout_seconds * 1000
    results: list[StepResult] = []
    for index, step in enumerate(scenario.steps):
        start = time.monotonic()
        action = step["action"]
        observed: dict[str, Any] = {}
        try:
            try:
                if action == "goto":
                    response = page.goto(
                        step["url"], wait_until=step["wait_until"], timeout=timeout_ms
                    )
                    status = getattr(response, "status", None)
                    observed["status_code"] = status
                    if status is None or status >= 400:
                        raise StepFailure(
                            "http_status", f"navigation returned {status}"
                        )
                elif action == "click" and "selector" in step:
                    _visible(page.locator(step["selector"])).click(timeout=timeout_ms)
                elif action == "click":
                    verdict = page.evaluate(
                        _POINT_PROBE, [step["x"], step["y"], step["expect_target"]]
                    )
                    if verdict != "ok":
                        # Never click whatever happens to be at the point.
                        raise StepFailure(
                            "target_mismatch"
                            if verdict != "missing"
                            else "element_missing",
                            f"element at ({step['x']}, {step['y']}) is not the recorded target ({verdict})",
                        )
                    page.mouse.click(step["x"], step["y"])
                elif action == "fill":
                    field = _visible(page.locator(step["selector"]))
                    field_type = (
                        field.get_attribute("type", timeout=timeout_ms) or ""
                    ).lower()
                    if field_type == "password":
                        raise StepFailure(
                            "checker_error", "password fields are never filled"
                        )
                    field.fill(step["value"], timeout=timeout_ms)
                elif action == "press":
                    page.keyboard.press(step["key"])
                elif action == "scroll":
                    page.mouse.wheel(step["delta_x"], step["delta_y"])
                elif action == "wait":
                    page.wait_for_timeout(step["ms"])
            except StepFailure:
                raise
            except Exception as exc:
                failure, detail = _classify_browser(exc)
                if action in ("click", "fill") and failure in (
                    "timeout",
                    "navigation_error",
                ):
                    failure, detail = (
                        "element_missing",
                        f"{action} target not actionable",
                    )
                raise StepFailure(failure, detail) from exc
            if not suite.host_allowed(str(page.url)):
                raise StepFailure("outside_allowlist", "page left allowed_hosts")
            _browser_expect(page, step["expect"], timeout_ms)
            if not suite.host_allowed(str(page.url)):
                raise StepFailure("outside_allowlist", "page left allowed_hosts")
            # Latency includes waiting for the expected state, not just the action.
            elapsed = _ms(start)
            _check_latency(elapsed, step["expect"])
            observed["url"] = safe_url(str(page.url))
            results.append(
                StepResult(
                    index, action, step["name"], True, elapsed, observed=observed
                )
            )
        except StepFailure as exc:
            observed["url"] = safe_url(str(getattr(page, "url", "")))
            results.append(
                StepResult(
                    index,
                    action,
                    step["name"],
                    False,
                    _ms(start),
                    exc.failure,
                    exc.detail,
                    {**observed, **exc.observed},
                )
            )
            exc.steps = results  # type: ignore[attr-defined]
            raise
    return results


def _load_playwright() -> Any:
    from playwright.sync_api import sync_playwright  # optional dependency

    return sync_playwright


def run_browser_attempt(
    scenario: Scenario,
    suite: CheckSuite,
    number: int,
    playwright_factory: Callable[[], Any] | None = None,
) -> AttemptResult:
    started = time.monotonic()
    started_at = iso_utc(utcnow())
    try:
        factory = playwright_factory or _load_playwright()
    except ImportError:
        return AttemptResult(
            number,
            started_at,
            [],
            0,
            "browser_unavailable",
            "Playwright is not installed (install the 'browser' extra and Chromium)",
        )
    conditions = scenario.browser
    server_errors: list[dict[str, Any]] = []
    console_errors = 0
    steps: list[StepResult] = []
    failure = detail = ""
    try:
        with factory() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                return AttemptResult(
                    number,
                    started_at,
                    [],
                    _ms(started),
                    "browser_launch_failed",
                    type(exc).__name__,
                )
            try:
                context = browser.new_context(
                    viewport=dict(conditions["viewport"]),
                    device_scale_factor=conditions["device_scale_factor"],
                    locale=conditions["locale"],
                    timezone_id=conditions["timezone"],
                    color_scheme=conditions["color_scheme"],
                    is_mobile=conditions["is_mobile"],
                    service_workers="block",
                )
                page = context.new_page()

                def on_response(response: Any) -> None:
                    if response.status >= 500 and suite.host_allowed(response.url):
                        server_errors.append(
                            {"url": safe_url(response.url), "status": response.status}
                        )

                def on_console(message: Any) -> None:
                    nonlocal console_errors
                    if message.type == "error":
                        console_errors += 1

                page.on("response", on_response)
                page.on("console", on_console)
                try:
                    steps = run_browser_steps(page, scenario, suite)
                except StepFailure as exc:
                    steps = getattr(exc, "steps", [])
                    failure, detail = exc.failure, exc.detail
                version = browser.version
            finally:
                browser.close()
    except Exception as exc:  # the checker itself failed, not the site
        return AttemptResult(
            number, started_at, steps, _ms(started), "checker_error", type(exc).__name__
        )
    total = _ms(started)
    page_errors = {
        "server_errors": server_errors[:10],
        "server_error_count": len(server_errors),
        "console_error_count": console_errors,
        "browser_version": version,
    }
    if not failure and scenario.fail_on_http_5xx and server_errors:
        failure, detail = (
            "server_error_response",
            f"{len(server_errors)} responses with 5xx from allowed hosts during the scenario",
        )
    if not failure and 0 <= scenario.max_console_errors < console_errors:
        failure, detail = "console_errors", f"{console_errors} console errors"
    if not failure and total > scenario.max_total_ms:
        failure, detail = "latency_exceeded", f"scenario took {total} ms"
    return AttemptResult(number, started_at, steps, total, failure, detail, page_errors)


# --------------------------------------------------------------------------- judgement


def control_check(
    url: str, client_factory: Callable[..., httpx.Client] = httpx.Client
) -> dict:
    """Reachability of a URL outside the target, to judge the checker's own network."""
    if not url:
        return {"status": "not_configured"}
    start = time.monotonic()
    try:
        with client_factory(
            follow_redirects=True, timeout=10, headers={"user-agent": USER_AGENT}
        ) as client:
            response = client.get(url)
        return {
            "status": "ok" if response.status_code < 500 else "failed",
            "status_code": response.status_code,
            "latency_ms": _ms(start),
        }
    except httpx.HTTPError as exc:
        failure, _ = _classify_transport(exc)
        return {"status": "failed", "failure": failure, "latency_ms": _ms(start)}


def judge(attempts: list[AttemptResult], control: Mapping[str, Any]) -> str:
    """Outcome of one scenario from its attempts (stops at the first pass)."""
    if not attempts:
        return "not_run"
    if attempts[0].passed:
        return "pass"
    if any(attempt.passed for attempt in attempts):
        return "flaky"
    failures = {attempt.failure for attempt in attempts}
    if failures <= CHECKER:
        return "checker_error"
    if failures <= NETWORK | CHECKER and control.get("status") == "failed":
        return "inconclusive"
    return "fail"


def severity_for(scenario: Scenario, outcome: str) -> str:
    if outcome == "fail":
        return "high" if scenario.critical else "medium"
    if outcome == "flaky":
        return "low"
    return "info"


@contextmanager
def _deadline(seconds: int) -> Iterator[Callable[[], bool]]:
    end = time.monotonic() + seconds
    yield lambda: time.monotonic() >= end


def run_scenario(
    scenario: Scenario,
    suite: CheckSuite,
    attempts: int,
    control: Mapping[str, Any],
    *,
    expired: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    http_client: Callable[..., httpx.Client] = httpx.Client,
    playwright_factory: Callable[[], Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Run one scenario with confirmation retries and return its compact result."""
    when = now or utcnow()
    base: dict[str, Any] = {
        "id": scenario.id,
        "version": scenario.version,
        "service": scenario.service,
        "environment": scenario.environment,
        "critical": scenario.critical,
        "fingerprint": scenario.fingerprint,
        "conditions": scenario.conditions(),
        "target_urls": sorted(
            {safe_url(step["url"]) for step in scenario.steps if step.get("url")}
        ),
    }
    window = suite.maintenance_for(scenario.id, when)
    if window is not None:
        return {
            **base,
            "outcome": "maintenance",
            "severity": "info",
            "maintenance": {
                "start": iso_utc(window.start),
                "end": iso_utc(window.end),
                "reason": redact(window.reason)[:200],
            },
            "attempts": [],
        }
    limit = max(1, min(attempts, scenario.attempts))
    results: list[AttemptResult] = []
    for number in range(1, limit + 1):
        if expired():
            break
        if scenario.runner == "http":
            result = run_http_attempt(scenario, suite, number, http_client)
        else:
            result = run_browser_attempt(scenario, suite, number, playwright_factory)
        results.append(result)
        if result.passed or result.failure in CHECKER:
            break
        if number < limit and scenario.attempt_interval_seconds:
            sleep(scenario.attempt_interval_seconds)
    outcome = judge(results, control)
    if not results:
        return {
            **base,
            "outcome": "not_run",
            "severity": "info",
            "reason": "run_budget_exhausted",
            "attempts": [],
        }
    first_failure = next((r for r in results if not r.passed), None)
    out = {
        **base,
        "outcome": outcome,
        "severity": severity_for(scenario, outcome),
        "attempts": [result.compact() for result in results],
        "attempts_made": len(results),
        "attempts_allowed": limit,
        "latency_ms": {
            "first": results[0].elapsed_ms,
            "max": max(result.elapsed_ms for result in results),
        },
    }
    if first_failure is not None:
        out["failure"] = first_failure.failure
        failed = first_failure.failed_step
        if failed is not None:
            out["failed_step"] = {
                "index": failed.index,
                "action": failed.action,
                "name": failed.name,
            }
    if outcome == "inconclusive":
        out["reason"] = "control_check_failed"
    if outcome == "fail" and len(results) < limit:
        out["confirmation"] = "stopped_early_on_checker_fault"
    return out


def run_suite(
    suite: CheckSuite,
    attempts: int,
    *,
    scenarios: tuple[Scenario, ...] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    http_client: Callable[..., httpx.Client] = httpx.Client,
    playwright_factory: Callable[[], Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Run scenarios sequentially within the suite's time budget."""
    selected = suite.scenarios if scenarios is None else scenarios
    control = control_check(suite.control_url, http_client)
    rows = []
    with _deadline(suite.run_budget_seconds) as expired:
        for scenario in selected:
            if expired():
                rows.append(
                    {
                        "id": scenario.id,
                        "version": scenario.version,
                        "service": scenario.service,
                        "fingerprint": scenario.fingerprint,
                        "critical": scenario.critical,
                        "outcome": "not_run",
                        "severity": "info",
                        "reason": "run_budget_exhausted",
                        "attempts": [],
                    }
                )
                continue
            rows.append(
                run_scenario(
                    scenario,
                    suite,
                    attempts,
                    control,
                    expired=expired,
                    sleep=sleep,
                    http_client=http_client,
                    playwright_factory=playwright_factory,
                    now=now,
                )
            )
    summary = dict.fromkeys(OUTCOMES, 0)
    for row in rows:
        summary[row["outcome"]] += 1
    return {"control": control, "scenarios": rows, "summary": summary}
