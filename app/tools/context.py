"""Run context shared by every specialist."""

from __future__ import annotations

import datetime as dt

from app.config import CONFIG
from app.providers import get_provider

# Always-relevant building blocks used to filter threat intelligence.
GENERIC_STACK_KEYWORDS: tuple[str, ...] = (
    "Linux",
    "Debian",
    "Ubuntu",
    "Alpine",
    "OpenSSL",
    "PostgreSQL",
    "MySQL",
    "Node.js",
    "Python",
    "nginx",
)


def effective_stack_keywords() -> tuple[str, ...]:
    """``OPS_STACK_KEYWORDS`` if set, else provider defaults + generic ones."""
    if CONFIG.stack_keywords:
        return CONFIG.stack_keywords
    return (*get_provider().profile.stack_keywords, *GENERIC_STACK_KEYWORDS)


def get_run_context() -> dict:
    """Return the current time (local + UTC) and the team's operating
    parameters: cloud provider, target scope and region, the provider
    capabilities that are implemented, GitHub repository, dry-run and
    auto-close flags, issue cap, default error look-back, report language and
    stack keywords.

    Call this at the start of a run to timestamp reports correctly, to know
    whether writes to GitHub are real or simulated, and to know which checks
    this cloud supports.
    """
    now_utc = dt.datetime.now(dt.UTC)
    now_local = now_utc.astimezone(CONFIG.tz)
    iso_year, iso_week, _ = now_local.isocalendar()
    return {
        "now_local": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "now_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "iso_week": f"{iso_year}-W{iso_week:02d}",
        "month": now_local.strftime("%Y-%m"),
        **get_provider().describe(),
        "github_repo": CONFIG.github_repo,
        "dry_run": CONFIG.dry_run,
        "auto_close_issues": CONFIG.auto_close_issues,
        "max_issues_per_run": CONFIG.max_issues_per_run,
        "default_error_lookback_hours": CONFIG.error_lookback_hours,
        "report_language": CONFIG.report_language,
        "stack_keywords": list(effective_stack_keywords()),
    }
