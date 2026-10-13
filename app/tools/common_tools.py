"""Tools shared by every specialist."""

from __future__ import annotations

import datetime as dt

from app.config import CONFIG


def get_run_context() -> dict:
    """Return the current time (local + UTC) and the team's operating
    parameters: target GCP project/region, GitHub repository, dry-run and
    auto-close flags, issue cap, default error look-back and stack keywords.

    Call this at the start of a run to timestamp reports correctly and to
    know whether writes to GitHub are real or simulated.
    """
    now_utc = dt.datetime.now(dt.UTC)
    now_local = now_utc.astimezone(CONFIG.tz)
    iso_year, iso_week, _ = now_local.isocalendar()
    return {
        "now_local": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "now_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "iso_week": f"{iso_year}-W{iso_week:02d}",
        "month": now_local.strftime("%Y-%m"),
        "target_project": CONFIG.target_project,
        "target_region": CONFIG.target_region,
        "github_repo": CONFIG.github_repo,
        "dry_run": CONFIG.dry_run,
        "auto_close_issues": CONFIG.auto_close_issues,
        "max_issues_per_run": CONFIG.max_issues_per_run,
        "default_error_lookback_hours": CONFIG.error_lookback_hours,
        "stack_keywords": list(CONFIG.stack_keywords),
    }
