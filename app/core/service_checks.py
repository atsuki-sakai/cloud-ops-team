"""Operator-defined service check scenarios: loading, validation and versioning.

Scenarios are configuration written by operators (``OPS_SERVICE_CHECKS_FILE``),
never input chosen by a model. Every URL a scenario may open is bounded by the
file's ``allowed_hosts`` list, only read-only HTTP methods are accepted, and a
scenario with side effects (bookings, payments, notifications) is rejected
until dedicated targets and clean-up rules exist. Each scenario carries a
content hash so scheduled, manual and post-deployment runs can prove they used
the same version.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.core.redact import redact
from app.core.timeutil import parse_time

MAX_SCENARIOS = 20
MAX_STEPS = 30
MAX_ATTEMPTS = 5
MAX_TEXT = 500
RUNNERS = ("http", "browser")
ENVIRONMENTS = ("production", "staging", "candidate")
HTTP_METHODS = ("GET", "HEAD")
BROWSER_ACTIONS = ("goto", "click", "fill", "press", "scroll", "wait", "expect")
ALLOWED_KEYS = (
    "Enter",
    "Tab",
    "Escape",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "PageDown",
    "PageUp",
    "Home",
    "End",
    "Space",
)
# Request headers a scenario may set. Credentials, cookies and arbitrary
# headers are excluded: saved authentication is a separate, later feature.
ALLOWED_HEADERS = ("accept", "accept-language", "cache-control")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HOST = re.compile(r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_CREDENTIAL_WORDS = re.compile(
    r"password|passwd|secret|token|api[_-]?key|cookie|authorization", re.I
)

DEFAULTS: dict[str, Any] = {
    "attempts": 2,
    "attempt_interval_seconds": 5,
    "timeout_seconds": 15,
    "max_total_ms": 30_000,
    "fail_on_http_5xx": True,
    "max_console_errors": -1,
}
DEPLOY_DEFAULTS: dict[str, Any] = {
    "min_requests": 30,
    "error_ratio_factor": 2.0,
    "error_ratio_min_increase": 0.01,
    "min_new_5xx": 3,
    "latency_p95_factor": 1.5,
    "latency_p95_min_increase_ms": 200,
}
RUN_LIMITATIONS = (
    "Results describe the checker's region, network and browser conditions, not every user's experience.",
    "A scenario passing proves only the recorded path; unrecorded pages and actions are unchecked.",
    "Cross-run state (consecutive runs, missed schedules) comes from open issues and logs, not a run store.",
)
BROWSER_DEFAULTS: dict[str, Any] = {
    "viewport": {"width": 1280, "height": 800},
    "device_scale_factor": 1,
    "locale": "ja-JP",
    "timezone": "Asia/Tokyo",
    "color_scheme": "light",
    "is_mobile": False,
}


class ScenarioError(ValueError):
    """A scenario file that cannot be used; the message names the field."""


@dataclass(frozen=True)
class Scenario:
    id: str
    version: str
    service: str
    environment: str
    runner: str
    critical: bool
    steps: tuple[Mapping[str, Any], ...]
    attempts: int
    attempt_interval_seconds: int
    timeout_seconds: int
    max_total_ms: int
    fail_on_http_5xx: bool
    max_console_errors: int
    browser: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return f"service_check:{self.id}"

    def conditions(self) -> dict[str, Any]:
        """Execution conditions recorded with every result."""
        out: dict[str, Any] = {"runner": self.runner}
        if self.runner == "browser":
            out.update(dict(self.browser))
        return out


@dataclass(frozen=True)
class MaintenanceWindow:
    start: dt.datetime
    end: dt.datetime
    scenarios: tuple[str, ...]
    reason: str

    def covers(self, scenario_id: str, when: dt.datetime) -> bool:
        return self.start <= when < self.end and (
            not self.scenarios or scenario_id in self.scenarios
        )


@dataclass(frozen=True)
class CheckSuite:
    version: str
    allowed_hosts: tuple[str, ...]
    scenarios: tuple[Scenario, ...]
    control_url: str
    maintenance: tuple[MaintenanceWindow, ...]
    run_budget_seconds: int
    deploy: Mapping[str, Any]

    def host_allowed(self, url: str) -> bool:
        return host_allowed(url, self.allowed_hosts)

    def maintenance_for(
        self, scenario_id: str, when: dt.datetime
    ) -> MaintenanceWindow | None:
        return next((w for w in self.maintenance if w.covers(scenario_id, when)), None)


def host_allowed(url: str, allowed: tuple[str, ...]) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return False
    for pattern in allowed:
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]) and host != pattern[2:]:
                return True
        elif host == pattern:
            return True
    return False


def safe_url(url: str) -> str:
    """Scheme, host and path only: queries and fragments can carry personal data."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.hostname:
        return ""
    return redact(f"{parts.scheme}://{parts.hostname}{parts.path or '/'}")[:300]


def _text(value: Any, where: str, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ScenarioError(f"{where} must be a non-empty string")
    if len(value) > MAX_TEXT:
        raise ScenarioError(f"{where} is longer than {MAX_TEXT} characters")
    if redact(value) != value:
        raise ScenarioError(f"{where} looks like a credential or personal data")
    return value


def _texts(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ScenarioError(f"{where} must be a list of at most 20 strings")
    return [_text(item, f"{where}[{index}]") for index, item in enumerate(value)]


def _int(value: Any, where: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ScenarioError(f"{where} must be an integer between {low} and {high}")
    return value


def _number(value: Any, where: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not low <= value <= high:
        raise ScenarioError(f"{where} must be a number between {low} and {high}")
    return float(value)


def _bool(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise ScenarioError(f"{where} must be true or false")
    return value


def _only(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ScenarioError(f"{where} has unsupported fields: {', '.join(unknown)}")


def _url(value: Any, where: str, hosts: tuple[str, ...]) -> str:
    url = _text(value, where)
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ScenarioError(f"{where} must use https")
    if parts.username or parts.password:
        raise ScenarioError(f"{where} must not embed credentials")
    if not host_allowed(url, hosts):
        raise ScenarioError(f"{where} host is not listed in allowed_hosts")
    return url


def _expectations(data: Any, where: str, runner: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ScenarioError(f"{where} must be an object")
    common = {"max_latency_ms", "url_prefix", "text_contains", "text_absent"}
    http_only = {
        "status",
        "json",
        "headers_contain",
        "content_type",
        "min_bytes",
    }
    browser_only = {
        "title_contains",
        "selector_visible",
        "selector_absent",
        "text_visible",
        "min_count",
    }
    _only(data, common | (http_only if runner == "http" else browser_only), where)
    out: dict[str, Any] = {}
    if "max_latency_ms" in data:
        out["max_latency_ms"] = _int(
            data["max_latency_ms"], f"{where}.max_latency_ms", 1, 120_000
        )
    if "url_prefix" in data:
        out["url_prefix"] = _text(data["url_prefix"], f"{where}.url_prefix")
    for key in ("text_contains", "text_absent", "selector_visible", "text_visible"):
        if key in data:
            out[key] = _texts(data[key], f"{where}.{key}")
    if "selector_absent" in data:
        out["selector_absent"] = _texts(
            data["selector_absent"], f"{where}.selector_absent"
        )
    if "title_contains" in data:
        out["title_contains"] = _text(data["title_contains"], f"{where}.title_contains")
    if "min_count" in data:
        rows = data["min_count"]
        if not isinstance(rows, list) or len(rows) > 10:
            raise ScenarioError(f"{where}.min_count must be a list")
        out["min_count"] = []
        for index, row in enumerate(rows):
            spot = f"{where}.min_count[{index}]"
            if not isinstance(row, Mapping):
                raise ScenarioError(f"{spot} must be an object")
            _only(row, {"selector", "count"}, spot)
            out["min_count"].append(
                {
                    "selector": _text(row.get("selector"), f"{spot}.selector"),
                    "count": _int(row.get("count"), f"{spot}.count", 1, 1000),
                }
            )
    if "status" in data:
        codes = data["status"]
        if not isinstance(codes, list) or not codes or len(codes) > 10:
            raise ScenarioError(f"{where}.status must be a non-empty list")
        out["status"] = [
            _int(code, f"{where}.status[{i}]", 100, 599) for i, code in enumerate(codes)
        ]
    if "json" in data:
        rows = data["json"]
        if not isinstance(rows, list) or len(rows) > 20:
            raise ScenarioError(f"{where}.json must be a list")
        out["json"] = []
        for index, row in enumerate(rows):
            spot = f"{where}.json[{index}]"
            if not isinstance(row, Mapping) or "path" not in row:
                raise ScenarioError(f"{spot} needs a path")
            _only(row, {"path", "equals", "exists"}, spot)
            item: dict[str, Any] = {"path": _text(row["path"], f"{spot}.path")}
            if "equals" in row:
                value = row["equals"]
                if (
                    not isinstance(value, str | int | float | bool)
                    and value is not None
                ):
                    raise ScenarioError(f"{spot}.equals must be a scalar")
                if isinstance(value, str):
                    _text(value, f"{spot}.equals")
                item["equals"] = value
            if "exists" in row:
                item["exists"] = _bool(row["exists"], f"{spot}.exists")
            out["json"].append(item)
    if "headers_contain" in data:
        headers = data["headers_contain"]
        if not isinstance(headers, Mapping) or len(headers) > 10:
            raise ScenarioError(f"{where}.headers_contain must be an object")
        out["headers_contain"] = {
            _text(name, f"{where}.headers_contain key").lower(): _text(
                value, f"{where}.headers_contain.{name}"
            )
            for name, value in headers.items()
        }
    if "content_type" in data:
        out["content_type"] = _text(data["content_type"], f"{where}.content_type")
    if "min_bytes" in data:
        out["min_bytes"] = _int(data["min_bytes"], f"{where}.min_bytes", 1, 50_000_000)
    return out


def _http_step(data: Mapping[str, Any], where: str, hosts: tuple[str, ...]) -> dict:
    _only(data, {"action", "method", "url", "headers", "expect", "name"}, where)
    if data.get("action", "request") != "request":
        raise ScenarioError(f"{where}.action must be request for the http runner")
    method = data.get("method", "GET")
    if method not in HTTP_METHODS:
        raise ScenarioError(f"{where}.method must be GET or HEAD (read-only checks)")
    headers = data.get("headers") or {}
    if not isinstance(headers, Mapping):
        raise ScenarioError(f"{where}.headers must be an object")
    clean_headers = {}
    for name, value in headers.items():
        if not isinstance(name, str) or name.lower() not in ALLOWED_HEADERS:
            raise ScenarioError(
                f"{where}.headers allows only {', '.join(ALLOWED_HEADERS)}"
            )
        clean_headers[name.lower()] = _text(value, f"{where}.headers.{name}")
    expect = _expectations(data.get("expect"), f"{where}.expect", "http")
    expect.setdefault("status", [200])
    return {
        "action": "request",
        "name": _text(data.get("name"), f"{where}.name", required=False),
        "method": method,
        "url": _url(data.get("url"), f"{where}.url", hosts),
        "headers": clean_headers,
        "expect": expect,
    }


def _browser_step(
    data: Mapping[str, Any], where: str, hosts: tuple[str, ...], viewport: Mapping
) -> dict:
    action = data.get("action")
    if action not in BROWSER_ACTIONS:
        raise ScenarioError(
            f"{where}.action must be one of {', '.join(BROWSER_ACTIONS)}"
        )
    name = _text(data.get("name"), f"{where}.name", required=False)
    out: dict[str, Any] = {"action": action, "name": name}
    if action == "goto":
        _only(data, {"action", "name", "url", "wait_until", "expect"}, where)
        out["url"] = _url(data.get("url"), f"{where}.url", hosts)
        wait_until = data.get("wait_until", "load")
        if wait_until not in ("load", "domcontentloaded", "networkidle"):
            raise ScenarioError(f"{where}.wait_until is not supported")
        out["wait_until"] = wait_until
    elif action == "click":
        _only(
            data,
            {"action", "name", "selector", "x", "y", "expect_target", "expect"},
            where,
        )
        if "selector" in data:
            if "x" in data or "y" in data:
                raise ScenarioError(f"{where} uses either selector or x/y")
            out["selector"] = _text(data["selector"], f"{where}.selector")
        else:
            # Coordinates are CSS pixels inside the recorded viewport. A target
            # selector is mandatory: the runner refuses to click when the
            # element under the point is not the recorded one (moved button,
            # overlapping dialog) instead of clicking whatever is there.
            out["x"] = _int(data.get("x"), f"{where}.x", 0, viewport["width"] - 1)
            out["y"] = _int(data.get("y"), f"{where}.y", 0, viewport["height"] - 1)
            out["expect_target"] = _text(
                data.get("expect_target"), f"{where}.expect_target"
            )
    elif action == "fill":
        _only(data, {"action", "name", "selector", "value", "expect"}, where)
        out["selector"] = _text(data.get("selector"), f"{where}.selector")
        if _CREDENTIAL_WORDS.search(out["selector"]):
            raise ScenarioError(f"{where} must not fill credential fields")
        out["value"] = _text(data.get("value"), f"{where}.value")
    elif action == "press":
        _only(data, {"action", "name", "key", "expect"}, where)
        if data.get("key") not in ALLOWED_KEYS:
            raise ScenarioError(f"{where}.key must be one of {', '.join(ALLOWED_KEYS)}")
        out["key"] = data["key"]
    elif action == "scroll":
        _only(data, {"action", "name", "delta_x", "delta_y", "expect"}, where)
        out["delta_x"] = _int(
            data.get("delta_x", 0), f"{where}.delta_x", -20_000, 20_000
        )
        out["delta_y"] = _int(
            data.get("delta_y", 0), f"{where}.delta_y", -20_000, 20_000
        )
    elif action == "wait":
        _only(data, {"action", "name", "ms", "expect"}, where)
        out["ms"] = _int(data.get("ms"), f"{where}.ms", 1, 10_000)
    else:
        _only(data, {"action", "name", "expect"}, where)
    out["expect"] = _expectations(data.get("expect"), f"{where}.expect", "browser")
    if action == "expect" and not out["expect"]:
        raise ScenarioError(f"{where}.expect must contain at least one expectation")
    return out


def _browser_conditions(data: Any, where: str) -> dict[str, Any]:
    data = {} if data is None else data
    if not isinstance(data, Mapping):
        raise ScenarioError(f"{where} must be an object")
    _only(data, set(BROWSER_DEFAULTS), where)
    merged = {**BROWSER_DEFAULTS, **data}
    viewport = merged["viewport"]
    if not isinstance(viewport, Mapping):
        raise ScenarioError(f"{where}.viewport must be an object")
    _only(viewport, {"width", "height"}, f"{where}.viewport")
    color = merged["color_scheme"]
    if color not in ("light", "dark"):
        raise ScenarioError(f"{where}.color_scheme must be light or dark")
    return {
        "viewport": {
            "width": _int(viewport.get("width"), f"{where}.viewport.width", 200, 3840),
            "height": _int(
                viewport.get("height"), f"{where}.viewport.height", 200, 3840
            ),
        },
        "device_scale_factor": _number(
            merged["device_scale_factor"], f"{where}.device_scale_factor", 1, 4
        ),
        "locale": _text(merged["locale"], f"{where}.locale"),
        "timezone": _text(merged["timezone"], f"{where}.timezone"),
        "color_scheme": color,
        "is_mobile": _bool(merged["is_mobile"], f"{where}.is_mobile"),
    }


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _scenario(
    data: Any, index: int, hosts: tuple[str, ...], defaults: Mapping[str, Any]
) -> Scenario:
    where = f"scenarios[{index}]"
    if not isinstance(data, Mapping):
        raise ScenarioError(f"{where} must be an object")
    _only(
        data,
        {
            "id",
            "description",
            "service",
            "environment",
            "runner",
            "critical",
            "side_effects",
            "steps",
            "browser",
            *DEFAULTS,
        },
        where,
    )
    scenario_id = data.get("id")
    if not isinstance(scenario_id, str) or not _ID.fullmatch(scenario_id):
        raise ScenarioError(f"{where}.id must be lowercase letters, digits and '-'")
    if data.get("side_effects", "none") != "none":
        raise ScenarioError(
            f"{where}.side_effects: scenarios that book, pay or notify are not enabled"
        )
    runner = data.get("runner", "http")
    if runner not in RUNNERS:
        raise ScenarioError(f"{where}.runner must be http or browser")
    environment = data.get("environment", "production")
    if environment not in ENVIRONMENTS:
        raise ScenarioError(f"{where}.environment must be one of {ENVIRONMENTS}")
    service = _text(data.get("service"), f"{where}.service", required=False)
    if service and not _ID.fullmatch(service):
        raise ScenarioError(f"{where}.service must be a service name")
    merged = {**DEFAULTS, **defaults, **{k: data[k] for k in DEFAULTS if k in data}}
    browser = (
        _browser_conditions(data.get("browser"), f"{where}.browser")
        if runner == "browser"
        else {}
    )
    if runner == "http" and "browser" in data:
        raise ScenarioError(f"{where}.browser applies only to the browser runner")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise ScenarioError(f"{where}.steps must contain 1-{MAX_STEPS} steps")
    parsed = []
    for step_index, step in enumerate(steps):
        spot = f"{where}.steps[{step_index}]"
        if not isinstance(step, Mapping):
            raise ScenarioError(f"{spot} must be an object")
        parsed.append(
            _http_step(step, spot, hosts)
            if runner == "http"
            else _browser_step(step, spot, hosts, browser["viewport"])
        )
    if runner == "browser" and parsed[0]["action"] != "goto":
        raise ScenarioError(f"{where}.steps[0] must be a goto step")
    normalized = {
        "id": scenario_id,
        "service": service,
        "environment": environment,
        "runner": runner,
        "critical": _bool(data.get("critical", False), f"{where}.critical"),
        "steps": parsed,
        "attempts": _int(merged["attempts"], f"{where}.attempts", 1, MAX_ATTEMPTS),
        "attempt_interval_seconds": _int(
            merged["attempt_interval_seconds"],
            f"{where}.attempt_interval_seconds",
            0,
            60,
        ),
        "timeout_seconds": _int(
            merged["timeout_seconds"], f"{where}.timeout_seconds", 1, 60
        ),
        "max_total_ms": _int(
            merged["max_total_ms"], f"{where}.max_total_ms", 100, 300_000
        ),
        "fail_on_http_5xx": _bool(
            merged["fail_on_http_5xx"], f"{where}.fail_on_http_5xx"
        ),
        "max_console_errors": _int(
            merged["max_console_errors"], f"{where}.max_console_errors", -1, 1000
        ),
        "browser": browser,
    }
    return Scenario(
        id=scenario_id,
        version=_digest(normalized),
        service=service,
        environment=environment,
        runner=runner,
        critical=normalized["critical"],
        steps=tuple(parsed),
        attempts=normalized["attempts"],
        attempt_interval_seconds=normalized["attempt_interval_seconds"],
        timeout_seconds=normalized["timeout_seconds"],
        max_total_ms=normalized["max_total_ms"],
        fail_on_http_5xx=normalized["fail_on_http_5xx"],
        max_console_errors=normalized["max_console_errors"],
        browser=browser,
    )


def _maintenance(data: Any, ids: set[str]) -> tuple[MaintenanceWindow, ...]:
    if data is None:
        return ()
    if not isinstance(data, list) or len(data) > 50:
        raise ScenarioError("maintenance must be a list of at most 50 windows")
    windows = []
    for index, row in enumerate(data):
        where = f"maintenance[{index}]"
        if not isinstance(row, Mapping):
            raise ScenarioError(f"{where} must be an object")
        _only(row, {"start", "end", "scenarios", "reason"}, where)
        start = parse_time(row.get("start") or "")
        end = parse_time(row.get("end") or "")
        if (
            start is None
            or end is None
            or "T" not in str(row.get("start"))
            or end <= start
            or end - start > dt.timedelta(days=7)
        ):
            raise ScenarioError(
                f"{where} needs RFC 3339 start/end with end after start (max 7 days)"
            )
        scenarios = tuple(_texts(row.get("scenarios"), f"{where}.scenarios"))
        unknown = set(scenarios) - ids
        if unknown:
            raise ScenarioError(f"{where}.scenarios references unknown scenarios")
        windows.append(
            MaintenanceWindow(
                start=start,
                end=end,
                scenarios=scenarios,
                reason=_text(row.get("reason"), f"{where}.reason", required=False),
            )
        )
    return tuple(windows)


def parse_suite(data: Any) -> CheckSuite:
    """Validate a decoded scenario file; raise ``ScenarioError`` on any problem."""
    if not isinstance(data, Mapping):
        raise ScenarioError("The service check file must contain a JSON object")
    _only(
        data,
        {
            "version",
            "description",
            "allowed_hosts",
            "control_url",
            "defaults",
            "maintenance",
            "run_budget_seconds",
            "deploy",
            "scenarios",
        },
        "file",
    )
    if data.get("version") != 1:
        raise ScenarioError("version must be 1")
    hosts_raw = data.get("allowed_hosts")
    if not isinstance(hosts_raw, list) or not hosts_raw or len(hosts_raw) > 50:
        raise ScenarioError("allowed_hosts must list 1-50 host names")
    hosts = []
    for index, host in enumerate(hosts_raw):
        if not isinstance(host, str) or not _HOST.fullmatch(host.lower()):
            raise ScenarioError(f"allowed_hosts[{index}] must be a host name")
        hosts.append(host.lower())
    allowed = tuple(hosts)
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, Mapping):
        raise ScenarioError("defaults must be an object")
    _only(defaults, set(DEFAULTS), "defaults")
    deploy_raw = data.get("deploy") or {}
    if not isinstance(deploy_raw, Mapping):
        raise ScenarioError("deploy must be an object")
    _only(deploy_raw, set(DEPLOY_DEFAULTS), "deploy")
    deploy = {**DEPLOY_DEFAULTS, **deploy_raw}
    deploy = {
        "min_requests": _int(
            deploy["min_requests"], "deploy.min_requests", 1, 1_000_000
        ),
        "error_ratio_factor": _number(
            deploy["error_ratio_factor"], "deploy.error_ratio_factor", 1, 100
        ),
        "error_ratio_min_increase": _number(
            deploy["error_ratio_min_increase"], "deploy.error_ratio_min_increase", 0, 1
        ),
        "min_new_5xx": _int(deploy["min_new_5xx"], "deploy.min_new_5xx", 1, 1_000_000),
        "latency_p95_factor": _number(
            deploy["latency_p95_factor"], "deploy.latency_p95_factor", 1, 100
        ),
        "latency_p95_min_increase_ms": _int(
            deploy["latency_p95_min_increase_ms"],
            "deploy.latency_p95_min_increase_ms",
            0,
            600_000,
        ),
    }
    rows = data.get("scenarios")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_SCENARIOS:
        raise ScenarioError(f"scenarios must contain 1-{MAX_SCENARIOS} scenarios")
    scenarios = tuple(
        _scenario(row, index, allowed, defaults) for index, row in enumerate(rows)
    )
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ScenarioError("scenario ids must be unique")
    control = data.get("control_url")
    control_url = ""
    if control is not None:
        # The control is outside the target on purpose: it tells a checker
        # network outage apart from a site outage. It must still be https.
        control_url = _text(control, "control_url")
        parts = urlsplit(control_url)
        if parts.scheme != "https" or not parts.hostname or parts.username:
            raise ScenarioError("control_url must be an https URL without credentials")
    budget = _int(data.get("run_budget_seconds", 240), "run_budget_seconds", 10, 1500)
    normalized = {
        "allowed_hosts": allowed,
        "control_url": control_url,
        "deploy": deploy,
        "scenarios": [scenario.version for scenario in scenarios],
        "run_budget_seconds": budget,
    }
    return CheckSuite(
        version=_digest(normalized),
        allowed_hosts=allowed,
        scenarios=scenarios,
        control_url=control_url,
        maintenance=_maintenance(data.get("maintenance"), set(ids)),
        run_budget_seconds=budget,
        deploy=deploy,
    )


def load_suite(path: str | Path) -> CheckSuite | None:
    """Read and validate the scenario file; ``None`` when it does not exist."""
    file = Path(path)
    if not file.exists():
        return None
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioError(
            f"The service check file cannot be read: {type(exc).__name__}"
        ) from exc
    if len(text) > 512_000:
        raise ScenarioError("The service check file is larger than 512 KB")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScenarioError(
            f"The service check file is not valid JSON (line {exc.lineno})"
        ) from exc
    return parse_suite(data)
