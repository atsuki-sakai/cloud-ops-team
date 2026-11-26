"""Code-owned decisions for service checks and deployment verification.

The collectors already judged each scenario and rollout. This module adds the
state transition against open incidents (first failure, ongoing, recovered),
decides whether a model needs to investigate at all, and renders the fixed
verdict summary that is always part of the final report. A healthy run never
reaches a model, and a failed analysis cannot erase a failed check.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import CONFIG

SERVICE_PREFIX = "service_check:"
NEEDS_ANALYSIS = frozenset(
    {"new_incident", "ongoing", "recovered", "unstable", "unverified", "orphaned"}
)

_LABELS = {
    "ja": {
        "service_title": "稼働確認（固定判定）",
        "deploy_title": "デプロイ後検証（固定判定）",
        "run": "実行 ID",
        "suite": "シナリオ一式の版",
        "control": "検査側の外部疎通",
        "none_configured": "シナリオが未設定のため、稼働確認は実行していません。",
        "unavailable": "収集に失敗したため判定できませんでした",
        "healthy": "全シナリオが期待結果を満たしました。AI による調査は不要と判定しました。",
        "no_rollouts": "対象期間に作成されたリビジョンはありません。AI による調査は不要と判定しました。",
        "all_verified": "全ロールアウトが検証済みです。AI による調査は不要と判定しました。",
        "incident_state": "継続中障害の照合",
        "incident_unknown": "未確認（open ISSUE を取得できませんでした）",
        "analysis_failed": "AI による原因調査は失敗しました。上の判定は有効ですが、原因は未確認です。",
        "scope": "この結果は検査地点・ブラウザ条件・記録した操作に限られ、全利用者の体験を保証しません。",
    },
    "en": {
        "service_title": "Service check (code verdicts)",
        "deploy_title": "Deployment verification (code verdicts)",
        "run": "Run ID",
        "suite": "Scenario set version",
        "control": "Checker network control",
        "none_configured": "No scenarios are configured, so no service check ran.",
        "unavailable": "Collection failed; no verdict is available",
        "healthy": "Every scenario met its expected result. No AI investigation is needed.",
        "no_rollouts": "No revision was created in the window. No AI investigation is needed.",
        "all_verified": "Every rollout is verified. No AI investigation is needed.",
        "incident_state": "Open incident matching",
        "incident_unknown": "unverified (open issues could not be read)",
        "analysis_failed": "The AI investigation failed. The verdicts above stand; the cause is unverified.",
        "scope": "Results cover only the checker location, browser conditions and recorded steps.",
    },
}


def _label(key: str) -> str:
    return _LABELS.get(CONFIG.report_language, _LABELS["en"])[key]


def service_incidents(result: Any) -> Any:
    """Keep only open service-check incidents so other issues cannot crowd them out."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return result
    rows = [
        issue
        for issue in result.get("issues") or []
        if isinstance(issue, dict)
        and str(issue.get("fingerprint") or "").startswith(SERVICE_PREFIX)
        and issue.get("state", "open") == "open"
    ]
    return {**result, "issues": rows, "count": len(rows)}


def _transition(outcome: str, incident: Mapping[str, Any] | None) -> str:
    if outcome == "fail":
        return "ongoing" if incident else "new_incident"
    if outcome == "pass":
        return "recovered" if incident else "healthy"
    if outcome == "flaky":
        return "unstable" if incident else "transient"
    if outcome in ("inconclusive", "checker_error"):
        return "unverified"
    return "not_checked"


def service_check_decision(
    checks: Mapping[str, Any], *, collection_failed: bool = False
) -> dict:
    run = checks.get("run_service_checks") or {}
    incidents = checks.get("list_ops_issues") or {}
    if collection_failed:
        return {
            "analysis_required": True,
            "reason": "collection_failed",
            "transitions": [],
        }
    if run.get("status") == "not_configured":
        return {
            "analysis_required": False,
            "reason": "not_configured",
            "transitions": [],
        }
    scenarios = run.get("scenarios")
    if not isinstance(scenarios, list):
        return {
            "analysis_required": True,
            "reason": "check_result_unavailable",
            "transitions": [],
        }
    incidents_known = incidents.get("status") == "ok"
    open_by_fp = {
        issue.get("fingerprint"): issue
        for issue in incidents.get("issues") or []
        if isinstance(issue, dict)
    }
    transitions = []
    for row in scenarios:
        if not isinstance(row, dict):
            continue
        fingerprint = row.get("fingerprint")
        incident = open_by_fp.get(fingerprint)
        state = _transition(str(row.get("outcome")), incident)
        if not incidents_known and state in ("healthy", "transient"):
            # Without the open-issue list a pass may be a recovery and a flaky
            # run may belong to an ongoing incident.
            state = "unverified" if state == "transient" else state
        item = {
            "scenario": row.get("id"),
            "fingerprint": fingerprint,
            "outcome": row.get("outcome"),
            "transition": state,
            "severity": row.get("severity"),
        }
        if incident:
            item["issue_number"] = incident.get("number")
        transitions.append(item)
    known = {row.get("fingerprint") for row in scenarios if isinstance(row, dict)}
    for fingerprint, issue in open_by_fp.items():
        if fingerprint not in known:
            transitions.append(
                {
                    "scenario": str(fingerprint)[len(SERVICE_PREFIX) :],
                    "fingerprint": fingerprint,
                    "outcome": "not_registered",
                    "transition": "orphaned",
                    "issue_number": issue.get("number"),
                }
            )
    required = any(item["transition"] in NEEDS_ANALYSIS for item in transitions) or (
        not incidents_known and any(item["outcome"] != "pass" for item in transitions)
    )
    return {
        "analysis_required": required,
        "reason": "abnormal_or_recovered" if required else "all_healthy",
        "incidents_known": incidents_known,
        "transitions": transitions,
    }


def deploy_decision(
    checks: Mapping[str, Any], *, collection_failed: bool = False
) -> dict:
    result = checks.get("verify_recent_deployments") or {}
    rollouts = result.get("rollouts")
    if collection_failed or not isinstance(rollouts, list):
        return {"analysis_required": True, "reason": "verification_unavailable"}
    required = any(
        isinstance(row, dict) and row.get("needs_analysis") for row in rollouts
    )
    return {
        "analysis_required": required,
        "reason": "abnormal_rollout"
        if required
        else ("all_verified" if rollouts else "no_rollouts"),
    }


def _service_lines(evidence: Mapping[str, Any]) -> list[str]:
    run = (evidence.get("checks") or {}).get("run_service_checks") or {}
    decision = evidence.get("decision") or {}
    lines = [f"## {_label('service_title')}"]
    if run.get("status") == "not_configured":
        return [*lines, "", _label("none_configured")]
    if not isinstance(run.get("scenarios"), list):
        return [*lines, "", f"{_label('unavailable')}: {run.get('status', 'error')}"]
    control = (run.get("control") or {}).get("status", "not_configured")
    lines += [
        "",
        f"- {_label('run')}: `{run.get('run_id')}` / {_label('suite')}: `{run.get('suite_version')}`",
        f"- {_label('control')}: {control}",
        f"- {_label('incident_state')}: "
        + ("ok" if decision.get("incidents_known") else _label("incident_unknown")),
        "",
        "| scenario | version | outcome | transition | failure | attempts |",
        "|---|---|---|---|---|---|",
    ]
    by_fp = {
        item.get("fingerprint"): item for item in decision.get("transitions") or []
    }
    for row in run["scenarios"]:
        item = by_fp.get(row.get("fingerprint")) or {}
        issue = f" #{item['issue_number']}" if item.get("issue_number") else ""
        lines.append(
            f"| {row.get('id')} | `{row.get('version')}` | {row.get('outcome')} | "
            f"{item.get('transition', '-')}{issue} | {row.get('failure') or '-'} | "
            f"{row.get('attempts_made', 0)} |"
        )
    for item in decision.get("transitions") or []:
        if item.get("transition") == "orphaned":
            lines.append(
                f"| {item.get('scenario')} | - | not_registered | orphaned #{item.get('issue_number')} | - | 0 |"
            )
    lines += ["", _label("scope")]
    return lines


def _deploy_lines(evidence: Mapping[str, Any]) -> list[str]:
    result = (evidence.get("checks") or {}).get("verify_recent_deployments") or {}
    lines = [f"## {_label('deploy_title')}"]
    rollouts = result.get("rollouts")
    if not isinstance(rollouts, list):
        return [*lines, "", f"{_label('unavailable')}: {result.get('status', 'error')}"]
    lines += [
        "",
        f"- {_label('run')}: `{result.get('run_id')}` / window: {result.get('window_minutes')} min",
    ]
    if not rollouts:
        return lines
    lines += [
        "",
        "| service | revision | ready | traffic | verdict | 5xx before→after | p95 before→after | samples |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rollouts:
        metrics = row.get("metrics") or {}
        before, after = metrics.get("before") or {}, metrics.get("after") or {}
        samples = (
            f"{before.get('requests', '-')}/{after.get('requests', '-')}"
            if metrics.get("verdict") != "unavailable"
            else "unavailable"
        )
        if not row.get("metrics_evaluated") and metrics.get("verdict") != "unavailable":
            samples += " (unevaluated)"
        lines.append(
            f"| {row.get('service')} | {row.get('revision')} | {row.get('ready')} | "
            f"{row.get('traffic_percent')}% | {row.get('verdict')} | "
            f"{before.get('5xx_ratio', '-')}→{after.get('5xx_ratio', '-')} | "
            f"{before.get('p95_ms', '-')}→{after.get('p95_ms', '-')} | {samples} |"
        )
    return lines


def verdict_summary(evidence: Mapping[str, Any]) -> str:
    """Markdown summary of the code verdicts, in the configured language."""
    lines = (
        _service_lines(evidence)
        if evidence.get("job") == "service_check"
        else _deploy_lines(evidence)
    )
    return "\n".join(lines)


def fixed_report(evidence: Mapping[str, Any]) -> str:
    """Final report when no model investigation is needed."""
    decision = evidence.get("decision") or {}
    note = {
        "all_healthy": _label("healthy"),
        "no_rollouts": _label("no_rollouts"),
        "all_verified": _label("all_verified"),
    }.get(str(decision.get("reason")), "")
    return verdict_summary(evidence) + (f"\n\n{note}" if note else "")


def analysis_failed_report(evidence: Mapping[str, Any]) -> str:
    return f"{verdict_summary(evidence)}\n\n{_label('analysis_failed')}"
