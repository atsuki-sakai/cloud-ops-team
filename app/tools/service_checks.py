"""Service check and post-deployment verification collectors (read-only).

Both tools run operator-registered scenarios against public URLs and judge
them in code. They are fixed collection steps of their jobs and are never
offered to a model: a model only reads the recorded verdicts and investigates
the failures. Every scenario verdict is also written as a structured log line
so a failed analysis cannot lose the check result.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from app.config import CONFIG
from app.core.deploy_verification import evaluate_rollout
from app.core.service_checks import (
    DEPLOY_DEFAULTS,
    RUN_LIMITATIONS,
    ScenarioError,
    load_suite,
)
from app.core.synthetic import control_check, run_scenario, run_suite
from app.core.timeutil import iso_utc, utcnow
from app.tools.cloud import list_recent_rollouts

logger = logging.getLogger(__name__)
# One run per process at a time: a delayed scheduler retry or a concurrent
# manual request must not start the same browser scenarios twice.
_RUN_LOCK = threading.Lock()
MAX_ROLLOUTS = 10


def _suite_path() -> str:
    path = Path(CONFIG.service_checks_file)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return str(path)


def _load() -> tuple[Any, dict | None]:
    try:
        suite = load_suite(_suite_path())
    except ScenarioError as exc:
        return None, {
            "status": "invalid_input",
            "complete": False,
            "message": f"The service check file is invalid: {exc}",
        }
    if suite is None:
        return None, {
            "status": "not_configured",
            "complete": False,
            "message": "No service check scenarios are configured (OPS_SERVICE_CHECKS_FILE).",
            "hint": "Copy config/service-checks.example.json to config/service-checks.json.",
        }
    return suite, None


def _log(run_id: str, trigger: str, suite_version: str, row: dict[str, Any]) -> None:
    logger.info(
        json.dumps(
            {
                "event": "ops_service_check",
                "run_id": run_id,
                "trigger": trigger,
                "suite_version": suite_version,
                "scenario": row.get("id"),
                "scenario_version": row.get("version"),
                "service": row.get("service"),
                "outcome": row.get("outcome"),
                "failure": row.get("failure"),
                "attempts_made": row.get("attempts_made", 0),
            },
            ensure_ascii=False,
        )
    )


def _busy(trigger: str) -> dict[str, Any]:
    return {
        "status": "partial",
        "complete": False,
        "trigger": trigger,
        "message": "Another service check run is in progress in this instance; nothing was executed.",
    }


def run_service_checks(attempts: int) -> dict:
    """Run every registered service check scenario and judge it in code.

    Each scenario is retried up to ``attempts`` times only to confirm a
    failure; a failure followed by a pass is reported as ``flaky`` with the
    first failure kept. Outcomes: pass, flaky, fail, inconclusive (checker
    network down), checker_error, maintenance, not_run.

    Args:
        attempts: Maximum attempts per scenario (1-5).
    """
    attempts = max(1, min(int(attempts), 5))
    suite, error = _load()
    if error:
        return {**error, "trigger": "service_check"}
    if not _RUN_LOCK.acquire(blocking=False):
        return _busy("service_check")
    run_id = uuid.uuid4().hex
    started = utcnow()
    try:
        result = run_suite(suite, attempts)
    finally:
        _RUN_LOCK.release()
    for row in result["scenarios"]:
        _log(run_id, "service_check", suite.version, row)
    summary = result["summary"]
    complete = not (
        summary["not_run"] or summary["checker_error"] or summary["inconclusive"]
    )
    return {
        "status": "ok" if complete else "partial",
        "complete": complete,
        "run_id": run_id,
        "trigger": "service_check",
        "suite_version": suite.version,
        "started_at": iso_utc(started),
        "finished_at": iso_utc(utcnow()),
        "control": result["control"],
        "summary": summary,
        "scenarios": result["scenarios"],
        "limitations": list(RUN_LIMITATIONS)
        + (
            []
            if suite.control_url
            else [
                "No control_url: a checker network outage cannot be told apart from a site outage."
            ]
        ),
    }


def verify_recent_deployments(minutes: int) -> dict:
    """Verify revisions deployed in the last ``minutes`` minutes.

    Uses the cloud's actual serving state (ready revision, traffic split,
    tagged candidate URLs), runs the production scenarios registered for each
    deployed service when the new revision serves traffic, and compares the
    new revision's own error rate and p95 latency with the previous revisions.

    Args:
        minutes: Look-back window for deployments in minutes (5-1440).
    """
    minutes = max(5, min(int(minutes), 1440))
    suite, error = _load()
    if error and error["status"] != "not_configured":
        return {**error, "trigger": "deploy_verification"}
    rollouts = list_recent_rollouts(minutes)
    if not isinstance(rollouts, dict) or rollouts.get("status") not in (
        "ok",
        "partial",
    ):
        return {
            "status": (rollouts or {}).get("status", "error")
            if isinstance(rollouts, dict)
            else "error",
            "complete": False,
            "trigger": "deploy_verification",
            "message": "Deployment state could not be read; nothing is verified.",
            "rollouts_error": rollouts,
        }
    rows = [row for row in rollouts.get("rollouts") or [] if isinstance(row, dict)]
    truncated = len(rows) > MAX_ROLLOUTS or bool(rollouts.get("truncated"))
    rows = rows[:MAX_ROLLOUTS]
    if not _RUN_LOCK.acquire(blocking=False):
        return _busy("deploy_verification")
    run_id = uuid.uuid4().hex
    cfg = dict(suite.deploy) if suite else dict(DEPLOY_DEFAULTS)
    results = []
    try:
        control = (
            control_check(suite.control_url) if suite else {"status": "not_configured"}
        )
        for rollout in rows:
            scenario_rows = []
            serving = any(
                row.get("revision") == rollout.get("revision")
                and int(row.get("percent") or 0) > 0
                for row in rollout.get("traffic") or []
                if isinstance(row, dict)
            )
            if (
                suite
                and rollout.get("ready")
                and serving
                and not rollout.get("superseded")
            ):
                for scenario in suite.scenarios:
                    if (
                        scenario.service != rollout.get("service")
                        or scenario.environment != "production"
                    ):
                        continue
                    row = run_scenario(scenario, suite, scenario.attempts, control)
                    _log(run_id, "deploy_verification", suite.version, row)
                    scenario_rows.append(row)
            results.append(evaluate_rollout(rollout, scenario_rows, cfg))
    finally:
        _RUN_LOCK.release()
    incomplete = (
        truncated
        or rollouts.get("complete") is False
        or any(
            not row["metrics_evaluated"]
            for row in results
            if row["verdict"] != "superseded"
        )
        or any(row["verdict"] == "unverified" for row in results)
    )
    out = {
        "status": "partial" if incomplete else "ok",
        "complete": not incomplete,
        "run_id": run_id,
        "trigger": "deploy_verification",
        "window_minutes": minutes,
        "suite_version": suite.version if suite else None,
        "checked_at": iso_utc(utcnow()),
        "control": control,
        "rollout_count": len(results),
        "rollouts": results,
        "truncated": truncated,
        "limitations": [
            "A merged pull request or finished build is not treated as deployed; only serving revisions are verified.",
            "Scenario results describe the checker's region and network, not every user's experience.",
        ],
    }
    if error:
        out["scenario_configuration"] = error
    return out
