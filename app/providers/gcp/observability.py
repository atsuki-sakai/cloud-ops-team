"""Google Cloud observability (read-only): Cloud Logging, Cloud Run,
Cloud Scheduler and Cloud Monitoring. Wrapped by ``GcpProvider``."""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from typing import Any

from app.config import CONFIG
from app.core.fingerprint import fingerprint, message_template, search_text_from
from app.core.redact import redact
from app.core.timeutil import iso_utc, parse_time, utcnow
from app.providers.gcp.client import gcp_list, project_path

LOGGING = "https://logging.googleapis.com/v2"
RUN = "https://run.googleapis.com/v2"
SCHEDULER = "https://cloudscheduler.googleapis.com/v1"
MONITORING = "https://monitoring.googleapis.com/v3"

_SERVICE_LABELS = (
    "service_name",
    "job_id",
    "database_id",
    "function_name",
    "workflow_id",
    "queue_id",
    "instance_id",
    "cluster_name",
    "topic_id",
    "subscription_id",
    "bucket_name",
)
_MSG_KEYS = (
    "message",
    "msg",
    "error",
    "err",
    "exception",
    "description",
    "debugInfo",
    "text",
    "reason",
    "status",
)


def _search_text_for(entry: dict[str, Any], raw_message: str) -> str:
    return search_text_from(
        (entry.get("httpRequest") or {}).get("requestUrl"), raw_message
    )


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _service_of(entry: dict[str, Any]) -> str:
    labels = (entry.get("resource") or {}).get("labels") or {}
    for key in _SERVICE_LABELS:
        if labels.get(key):
            return str(labels[key])
    return str((entry.get("resource") or {}).get("type") or "unknown")


def _message_of(entry: dict[str, Any]) -> str:
    text = entry.get("textPayload")
    if text:
        return str(text)
    payload = entry.get("jsonPayload")
    if isinstance(payload, dict):
        type_url = str(payload.get("@type", ""))
        if "scheduler" in type_url:
            parts = [
                f"job={str(payload.get('jobName', '')).rsplit('/', 1)[-1]}",
                f"status={payload.get('status', '')}",
                str(payload.get("debugInfo") or ""),
            ]
            return " ".join(p for p in parts if p).strip()
        for key in _MSG_KEYS:
            value = payload.get(key)
            if value:
                return (
                    value
                    if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False)[:500]
                )
        return json.dumps(payload, ensure_ascii=False)[:500]
    proto = entry.get("protoPayload")
    if isinstance(proto, dict):
        status = proto.get("status") or {}
        if status.get("message"):
            return f"{proto.get('methodName', '')} {status.get('message')}".strip()
        return str(
            proto.get("methodName") or json.dumps(proto, ensure_ascii=False)[:300]
        )
    http = entry.get("httpRequest")
    if isinstance(http, dict):
        return f"HTTP {http.get('status')} {http.get('requestMethod')} {http.get('requestUrl', '')}"
    return "(no message)"


def _compact(entry: dict[str, Any]) -> dict[str, Any]:
    labels = (entry.get("resource") or {}).get("labels") or {}
    http = entry.get("httpRequest") or {}
    return {
        "timestamp": entry.get("timestamp"),
        "severity": entry.get("severity"),
        "resource_type": (entry.get("resource") or {}).get("type"),
        "service": _service_of(entry),
        "revision": labels.get("revision_name"),
        "message": redact(_message_of(entry))[:400],
        "http_status": http.get("status"),
        "trace": entry.get("trace"),
        "insert_id": entry.get("insertId"),
        "log_name": str(entry.get("logName", "")).rsplit("/", 1)[-1],
    }


def _list_entries(
    filter_: str, *, page_size: int, order: str, max_pages: int
) -> tuple[bool, list[dict[str, Any]] | dict[str, Any]]:
    body = {
        "resourceNames": [project_path()],
        "filter": filter_,
        "orderBy": order,
        "pageSize": page_size,
    }
    return gcp_list(
        f"{LOGGING}/entries:list",
        "entries",
        method="POST",
        json=body,
        max_pages=max_pages,
    )


def _service_clause(service_name: str) -> str:
    if not service_name:
        return ""
    name = _escape(service_name)
    return (
        f'(resource.labels.service_name="{name}" OR resource.labels.job_id="{name}" '
        f'OR resource.labels.database_id="{name}" OR resource.labels.workflow_id="{name}")'
    )


# ---------------------------------------------------------------------------
# Cloud Logging
# ---------------------------------------------------------------------------


def summarize_error_groups(hours: int) -> dict:
    """Group ERROR-and-above log entries from the last ``hours`` hours.

    Call this first in an error-watch run. Each group carries a stable
    ``fingerprint`` (used for issue de-duplication), counts, first/last seen
    timestamps, a redacted sample message, affected services/revisions and
    a ``search_text`` usable with ``count_matching_errors``.

    Args:
        hours: Look-back window in hours (1-168).

    Returns:
        dict with ``status``, ``window``, ``total_entries``, ``truncated``,
        ``by_service`` and ``groups`` (sorted by count, max 30).
    """
    hours = max(1, min(int(hours), 168))
    end = utcnow()
    start = end - dt.timedelta(hours=hours)
    filter_ = f'severity>=ERROR AND timestamp>="{iso_utc(start)}" AND timestamp<="{iso_utc(end)}"'
    ok, entries = _list_entries(
        filter_, page_size=1000, order="timestamp desc", max_pages=3
    )
    if not ok:
        return {"status": entries.get("status", "error"), "error": entries}
    assert isinstance(entries, list)

    groups: dict[str, dict[str, Any]] = {}
    by_service: dict[str, int] = defaultdict(int)
    for entry in entries:
        compact = _compact(entry)
        raw_message = _message_of(entry)
        template = message_template(raw_message)
        key = fingerprint(compact["resource_type"] or "", compact["service"], template)
        by_service[compact["service"]] += 1
        group = groups.get(key)
        ts = compact["timestamp"]
        if group is None:
            group = groups[key] = {
                "fingerprint": key,
                "resource_type": compact["resource_type"],
                "service": compact["service"],
                "template": template,
                "count": 0,
                "first_seen": ts,
                "last_seen": ts,
                "severities": set(),
                "revisions": set(),
                "sample_message": compact["message"],
                "sample_insert_ids": [],
                "sample_traces": [],
                "search_text": _search_text_for(entry, raw_message),
                "log_name": compact["log_name"],
            }
        group["count"] = int(group["count"]) + 1
        if ts and (group["first_seen"] is None or ts < group["first_seen"]):
            group["first_seen"] = ts
        if ts and (group["last_seen"] is None or ts > group["last_seen"]):
            group["last_seen"] = ts
        group["severities"].add(compact["severity"])
        if compact["revision"]:
            group["revisions"].add(compact["revision"])
        if compact["insert_id"] and len(group["sample_insert_ids"]) < 3:
            group["sample_insert_ids"].append(compact["insert_id"])
        if compact["trace"] and len(group["sample_traces"]) < 3:
            group["sample_traces"].append(compact["trace"])

    ordered = sorted(groups.values(), key=lambda g: g["count"], reverse=True)[:30]
    for group in ordered:
        group["severities"] = sorted(s for s in group["severities"] if s)
        group["revisions"] = sorted(group["revisions"])[:5]
    return {
        "status": "ok",
        "target": CONFIG.target,
        "window": {"start": iso_utc(start), "end": iso_utc(end), "hours": hours},
        "total_entries": len(entries),
        "truncated": len(entries) >= 3000,
        "group_count": len(groups),
        "by_service": dict(
            sorted(by_service.items(), key=lambda kv: kv[1], reverse=True)
        ),
        "groups": ordered,
    }


def list_error_logs(hours: int, service_name: str, limit: int) -> dict:
    """List individual ERROR-and-above log entries (newest first).

    Args:
        hours: Look-back window in hours (1-168).
        service_name: Cloud Run service / scheduler job / Cloud SQL database id
            to filter on. Pass an empty string for all services.
        limit: Maximum entries to return (1-200).
    """
    hours = max(1, min(int(hours), 168))
    limit = max(1, min(int(limit), 200))
    start = utcnow() - dt.timedelta(hours=hours)
    clauses = [f'severity>=ERROR AND timestamp>="{iso_utc(start)}"']
    if service_name:
        clauses.append(_service_clause(service_name))
    ok, entries = _list_entries(
        " AND ".join(clauses), page_size=limit, order="timestamp desc", max_pages=1
    )
    if not ok:
        return {"status": entries.get("status", "error"), "error": entries}
    assert isinstance(entries, list)
    return {
        "status": "ok",
        "count": len(entries),
        "entries": [_compact(e) for e in entries],
    }


def get_log_context(
    service_name: str, around_time: str, window_minutes: int, trace: str
) -> dict:
    """Fetch surrounding log lines (all severities) to investigate an error.

    Either pass a ``trace`` id (preferred when available: all entries of that
    request are returned) or a ``service_name`` plus ``around_time``.

    Args:
        service_name: Service / job / database id. Empty string if using trace.
        around_time: ISO-8601 timestamp (e.g. ``2026-10-08T07:45:00Z``) at the
            centre of the window. Empty string if using trace.
        window_minutes: Minutes before and after ``around_time`` (1-120).
        trace: Trace id or full ``projects/P/traces/ID`` name. Empty string if unused.
    """
    clauses: list[str] = []
    if trace:
        trace_name = (
            trace
            if trace.startswith("projects/")
            else f"{project_path()}/traces/{trace}"
        )
        clauses.append(f'trace="{_escape(trace_name)}"')
        start = utcnow() - dt.timedelta(days=7)
        clauses.append(f'timestamp>="{iso_utc(start)}"')
    else:
        centre = parse_time(around_time) or utcnow()
        window = max(1, min(int(window_minutes), 120))
        clauses.append(f'timestamp>="{iso_utc(centre - dt.timedelta(minutes=window))}"')
        clauses.append(f'timestamp<="{iso_utc(centre + dt.timedelta(minutes=window))}"')
        if service_name:
            clauses.append(_service_clause(service_name))
    ok, entries = _list_entries(
        " AND ".join(clauses), page_size=80, order="timestamp asc", max_pages=1
    )
    if not ok:
        return {"status": entries.get("status", "error"), "error": entries}
    assert isinstance(entries, list)
    return {
        "status": "ok",
        "count": len(entries),
        "entries": [_compact(e) for e in entries],
    }


def count_matching_errors(service_name: str, search_text: str, since: str) -> dict:
    """Count ERROR-and-above entries containing ``search_text`` since a time.

    Used to confirm a fix: if a PR fixing an error was merged at time T, call
    with ``since=T`` and the issue's ``search_text`` to see whether it recurred.

    Args:
        service_name: Service / job / database id (empty string for all).
        search_text: Literal text to search for across all log fields.
        since: ISO-8601 timestamp to count from.
    """
    start = parse_time(since) or (utcnow() - dt.timedelta(days=7))
    clauses = [f'severity>=ERROR AND timestamp>="{iso_utc(start)}"']
    if service_name:
        clauses.append(_service_clause(service_name))
    if search_text:
        literal = _escape(search_text)
        clauses.append(f'("{literal}" OR httpRequest.requestUrl:"{literal}")')
    ok, entries = _list_entries(
        " AND ".join(clauses), page_size=1000, order="timestamp desc", max_pages=2
    )
    if not ok:
        return {"status": entries.get("status", "error"), "error": entries}
    assert isinstance(entries, list)
    timestamps = [e.get("timestamp") for e in entries if e.get("timestamp")]
    return {
        "status": "ok",
        "since": iso_utc(start),
        "count": len(entries),
        "capped": len(entries) >= 2000,
        "first_seen": min(timestamps) if timestamps else None,
        "last_seen": max(timestamps) if timestamps else None,
        "latest_samples": [_compact(e) for e in entries[:3]],
    }


# ---------------------------------------------------------------------------
# Cloud Run
# ---------------------------------------------------------------------------


def _run_parent() -> str:
    return f"{RUN}/projects/{CONFIG.target}/locations/{CONFIG.region}"


def _run_services_raw() -> tuple[bool, list[dict[str, Any]] | dict[str, Any]]:
    return gcp_list(f"{_run_parent()}/services", "services", params={"pageSize": 100})


def _short(name: str | None) -> str | None:
    return name.rsplit("/", 1)[-1] if name else name


def compact_service(svc: dict[str, Any]) -> dict[str, Any]:
    template = svc.get("template") or {}
    containers = template.get("containers") or []
    env_plain: list[str] = []
    env_secret: list[str] = []
    for container in containers:
        for env in container.get("env") or []:
            (env_secret if env.get("valueSource") else env_plain).append(
                env.get("name", "")
            )
    terminal = svc.get("terminalCondition") or {}
    return {
        "name": _short(svc.get("name")),
        "uri": svc.get("uri"),
        "ingress": svc.get("ingress"),
        "ready": terminal.get("state") == "CONDITION_SUCCEEDED",
        "ready_message": terminal.get("message"),
        "latest_ready_revision": _short(svc.get("latestReadyRevision")),
        "latest_created_revision": _short(svc.get("latestCreatedRevision")),
        "update_time": svc.get("updateTime"),
        "last_modifier": redact(str(svc.get("lastModifier") or "")),
        "service_account": template.get("serviceAccount"),
        "images": [c.get("image") for c in containers],
        "scaling": template.get("scaling") or {},
        "vpc_egress": (template.get("vpcAccess") or {}).get("egress"),
        "execution_environment": template.get("executionEnvironment"),
        "timeout": template.get("timeout"),
        "env_plain_names": env_plain,
        "env_secret_names": env_secret,
        "invoker_iam_disabled": bool(svc.get("invokerIamDisabled")),
        "labels": svc.get("labels") or {},
    }


def get_cloud_run_services() -> dict:
    """List Cloud Run services in the target project/region with health info.

    Returns readiness, latest revisions, images, service account, scaling,
    env var *names* (never values) and last deploy time per service.
    """
    ok, services = _run_services_raw()
    if not ok:
        return {"status": services.get("status", "error"), "error": services}
    assert isinstance(services, list)
    return {
        "status": "ok",
        "target": CONFIG.target,
        "region": CONFIG.region,
        "count": len(services),
        "services": [compact_service(s) for s in services],
    }


def get_cloud_run_revisions(service_name: str, limit: int) -> dict:
    """List recent revisions of a Cloud Run service (newest first).

    Useful to correlate an error spike with a deployment.

    Args:
        service_name: Cloud Run service name (e.g. ``api``).
        limit: Max revisions to return (1-20).
    """
    limit = max(1, min(int(limit), 20))
    ok, revisions = gcp_list(
        f"{_run_parent()}/services/{service_name}/revisions",
        "revisions",
        params={"pageSize": 50},
        max_pages=1,
    )
    if not ok:
        return {"status": revisions.get("status", "error"), "error": revisions}
    assert isinstance(revisions, list)
    revisions.sort(key=lambda r: r.get("createTime", ""), reverse=True)
    out = []
    for rev in revisions[:limit]:
        ready = next(
            (c for c in rev.get("conditions") or [] if c.get("type") == "Ready"), {}
        )
        out.append(
            {
                "name": _short(rev.get("name")),
                "create_time": rev.get("createTime"),
                "images": [c.get("image") for c in rev.get("containers") or []],
                "image_digests": [
                    c.get("imageDigest") for c in rev.get("containerStatuses") or []
                ],
                "ready": ready.get("state") == "CONDITION_SUCCEEDED",
                "ready_message": ready.get("message"),
                "scaling": rev.get("scaling") or {},
                "service_account": rev.get("serviceAccount"),
            }
        )
    return {
        "status": "ok",
        "service": service_name,
        "count": len(out),
        "revisions": out,
    }


# ---------------------------------------------------------------------------
# Cloud Scheduler
# ---------------------------------------------------------------------------


def get_scheduler_jobs() -> dict:
    """List Cloud Scheduler jobs with their last attempt status.

    Jobs whose last attempt failed are flagged with ``failing: true``; these
    often explain ``cloud_scheduler_job`` error log entries.
    """
    parent = f"{SCHEDULER}/projects/{CONFIG.target}/locations/{CONFIG.region}"
    ok, jobs = gcp_list(f"{parent}/jobs", "jobs", params={"pageSize": 100})
    if not ok:
        return {"status": jobs.get("status", "error"), "error": jobs}
    assert isinstance(jobs, list)
    out = []
    for job in jobs:
        status = job.get("status") or {}
        http = job.get("httpTarget") or {}
        uri = str(http.get("uri") or "").split("?", 1)[0]
        out.append(
            {
                "name": _short(job.get("name")),
                "schedule": job.get("schedule"),
                "time_zone": job.get("timeZone"),
                "state": job.get("state"),
                "last_attempt_time": job.get("lastAttemptTime"),
                "next_schedule_time": job.get("scheduleTime"),
                "last_status_code": status.get("code", 0),
                "last_status_message": redact(str(status.get("message") or "")),
                "failing": bool(status.get("code")),
                "target": uri or (job.get("pubsubTarget") or {}).get("topicName"),
                "http_method": http.get("httpMethod"),
                "attempt_deadline": job.get("attemptDeadline"),
                "retry_count": (job.get("retryConfig") or {}).get("retryCount"),
            }
        )
    failing = [j["name"] for j in out if j["failing"]]
    return {"status": "ok", "count": len(out), "failing_jobs": failing, "jobs": out}


# ---------------------------------------------------------------------------
# Cloud Monitoring
# ---------------------------------------------------------------------------


def get_request_error_rates(hours: int) -> dict:
    """Per-service Cloud Run request totals and 5xx/4xx ratios (Cloud Monitoring).

    Args:
        hours: Window in hours (1-168).
    """
    hours = max(1, min(int(hours), 168))
    end = utcnow()
    start = end - dt.timedelta(hours=hours)
    params = {
        "filter": 'metric.type="run.googleapis.com/request_count" AND resource.type="cloud_run_revision"',
        "interval.startTime": iso_utc(start),
        "interval.endTime": iso_utc(end),
        "aggregation.alignmentPeriod": f"{hours * 3600}s",
        "aggregation.perSeriesAligner": "ALIGN_SUM",
        "aggregation.crossSeriesReducer": "REDUCE_SUM",
        "aggregation.groupByFields": [
            "resource.labels.service_name",
            "metric.labels.response_code_class",
        ],
        "view": "FULL",
        "pageSize": 500,
    }
    ok, series = gcp_list(
        f"{MONITORING}/{project_path()}/timeSeries", "timeSeries", params=params
    )
    if not ok:
        return {"status": series.get("status", "error"), "error": series}
    assert isinstance(series, list)
    per_service: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for ts in series:
        service = ((ts.get("resource") or {}).get("labels") or {}).get(
            "service_name", "unknown"
        )
        klass = ((ts.get("metric") or {}).get("labels") or {}).get(
            "response_code_class", "unknown"
        )
        total = 0
        for point in ts.get("points") or []:
            value = point.get("value") or {}
            total += int(value.get("int64Value") or 0) + int(
                float(value.get("doubleValue") or 0)
            )
        per_service[service][klass] += total
    rows = []
    for service, classes in per_service.items():
        total = sum(classes.values())
        five = classes.get("5xx", 0)
        four = classes.get("4xx", 0)
        rows.append(
            {
                "service": service,
                "requests": total,
                "5xx": five,
                "4xx": four,
                "5xx_ratio": round(five / total, 4) if total else 0.0,
                "4xx_ratio": round(four / total, 4) if total else 0.0,
            }
        )
    rows.sort(key=lambda r: r["5xx"], reverse=True)
    return {"status": "ok", "window_hours": hours, "services": rows}


def get_alerting_setup() -> dict:
    """List Cloud Monitoring alert policies and uptime checks.

    Lets the team judge whether an error class is already covered by an
    alert, and whether critical services have uptime checks.
    """
    ok, policies = gcp_list(
        f"{MONITORING}/{project_path()}/alertPolicies",
        "alertPolicies",
        params={"pageSize": 100},
    )
    if not ok:
        return {"status": policies.get("status", "error"), "error": policies}
    assert isinstance(policies, list)
    ok2, uptime = gcp_list(
        f"{MONITORING}/{project_path()}/uptimeCheckConfigs",
        "uptimeCheckConfigs",
        params={"pageSize": 100},
    )
    policy_rows = []
    for pol in policies:
        enabled = pol.get("enabled")
        if isinstance(enabled, dict):
            enabled = enabled.get("value", False)
        policy_rows.append(
            {
                "display_name": pol.get("displayName"),
                "enabled": bool(enabled) if enabled is not None else True,
                "conditions": [
                    c.get("displayName") for c in pol.get("conditions") or []
                ],
                "notification_channels": len(pol.get("notificationChannels") or []),
                "severity": pol.get("severity"),
            }
        )
    uptime_rows = []
    if ok2 and isinstance(uptime, list):
        for cfg in uptime:
            labels = (cfg.get("monitoredResource") or {}).get("labels") or {}
            uptime_rows.append(
                {
                    "display_name": cfg.get("displayName"),
                    "host": labels.get("host"),
                    "path": (cfg.get("httpCheck") or {}).get("path"),
                    "period": cfg.get("period"),
                }
            )
    return {
        "status": "ok",
        "alert_policy_count": len(policy_rows),
        "alert_policies": policy_rows,
        "uptime_check_count": len(uptime_rows),
        "uptime_checks": uptime_rows,
        "uptime_error": None if ok2 else uptime,
    }
