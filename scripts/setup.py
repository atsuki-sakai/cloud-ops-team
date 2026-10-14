"""Create ``.env`` for your own project (interactive).

    uv run python scripts/setup.py                      # asks a few questions, detects defaults
    uv run python scripts/setup.py --yes \\
        --target my-project --repo my-org/my-app        # non-interactive

Detects the current ``gcloud`` project, whether Application Default
Credentials work and the GitHub login, then fills ``.env`` from
``.env.example`` (comments are kept). An existing ``.env`` is backed up first.
The result always starts in dry-run mode: nothing is written to GitHub until
you set ``OPS_DRY_RUN=false``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS = {
    "gcp": ("Google Cloud", "project", "asia-northeast1"),
    "aws": ("Amazon Web Services", "account id", "ap-northeast-1"),
    "azure": ("Microsoft Azure", "subscription id", "japaneast"),
}
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ASSIGNMENT = re.compile(r"^(#\s*)?([A-Z][A-Z0-9_]*)=(\S*)(.*)$")


def _run(cmd: list[str]) -> str:
    """stdout of ``cmd`` or an empty string when it is missing or fails."""
    if not shutil.which(cmd[0]):
        return ""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def detect() -> dict[str, str | bool]:
    """Best-effort defaults from the local gcloud / gh setup."""
    project = _run(["gcloud", "config", "get-value", "project"])
    return {
        "gcloud_project": "" if project in ("(unset)",) else project,
        "gcloud_account": _run(["gcloud", "config", "get-value", "account"]),
        "adc": bool(
            _run(["gcloud", "auth", "application-default", "print-access-token"])
        ),
        "gh_user": _run(["gh", "api", "user", "--jq", ".login"]),
    }


def render_env(template: str, values: dict[str, str]) -> str:
    """Set ``KEY=value`` for every key in ``values`` inside ``template``.

    Keeps comments and their alignment, uncomments ``# KEY=...`` lines that
    are being set, and appends keys the template does not contain.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in template.splitlines():
        match = _ASSIGNMENT.match(line)
        if not match or match.group(2) not in values or match.group(2) in seen:
            out.append(line)
            continue
        key, rest = match.group(2), match.group(4)
        seen.add(key)
        new = f"{key}={values[key]}"
        comment = rest.strip()
        if comment.startswith("#"):
            column = len(line) - len(rest) + (len(rest) - len(rest.lstrip()))
            new = new.ljust(max(column, len(new) + 1)) + comment
        out.append(new)
    missing = [key for key in values if key not in seen]
    if missing:
        out += ["", "# ---- added by scripts/setup.py"] + [
            f"{k}={values[k]}" for k in missing
        ]
    return "\n".join(out) + "\n"


def _ask(question: str, default: str, interactive: bool, validate=None) -> str:
    if not interactive:
        if validate and default and (err := validate(default)):
            raise SystemExit(f"Invalid value for '{question}': {err}")
        return default
    while True:
        suffix = f" [{default}]" if default else ""
        answer = input(f"? {question}{suffix}: ").strip() or default
        error = validate(answer) if validate else None
        if not answer:
            print("  value required")
        elif error:
            print(f"  {error}")
        else:
            return answer


def _valid_repo(value: str) -> str | None:
    return (
        None if REPO_RE.match(value) else "use the form owner/repo (e.g. my-org/my-app)"
    )


def _valid_tz(value: str) -> str | None:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return "unknown time zone (e.g. Asia/Tokyo, UTC, America/New_York)"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create .env for cloud-ops-team")
    parser.add_argument("--provider", choices=sorted(PROVIDERS))
    parser.add_argument("--target", help="project / account / subscription to monitor")
    parser.add_argument("--region", help="region of the monitored workload")
    parser.add_argument(
        "--model-project", help="Google Cloud project that runs Gemini (Vertex AI)"
    )
    parser.add_argument("--repo", help="GitHub repository for issues, owner/repo")
    parser.add_argument("--language", choices=["ja", "en"])
    parser.add_argument("--timezone")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument(
        "--yes",
        action="store_true",
        help="non-interactive: use flags and detected defaults",
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing .env without asking"
    )
    args = parser.parse_args(argv)
    interactive = not args.yes and sys.stdin.isatty()

    print("cloud-ops-team setup — answers are written to", args.env_file)
    found = detect()
    if found["gcloud_account"]:
        print(
            f"  gcloud account: {found['gcloud_account']}  project: {found['gcloud_project'] or '(none)'}"
        )
    if found["gh_user"]:
        print(f"  GitHub login: {found['gh_user']}")
    print()

    provider = args.provider or _ask(
        "Cloud to monitor (gcp / aws / azure)",
        "gcp",
        interactive,
        lambda v: None if v in PROVIDERS else "choose gcp, aws or azure",
    )
    name, target_kind, default_region = PROVIDERS[provider]
    if provider != "gcp":
        print(
            f"  note: the {name} provider is a skeleton — cloud checks report 'not supported' (see docs/providers.md)"
        )
    target = args.target or _ask(
        f"{name} {target_kind} to monitor",
        str(found["gcloud_project"] if provider == "gcp" else ""),
        interactive,
    )
    region = args.region or _ask("Region of the workload", default_region, interactive)
    model_project = args.model_project or _ask(
        "Google Cloud project that runs Gemini (Vertex AI)",
        target if provider == "gcp" else str(found["gcloud_project"]),
        interactive,
    )
    repo = args.repo or _ask(
        "GitHub repository for issues (owner/repo)", "", interactive, _valid_repo
    )
    if _valid_repo(repo):
        raise SystemExit(f"--repo: {_valid_repo(repo)}")
    language = args.language or _ask(
        "Language of reports and issues (ja / en)",
        "ja",
        interactive,
        lambda v: None if v in ("ja", "en") else "choose ja or en",
    )
    timezone = args.timezone or _ask(
        "Time zone for reports", "Asia/Tokyo", interactive, _valid_tz
    )
    if not target or not model_project:
        raise SystemExit(
            "A target and a model project are required (pass --target / --model-project)."
        )

    gcp_auth = "adc" if found["adc"] or provider != "gcp" else "gcloud"
    if provider == "gcp" and not found["adc"]:
        print(
            "  note: Application Default Credentials are not set up; using your gcloud login (OPS_GCP_AUTH=gcloud)."
        )
        print(
            "        For Gemini, run `gcloud auth application-default login` (see `make doctor`)."
        )

    values = {
        "GOOGLE_CLOUD_PROJECT": model_project,
        "OPS_CLOUD_PROVIDER": provider,
        "OPS_TARGET": target,
        "OPS_TARGET_REGION": region,
        "GITHUB_REPO": repo,
        "OPS_GCP_AUTH": gcp_auth,
        "OPS_DRY_RUN": "true",
        "OPS_REPORT_LANGUAGE": language,
        "OPS_TIMEZONE": timezone,
    }

    env_path = Path(args.env_file)
    if env_path.exists():
        if not args.force and interactive:
            if (
                input(f"? {env_path.name} exists. Back it up and overwrite? [y/N]: ")
                .strip()
                .lower()
                != "y"
            ):
                print("Nothing written.")
                return 1
        elif not args.force:
            raise SystemExit(
                f"{env_path} exists; pass --force to back it up and overwrite."
            )
        backup = env_path.with_name(
            f"{env_path.name}.backup-{dt.datetime.now():%Y%m%d-%H%M%S}"
        )
        shutil.copy2(env_path, backup)
        backup.chmod(0o600)
        print(f"  backed up the old file to {backup.name}")

    env_path.write_text(render_env((ROOT / ".env.example").read_text(), values))
    env_path.chmod(0o600)
    print(f"\n✅ Wrote {env_path} (dry-run mode: nothing is written to GitHub yet)\n")
    print("Next:")
    print("  make doctor                 # check model, GitHub and cloud access")
    print(
        "  make run JOB=error_watch    # run the error watch once and see what it would file"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
