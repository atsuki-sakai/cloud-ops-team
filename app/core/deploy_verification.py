"""Judge a rollout from the revision actually serving traffic, scenarios and metrics.

A merged pull request or a finished build is not a deployment. A rollout is
verified only when the new revision is ready, receives the production traffic
the scenarios exercised, the scenarios pass, and its own request metrics do not
regress against the previous revisions. Too few requests leave the metric
comparison unevaluated instead of passing it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

VERDICTS = (
    "verified",
    "failed",
    "regressed",
    "not_ready",
    "not_serving",
    "unverified",
    "superseded",
)
NEEDS_ANALYSIS = frozenset(
    {"failed", "regressed", "not_ready", "not_serving", "unverified"}
)


def _ratio(errors: int, total: int) -> float:
    return round(errors / total, 4) if total else 0.0


def evaluate_metrics(metrics: Mapping[str, Any] | None, cfg: Mapping[str, Any]) -> dict:
    """Compare the new revision (after) with the previous revisions (before)."""
    if not isinstance(metrics, Mapping) or metrics.get("status") != "ok":
        return {
            "verdict": "unavailable",
            "reason": (metrics or {}).get("status", "missing")
            if isinstance(metrics, Mapping)
            else "missing",
        }
    before = metrics.get("before") or {}
    after = metrics.get("after") or {}
    b_total, a_total = int(before.get("requests") or 0), int(after.get("requests") or 0)
    b_5xx, a_5xx = int(before.get("5xx") or 0), int(after.get("5xx") or 0)
    out: dict[str, Any] = {
        "before": {
            "start": before.get("start"),
            "end": before.get("end"),
            "requests": b_total,
            "5xx": b_5xx,
            "5xx_ratio": _ratio(b_5xx, b_total),
            "p95_ms": before.get("p95_ms"),
        },
        "after": {
            "start": after.get("start"),
            "end": after.get("end"),
            "requests": a_total,
            "5xx": a_5xx,
            "5xx_ratio": _ratio(a_5xx, a_total),
            "p95_ms": after.get("p95_ms"),
        },
        "min_requests": cfg["min_requests"],
    }
    if a_total < cfg["min_requests"] or b_total < cfg["min_requests"]:
        out["verdict"] = "insufficient_samples"
        out["reason"] = (
            "new revision has too few requests"
            if a_total < cfg["min_requests"]
            else "baseline has too few requests"
        )
        return out
    ratio_b, ratio_a = out["before"]["5xx_ratio"], out["after"]["5xx_ratio"]
    threshold = max(
        ratio_b * cfg["error_ratio_factor"], ratio_b + cfg["error_ratio_min_increase"]
    )
    regressions = []
    if a_5xx >= cfg["min_new_5xx"] and ratio_a > threshold:
        regressions.append("error_rate")
    p95_b, p95_a = before.get("p95_ms"), after.get("p95_ms")
    if isinstance(p95_b, int | float) and isinstance(p95_a, int | float):
        if (
            p95_a > p95_b * cfg["latency_p95_factor"]
            and p95_a - p95_b >= cfg["latency_p95_min_increase_ms"]
        ):
            regressions.append("latency_p95")
    else:
        out["latency"] = "unevaluated"
    out["error_ratio_threshold"] = round(threshold, 4)
    out["regressions"] = regressions
    out["verdict"] = "regressed" if regressions else "ok"
    return out


def evaluate_rollout(
    rollout: Mapping[str, Any],
    scenario_results: list[Mapping[str, Any]],
    cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine readiness, traffic, scenarios and metrics into one verdict."""
    traffic = [row for row in rollout.get("traffic") or [] if isinstance(row, Mapping)]
    percent = sum(
        int(row.get("percent") or 0)
        for row in traffic
        if row.get("revision") == rollout.get("revision")
    )
    tags = [
        {"tag": row.get("tag"), "uri": row.get("uri")}
        for row in traffic
        if row.get("revision") == rollout.get("revision") and row.get("tag")
    ]
    metrics = evaluate_metrics(rollout.get("metrics"), cfg)
    outcomes = [row.get("outcome") for row in scenario_results]
    reasons: list[str] = []
    if rollout.get("superseded"):
        verdict = "superseded"
        reasons.append("a newer revision of the service was created in the window")
    elif not rollout.get("ready"):
        verdict = "not_ready"
        reasons.append("the revision is not ready")
    elif percent <= 0:
        verdict = "not_serving"
        reasons.append(
            "the revision receives no production traffic"
            + (" (tagged candidate URL only)" if tags else "")
        )
    elif "fail" in outcomes and percent >= 100:
        verdict = "failed"
        reasons.append(
            "a production scenario failed while the revision served all traffic"
        )
    elif metrics["verdict"] == "regressed":
        verdict = "regressed"
        reasons.append(
            "the new revision's own metrics regressed: "
            + ", ".join(metrics["regressions"])
        )
    elif not scenario_results:
        verdict = "unverified"
        reasons.append("no production scenario is registered for this service")
    elif percent < 100:
        verdict = "unverified"
        reasons.append(
            f"traffic is split ({percent}% to the new revision); scenario results cannot be attributed"
        )
    elif any(outcome not in ("pass", "flaky") for outcome in outcomes):
        verdict = "unverified"
        reasons.append("some scenarios were not conclusively checked")
    else:
        verdict = "verified"
        if "flaky" in outcomes:
            reasons.append("a scenario passed only after a retry")
    if verdict == "verified" and metrics["verdict"] != "ok":
        reasons.append(
            "metric comparison unevaluated: "
            + str(metrics.get("reason") or metrics["verdict"])
        )
    return {
        "service": rollout.get("service"),
        "revision": rollout.get("revision"),
        "create_time": rollout.get("create_time"),
        "ready": bool(rollout.get("ready")),
        "traffic_percent": percent,
        "candidate_urls": tags,
        "service_uri": rollout.get("service_uri"),
        "verdict": verdict,
        "reasons": reasons,
        "metrics": metrics,
        "metrics_evaluated": metrics["verdict"] in ("ok", "regressed"),
        "scenarios": [
            {
                key: row.get(key)
                for key in (
                    "id",
                    "version",
                    "outcome",
                    "failure",
                    "failed_step",
                    "attempts_made",
                )
                if row.get(key) is not None
            }
            for row in scenario_results
        ],
        "fingerprint": f"deploy_verification:{rollout.get('service')}:{rollout.get('revision')}",
        "needs_analysis": verdict in NEEDS_ANALYSIS,
    }
