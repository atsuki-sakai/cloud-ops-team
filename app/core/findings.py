"""Shared result helpers for audit-style tools (any provider).

Every audit returns ``{"status": "ok", "check": ..., "finding_counts": ...,
"findings": [...], "summary": {...}}`` and every finding carries ``check_id``,
``severity`` (critical/high/medium/low/info), ``title``, ``resource``,
``detail`` and ``recommendation``. Keeping the shape identical across
providers lets the prompts stay cloud-neutral.
"""

from __future__ import annotations

from typing import Any

from app.core.payloads import Payload, failure_result
from app.core.redact import redact

SEVERITIES = ("critical", "high", "medium", "low", "info")


def normalize_severity(severity: str) -> str:
    """Known severities pass through; anything else counts as ``medium``."""
    return severity if severity in SEVERITIES else "medium"


def finding(
    check_id: str,
    severity: str,
    title: str,
    resource: str,
    detail: str,
    recommendation: str,
) -> dict[str, Any]:
    """Build one normalized finding (``detail`` is redacted)."""
    return {
        "check_id": check_id,
        "severity": normalize_severity(severity),
        "title": title,
        "resource": resource,
        "detail": redact(detail),
        "recommendation": recommendation,
    }


def result(
    check: str, findings: list[dict[str, Any]], summary: dict[str, Any]
) -> dict[str, Any]:
    """Wrap findings with per-severity counts."""
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {
        "status": "ok",
        "check": check,
        "finding_counts": counts,
        "findings": findings,
        "summary": summary,
    }


def error_result(check: str, payload: Payload, hint: str = "") -> dict[str, Any]:
    """Turn an API error payload into an audit result the model can report."""
    return failure_result(payload, check=check, **({"hint": hint} if hint else {}))
