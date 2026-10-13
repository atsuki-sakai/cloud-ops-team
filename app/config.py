"""Environment-driven configuration for the GCP ops team agents.

Everything the team needs to know about *where* it operates (GCP project,
GitHub repository) and *how carefully* it should act (dry-run, auto-close,
issue caps) lives here so that prompts and tools never hardcode it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class OpsConfig:
    """Resolved runtime settings (see ``.env.example`` for documentation)."""

    target_project: str
    target_region: str
    github_repo: str
    dry_run: bool
    auto_close_issues: bool
    max_issues_per_run: int
    error_lookback_hours: int
    timezone: str
    stack_keywords: tuple[str, ...]
    recurrence_cooldown_hours: int = 24
    issue_label: str = "ops-agent"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def repo_owner(self) -> str:
        return self.github_repo.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        return self.github_repo.split("/", 1)[1] if "/" in self.github_repo else ""


def load_config() -> OpsConfig:
    """Build an ``OpsConfig`` from the process environment."""
    project = (
        os.getenv("GCP_TARGET_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT") or ""
    ).strip()
    return OpsConfig(
        target_project=project,
        target_region=os.getenv("GCP_TARGET_REGION", "asia-northeast1").strip(),
        github_repo=os.getenv("GITHUB_REPO", "").strip(),
        dry_run=_env_bool("OPS_DRY_RUN", True),
        auto_close_issues=_env_bool("OPS_AUTO_CLOSE_ISSUES", False),
        max_issues_per_run=_env_int("OPS_MAX_ISSUES_PER_RUN", 10),
        error_lookback_hours=_env_int("OPS_ERROR_LOOKBACK_HOURS", 24),
        timezone=os.getenv("OPS_TIMEZONE", "Asia/Tokyo").strip() or "Asia/Tokyo",
        recurrence_cooldown_hours=_env_int("OPS_RECURRENCE_COOLDOWN_HOURS", 24),
        stack_keywords=_env_csv(
            "OPS_STACK_KEYWORDS",
            (
                "Cloud Run",
                "Cloud SQL",
                "PostgreSQL",
                "Node.js",
                "Next.js",
                "Rust",
                "Firebase",
                "Stripe",
                "Debian",
                "Alpine",
                "Linux",
            ),
        ),
    )


CONFIG = load_config()
