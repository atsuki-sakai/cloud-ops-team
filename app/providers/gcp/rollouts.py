"""Cloud Run rollouts with their serving state and per-revision metrics (read-only).

A revision counts as deployed only by the cloud's own serving state: Ready,
the traffic it receives in ``trafficStatuses`` and the tagged URLs that reach
it. Metrics are grouped by ``revision_name`` so the new revision's requests are
compared with the previous revisions' requests, not with a blend of both.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.config import CONFIG
from app.core.payloads import Payload, clamp_int
from app.core.timeutil import iso_utc, parse_time, utcnow
from app.providers.gcp.client import (
    failure_payload,
    failure_result,
    gcp_get,
    gcp_list,
    items_payload,
    project_path,
)
from app.providers.gcp.observability import MONITORING, series_label
from app.providers.gcp.run import (
    is_ready,
    ready_condition,
    revisions_url,
    services_url,
    short_name,
)

MAX_SERVICES = 10
MIN_BASELINE = dt.timedelta(minutes=15)
MAX_BASELINE = dt.timedelta(minutes=60)


def _ready(resource: dict[str, Any]) -> tuple[bool, str]:
    condition = ready_condition(resource)
    return is_ready(condition), str(condition.get("message") or "")[:300]


def _series(
    metric: str, service: str, start: dt.datetime, end: dt.datetime, *, latency: bool
) -> tuple[bool, Payload]:
    seconds = max(60, int((end - start).total_seconds()))
    params: dict[str, Any] = {
        "filter": (
            f'metric.type="run.googleapis.com/{metric}" '
            'AND resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{service}"'
        ),
        "interval.startTime": iso_utc(start),
        "interval.endTime": iso_utc(end),
        "aggregation.alignmentPeriod": f"{seconds}s",
        "aggregation.perSeriesAligner": "ALIGN_DELTA" if latency else "ALIGN_SUM",
        "aggregation.crossSeriesReducer": (
            "REDUCE_PERCENTILE_95" if latency else "REDUCE_SUM"
        ),
        "aggregation.groupByFields": (
            ["resource.labels.revision_name"]
            if latency
            else ["resource.labels.revision_name", "metric.labels.response_code_class"]
        ),
        "pageSize": 500,
    }
    return gcp_list(
        f"{MONITORING}/{project_path()}/timeSeries", "timeSeries", params=params
    )


def _value(point: dict[str, Any]) -> float:
    value = point.get("value") or {}
    if "doubleValue" in value:
        return float(value["doubleValue"] or 0)
    return float(value.get("int64Value") or 0)


def _window(
    service: str,
    start: dt.datetime,
    end: dt.datetime,
    *,
    include: set[str] | None,
    exclude: set[str],
) -> dict[str, Any]:
    """Requests, 5xx and p95 for the selected revisions in ``[start, end)``."""
    out: dict[str, Any] = {
        "start": iso_utc(start),
        "end": iso_utc(end),
        "requests": 0,
        "5xx": 0,
        "4xx": 0,
        "p95_ms": None,
        "revisions": [],
    }

    def selected(revision: str) -> bool:
        return (include is None or revision in include) and revision not in exclude

    ok, counts = _series("request_count", service, start, end, latency=False)
    if not ok:
        return {**out, "status": failure_payload(counts).get("status", "error")}
    revisions: set[str] = set()
    for series in items_payload(counts):
        revision = series_label(series, "resource", "revision_name")
        if not selected(revision):
            continue
        revisions.add(revision)
        klass = series_label(series, "metric", "response_code_class")
        total = int(sum(_value(point) for point in series.get("points") or []))
        out["requests"] += total
        if klass in ("5xx", "4xx"):
            out[klass] += total
    ok, latencies = _series("request_latencies", service, start, end, latency=True)
    if ok:
        values = [
            _value(point)
            for series in items_payload(latencies)
            if selected(series_label(series, "resource", "revision_name"))
            for point in series.get("points") or []
        ]
        # Several points can come back when the window crosses an alignment
        # boundary; the highest one is reported rather than an invented mean.
        out["p95_ms"] = round(max(values), 1) if values else None
    else:
        out["latency_status"] = failure_payload(latencies).get("status", "error")
    out["revisions"] = sorted(revisions)
    out["status"] = "ok"
    return out


def _metrics(
    service: str, revision: str, created: dt.datetime, now: dt.datetime
) -> dict[str, Any]:
    after_len = now - created
    baseline = min(max(after_len, MIN_BASELINE), MAX_BASELINE)
    after = _window(service, created, now, include={revision}, exclude=set())
    before = _window(
        service, created - baseline, created, include=None, exclude={revision}
    )
    status = "ok" if after["status"] == "ok" and before["status"] == "ok" else "partial"
    return {
        "status": status,
        "before": before,
        "after": after,
        "note": "Cloud Monitoring data can arrive a few minutes late; a very recent rollout has few samples.",
    }


def list_recent_rollouts(minutes: int) -> dict[str, Any]:
    """Cloud Run revisions created in the last ``minutes`` minutes with serving state."""
    minutes = clamp_int(minutes, 5, 1440)
    now = utcnow()
    since = now - dt.timedelta(minutes=minutes)
    ok, services = gcp_list(services_url(), "services", params={"pageSize": 100})
    if not ok:
        return failure_result(services)
    services = items_payload(services)
    recent = [
        svc
        for svc in services
        if (
            parse_time(svc.get("updateTime") or "")
            or dt.datetime.min.replace(tzinfo=dt.UTC)
        )
        >= since
    ]
    recent.sort(key=lambda svc: svc.get("updateTime") or "", reverse=True)
    truncated = len(recent) > MAX_SERVICES
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for svc in recent[:MAX_SERVICES]:
        name = short_name(svc.get("name")) or ""
        traffic = [
            {
                "revision": short_name(row.get("revision")) or "",
                "percent": int(row.get("percent") or 0),
                "tag": row.get("tag") or "",
                "uri": row.get("uri") or "",
                "type": row.get("type") or "",
            }
            for row in svc.get("trafficStatuses") or []
        ]
        # ast-grep-ignore: api-call-in-loop -- bounded: at most MAX_SERVICES (10) services; revisions are listed per service
        ok, payload = gcp_get(revisions_url(name), params={"pageSize": 20})
        if not ok:
            errors.append({"service": name, "status": payload.get("status", "error")})
            continue
        revisions = [
            rev
            for rev in payload.get("revisions") or []
            if (
                parse_time(rev.get("createTime") or "")
                or since - dt.timedelta(seconds=1)
            )
            >= since
        ]
        revisions.sort(key=lambda rev: rev.get("createTime") or "", reverse=True)
        for index, rev in enumerate(revisions):
            revision = short_name(rev.get("name")) or ""
            created = parse_time(rev.get("createTime") or "") or now
            ready, message = _ready(rev)
            row = {
                "service": name,
                "service_uri": svc.get("uri") or "",
                "revision": revision,
                "create_time": iso_utc(created),
                "ready": ready,
                "ready_message": message,
                "latest_ready_revision": short_name(svc.get("latestReadyRevision"))
                or "",
                "latest_created_revision": short_name(svc.get("latestCreatedRevision"))
                or "",
                "traffic": traffic,
                "superseded": index > 0,
            }
            if index == 0:
                # ast-grep-ignore: api-call-in-loop -- bounded: newest revision only, at most MAX_SERVICES (10) services
                row["metrics"] = _metrics(name, revision, created, now)
            rows.append(row)
    return {
        "status": "ok" if not errors else "partial",
        "complete": not errors and not truncated,
        "target": CONFIG.target,
        "region": CONFIG.region,
        "window_minutes": minutes,
        "services_checked": min(len(recent), MAX_SERVICES),
        "rollouts": rows,
        "errors": errors,
        "truncated": truncated,
    }
