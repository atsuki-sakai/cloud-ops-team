"""Environment-driven configuration.

Everything the team needs to know about *where* it operates (cloud provider,
target scope and region, GitHub repository), *how* it thinks (model, report
language) and *how carefully* it acts (dry-run, auto-close, issue caps) lives
here. Prompts and tools read only from ``CONFIG``; nothing is hardcoded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODEL = "gemini-3.8-flash"
LANGUAGES: dict[str, str] = {"ja": "日本語", "en": "English"}

# Provider-specific variables used when the generic OPS_TARGET / OPS_TARGET_REGION
# are not set (keeps existing GCP deployments working unchanged).
_PROVIDER_ENV: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "gcp": (
        ("GCP_TARGET_PROJECT", "GOOGLE_CLOUD_PROJECT"),
        ("GCP_TARGET_REGION",),
        "asia-northeast1",
    ),
    "aws": (
        ("AWS_ACCOUNT_ID",),
        ("AWS_REGION", "AWS_DEFAULT_REGION"),
        "ap-northeast-1",
    ),
    "azure": (("AZURE_SUBSCRIPTION_ID",), ("AZURE_LOCATION",), "japaneast"),
}


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


def _env_csv(name: str) -> tuple[str, ...]:
    raw = os.getenv(name) or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _first_word(raw: str | None, default: str) -> str:
    """First whitespace-separated token, lower-cased (tolerates trailing comments)."""
    words = (raw or "").split()
    return words[0].lower() if words else default


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True)
class OpsConfig:
    """Resolved runtime settings (see ``.env.example`` for documentation)."""

    provider: str
    target: str
    region: str
    github_repo: str
    model: str
    research_model: str
    report_language: str
    dry_run: bool
    auto_close_issues: bool
    max_issues_per_run: int
    error_lookback_hours: int
    recurrence_cooldown_hours: int
    timezone: str
    stack_keywords: tuple[str, ...]
    issue_label: str = "ops-agent"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def language_name(self) -> str:
        return LANGUAGES.get(self.report_language, self.report_language)

    @property
    def repo_owner(self) -> str:
        return self.github_repo.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        return self.github_repo.split("/", 1)[1] if "/" in self.github_repo else ""


def load_config() -> OpsConfig:
    """Build an ``OpsConfig`` from the process environment."""
    provider = (os.getenv("OPS_CLOUD_PROVIDER") or "gcp").strip().lower()
    target_vars, region_vars, default_region = _PROVIDER_ENV.get(provider, ((), (), ""))
    model = (os.getenv("OPS_MODEL") or DEFAULT_MODEL).strip()
    return OpsConfig(
        provider=provider,
        target=(os.getenv("OPS_TARGET") or "").strip() or _first_env(target_vars),
        region=(os.getenv("OPS_TARGET_REGION") or "").strip()
        or _first_env(region_vars)
        or default_region,
        github_repo=(os.getenv("GITHUB_REPO") or "").strip(),
        model=model,
        research_model=(os.getenv("OPS_RESEARCH_MODEL") or model).strip(),
        report_language=_first_word(os.getenv("OPS_REPORT_LANGUAGE"), "ja"),
        dry_run=_env_bool("OPS_DRY_RUN", True),
        auto_close_issues=_env_bool("OPS_AUTO_CLOSE_ISSUES", False),
        max_issues_per_run=_env_int("OPS_MAX_ISSUES_PER_RUN", 10),
        error_lookback_hours=_env_int("OPS_ERROR_LOOKBACK_HOURS", 24),
        recurrence_cooldown_hours=_env_int("OPS_RECURRENCE_COOLDOWN_HOURS", 24),
        timezone=(os.getenv("OPS_TIMEZONE") or "Asia/Tokyo").strip(),
        stack_keywords=_env_csv("OPS_STACK_KEYWORDS"),
    )


CONFIG = load_config()
