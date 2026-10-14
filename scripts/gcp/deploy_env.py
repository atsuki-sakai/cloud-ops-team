"""Write the allowlisted Cloud Run environment as a JSON/YAML mapping."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path


def build_env(env: Mapping[str, str]) -> dict[str, str]:
    """Preserve values verbatim while excluding local credentials and paths."""
    values = {
        "OPS_CLOUD_PROVIDER": "gcp",
        "GOOGLE_GENAI_USE_ENTERPRISE": "true",
        "GOOGLE_CLOUD_PROJECT": env["PROJECT_ID"],
        "GOOGLE_CLOUD_LOCATION": "global",
        "OPS_TARGET": env["TARGET_PROJECT"],
        "OPS_TARGET_REGION": env["TARGET_REGION"],
        "OPS_GCP_AUTH": "adc",
        "GITHUB_REPO": env["GITHUB_REPO"],
        "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false",
    }
    defaults = {
        "OPS_DRY_RUN": "true",
        "OPS_AUTO_CLOSE_ISSUES": "false",
        "OPS_REPORT_LANGUAGE": "ja",
        "OPS_MODEL": "gemini-3.8-flash",
        "OPS_MAX_ISSUES_PER_RUN": "10",
        "OPS_ERROR_LOOKBACK_HOURS": "24",
        "OPS_RECURRENCE_COOLDOWN_HOURS": "24",
        "OPS_TIMEZONE": "Asia/Tokyo",
    }
    values.update({key: env.get(key) or default for key, default in defaults.items()})
    if env.get("OPS_TRIGGER_AUDIENCE"):
        values["OPS_TRIGGER_AUDIENCE"] = env["OPS_TRIGGER_AUDIENCE"]
        values["OPS_TRIGGER_SERVICE_ACCOUNTS"] = env["OPS_TRIGGER_SERVICE_ACCOUNTS"]
    for key in (
        "OPS_STACK_KEYWORDS",
        "OPS_ERROR_LABELS",
        "OPS_SECURITY_LABELS",
        "OPS_SEVERITY_LABELS",
        "OPS_RESEARCH_MODEL",
    ):
        if key in env:
            values[key] = env[key]
    return values


def main() -> None:
    Path(sys.argv[1]).write_text(
        json.dumps(build_env(os.environ), ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
