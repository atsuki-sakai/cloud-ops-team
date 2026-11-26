"""Narrow error-watch evidence to the problems before a model reads it.

The collectors return every service, scheduled job, database and request
series in the target. An error watch only needs the parts that show a
problem or that the error groups point at, so these filters drop healthy
rows and configuration details first. Each narrowed result says how many rows
it kept and omitted, so an omission is never read as missing coverage.

A filter receives the raw collector result and the checks collected before
it (in ``collection_steps`` order). If the result has an unexpected shape it
is returned unchanged; if the error groups are unusable, nothing is dropped
by service so a failed source cannot hide a problem.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Status fields that can explain an error; scaling, env names, labels and
# other configuration belong to the infrastructure review.
_SERVICE_FIELDS = (
    "name",
    "ready",
    "ready_message",
    "latest_ready_revision",
    "latest_created_revision",
    "update_time",
    "images",
)
_HEALTHY_DATABASE_STATES = frozenset({"RUNNABLE"})


def _ok_rows(result: Any, key: str) -> list[dict[str, Any]] | None:
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    rows = result.get(key)
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, dict)]


def _narrowed(
    result: dict[str, Any],
    key: str,
    kept: list[dict[str, Any]],
    total: int,
    rule: str,
) -> dict[str, Any]:
    return {
        **result,
        key: kept,
        "count": len(kept),
        "focus": {"rule": rule, "kept": len(kept), "omitted": total - len(kept)},
    }


def affected_services(checks: Mapping[str, Any]) -> set[str] | None:
    """Services named by the error groups or with 5xx; ``None`` when unknown."""
    summary = checks.get("summarize_error_groups")
    groups = _ok_rows(summary, "groups")
    # A truncated summary may omit the groups of an affected service.
    if (
        groups is None
        or not isinstance(summary, dict)
        or summary.get("truncated")
        or summary.get("complete") is False
    ):
        return None
    names = {str(group["service"]) for group in groups if group.get("service")}
    by_service = summary.get("by_service")
    if isinstance(by_service, dict):
        names.update(str(name) for name in by_service)
    for row in _ok_rows(checks.get("get_request_error_rates"), "services") or []:
        if row.get("service"):
            names.add(str(row["service"]))
    return names


def error_rates(result: Any, _checks: Mapping[str, Any]) -> Any:
    """Keep only services that returned 5xx responses in the window."""
    rows = _ok_rows(result, "services")
    if rows is None:
        return result
    kept = [row for row in rows if (row.get("5xx") or 0) > 0]
    return _narrowed(result, "services", kept, len(rows), "5xx > 0")


def failing_jobs(result: Any, _checks: Mapping[str, Any]) -> Any:
    """Keep only scheduled jobs whose last attempt failed."""
    rows = _ok_rows(result, "jobs")
    if rows is None:
        return result
    kept = [row for row in rows if row.get("failing")]
    return _narrowed(result, "jobs", kept, len(rows), "last attempt failed")


def problem_services(result: Any, checks: Mapping[str, Any]) -> Any:
    """Keep not-ready or error-affected services, with status fields only."""
    rows = _ok_rows(result, "services")
    if rows is None:
        return result
    affected = affected_services(checks)
    kept = [
        {field: row.get(field) for field in _SERVICE_FIELDS}
        for row in rows
        if affected is None or row.get("ready") is False or row.get("name") in affected
    ]
    rule = (
        "not ready or named by an error group / 5xx (status fields only)"
        if affected is not None
        else "error groups unavailable: all services kept (status fields only)"
    )
    return _narrowed(result, "services", kept, len(rows), rule)


def _database_named(name: str, affected: set[str]) -> bool:
    # Cloud SQL logs label the instance as ``<project>:<instance>``.
    return any(item == name or item.endswith(f":{name}") for item in affected)


def problem_databases(result: Any, checks: Mapping[str, Any]) -> Any:
    """Keep databases that are not running or that the error groups name."""
    rows = _ok_rows(result, "instances")
    if rows is None:
        return result
    affected = affected_services(checks)
    if affected is None:
        return result
    kept = [
        row
        for row in rows
        if row.get("state") not in _HEALTHY_DATABASE_STATES
        or _database_named(str(row.get("name") or ""), affected)
    ]
    return _narrowed(
        result, "instances", kept, len(rows), "not running or named by an error group"
    )
