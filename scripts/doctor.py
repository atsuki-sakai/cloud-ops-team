"""Check that the team can do its job, and say how to fix what is missing.

    uv run python scripts/doctor.py            # everything
    uv run python scripts/doctor.py --quick    # skip the security audits

Checks the configuration, Gemini, GitHub and every cloud capability the
agents use. Everything is read-only; one tiny model call is made. Exit code
is 1 when something required fails.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
SYMBOL = {OK: "✓", INFO: "·", WARN: "!", FAIL: "✗"}
PLACEHOLDER = re.compile(r"^(your-|owner/repo$|my-)", re.I)
# Optional security services: missing ones are reported by the audit itself, not a setup failure.
OPTIONAL_CAPABILITIES = {"list_security_findings", "scan_deployed_images"}
AUDITS = {
    "audit_iam",
    "audit_credentials",
    "audit_compute",
    "audit_databases",
    "audit_network",
    "audit_security_services",
    "list_security_findings",
    "scan_deployed_images",
    "investigate_access",
    "list_optimization_recommendations",
}


@dataclass
class Check:
    level: str
    title: str
    detail: str = ""
    hint: str = ""


def classify(capability: str, result: Any, target: str) -> Check:
    """Turn a capability result into a check with a fix hint."""
    if not isinstance(result, dict):
        return Check(
            FAIL, capability, f"unexpected result type {type(result).__name__}"
        )
    status = str(result.get("status", "error"))
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    message = str(error.get("message") or result.get("message") or "")[:220]
    if status == "ok":
        return Check(OK, capability)
    if status == "partial":
        return Check(
            FAIL,
            capability,
            "only part of the required evidence was retrieved; review source coverage",
            str(
                result.get("hint")
                or "check the source errors, API enablement and read permissions"
            ),
        )
    if status == "not_supported":
        return Check(
            INFO,
            capability,
            "not implemented by this provider yet (reported as 'not checked')",
        )
    level = WARN if capability in OPTIONAL_CAPABILITIES else FAIL
    if status == "api_disabled":
        api = error.get("api") or _api_from(message)
        hint = (
            f"gcloud services enable {api} --project {target}"
            if api
            else str(result.get("hint", ""))
        )
        return Check(level, capability, "API is not enabled", hint)
    if status == "permission_denied":
        permission = _permission_from(message)
        hint = "grant the read-only roles: make gcp-grant (or scripts/gcp/grant_target_roles.sh)"
        detail = f"missing permission {permission}" if permission else message
        if "serviceusage" in message.lower() or "USER_PROJECT_DENIED" in str(
            error.get("reason")
        ):
            detail = f"cannot use project {target} (serviceusage.services.use)"
            hint = f"grant roles/serviceusage.serviceUsageConsumer on {target} (make gcp-grant)"
        return Check(level, capability, detail, hint)
    return Check(level, capability, f"{status}: {message}", str(result.get("hint", "")))


def _api_from(message: str) -> str:
    match = re.search(r"([a-z0-9-]+\.googleapis\.com)", message)
    if match:
        return match.group(1)
    match = re.search(r"apis/api/([a-z0-9.-]+)/", message)
    return match.group(1) if match else ""


def _permission_from(message: str) -> str:
    match = re.search(r"[Pp]ermission ['\"]?([a-z]+\.[A-Za-z.]+)['\"]?", message)
    return match.group(1) if match else ""


def _print(section: str, checks: list[Check]) -> None:
    print(f"\n{section}")
    for check in checks:
        line = f"  {SYMBOL[check.level]} {check.title}"
        if check.detail:
            line += f" — {check.detail}"
        print(line)
        if check.hint:
            print(f"      → {check.hint}")


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def check_config() -> list[Check]:
    from app.config import CONFIG, LANGUAGES
    from app.providers import available_providers

    checks: list[Check] = []
    if CONFIG.provider not in available_providers():
        checks.append(
            Check(
                FAIL,
                f"OPS_CLOUD_PROVIDER={CONFIG.provider}",
                "unknown provider",
                "use gcp, aws or azure",
            )
        )
    else:
        checks.append(Check(OK, f"provider: {CONFIG.provider}"))
    for name, value, fix in (
        (
            "OPS_TARGET",
            CONFIG.target,
            "the project / account / subscription to monitor",
        ),
        ("GITHUB_REPO", CONFIG.github_repo, "owner/repo where issues are filed"),
    ):
        if not value or PLACEHOLDER.match(value):
            checks.append(
                Check(
                    FAIL,
                    name,
                    f"not set ({value or 'empty'})",
                    f"set {fix} in .env (make setup)",
                )
            )
        else:
            checks.append(Check(OK, f"{name}: {value}"))
    checks.append(
        Check(OK if CONFIG.region else WARN, f"region: {CONFIG.region or '(empty)'}")
    )
    if CONFIG.report_language not in LANGUAGES:
        checks.append(
            Check(WARN, f"OPS_REPORT_LANGUAGE={CONFIG.report_language}", "use ja or en")
        )
    mode = (
        "dry-run (previews only, nothing is written to GitHub)"
        if CONFIG.dry_run
        else "LIVE (issues are created)"
    )
    checks.append(Check(INFO, f"mode: {mode}"))
    return checks


def check_model() -> list[Check]:
    from google import genai
    from google.genai import types

    from app.config import CONFIG

    start = time.monotonic()
    try:
        client = (
            genai.Client()
        )  # keep a reference: a garbage-collected client closes its HTTP session
        response = client.models.generate_content(
            model=CONFIG.model,
            contents="Reply with the single word OK.",
            config=types.GenerateContentConfig(max_output_tokens=8, temperature=0),
        )
        elapsed = time.monotonic() - start
        return [
            Check(
                OK,
                f"{CONFIG.model} answered in {elapsed:.1f}s",
                (response.text or "").strip()[:20],
            )
        ]
    except Exception as exc:
        text = str(exc)
        hint = "check GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION=global, or set GEMINI_API_KEY"
        if "PERMISSION_DENIED" in text or "403" in text:
            hint = (
                "the Application Default Credentials account needs roles/aiplatform.user on GOOGLE_CLOUD_PROJECT: "
                "`gcloud auth application-default login` with the right account"
            )
        elif "SERVICE_DISABLED" in text or "has not been used" in text:
            hint = "gcloud services enable aiplatform.googleapis.com --project <GOOGLE_CLOUD_PROJECT>"
        elif (
            "DefaultCredentialsError" in type(exc).__name__
            or "credentials" in text.lower()
        ):
            hint = "gcloud auth application-default login"
        elif "404" in text or "NOT_FOUND" in text:
            hint = "use GOOGLE_CLOUD_LOCATION=global or another model (OPS_MODEL)"
        return [
            Check(
                FAIL,
                f"model {CONFIG.model}",
                f"{type(exc).__name__}: {text[:160]}",
                hint,
            )
        ]


def check_github() -> list[Check]:
    from app.config import CONFIG
    from app.integrations.github import GitHubAuthError, gh_rest, github_token

    try:
        github_token()
    except GitHubAuthError as exc:
        return [
            Check(
                FAIL,
                "GitHub token",
                str(exc),
                "set GITHUB_TOKEN or run `gh auth login`",
            )
        ]
    checks = [Check(OK, "token found")]
    ok, repo = gh_rest("GET", f"/repos/{CONFIG.github_repo}")
    if not ok:
        checks.append(
            Check(
                FAIL,
                f"repository {CONFIG.github_repo}",
                repo.get("message", ""),
                "check GITHUB_REPO and token access",
            )
        )
        return checks
    checks.append(
        Check(OK, f"repository {repo.get('full_name')} ({repo.get('visibility')})")
    )
    if not repo.get("has_issues"):
        checks.append(
            Check(
                FAIL,
                "issues are disabled",
                "",
                "enable Issues in the repository settings",
            )
        )
    permissions = repo.get("permissions") or {}
    if permissions and not (
        permissions.get("push")
        or permissions.get("triage")
        or permissions.get("maintain")
    ):
        checks.append(
            Check(
                WARN,
                "write access",
                "the token's user may not be able to create issues or labels",
                "use a token with Issues: read and write",
            )
        )
    return checks


def check_cloud(quick: bool) -> list[Check]:
    from app.config import CONFIG
    from app.providers import CAPABILITIES, get_provider

    provider = get_provider()
    implemented = set(provider.capabilities())
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    args: dict[str, dict[str, Any]] = {
        "summarize_error_groups": {"hours": 1},
        "list_error_logs": {"hours": 1, "service_name": "", "limit": 1},
        "get_log_context": {
            "service_name": "",
            "around_time": "",
            "window_minutes": 1,
            "trace": "",
        },
        "count_matching_errors": {
            "service_name": "",
            "search_text": "cloud-ops-team-doctor",
            "since": since,
        },
        "get_request_error_rates": {"hours": 1},
        "list_security_findings": {"max_findings": 1},
        "investigate_access": {"hours": 1},
    }
    names = [
        c
        for c in CAPABILITIES
        if c != "list_deployments" and not (quick and c in AUDITS)
    ]

    def call(capability: str) -> tuple[str, Any]:
        if capability not in implemented:
            return capability, {"status": "not_supported"}
        try:
            return capability, getattr(provider, capability)(**args.get(capability, {}))
        except Exception as exc:
            return capability, {
                "status": "error",
                "message": f"{type(exc).__name__}: {exc}",
            }

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = dict(pool.map(call, names))
    services = results.get("list_services", {})
    services_ok = isinstance(services, dict) and services.get("status") == "ok"
    first = next(iter(services.get("services") or []), {}) if services_ok else {}
    if "list_deployments" not in implemented:
        results["list_deployments"] = {"status": "not_supported"}
    elif first.get("name"):
        results["list_deployments"] = call_deployments(provider, first["name"])
    else:
        results["list_deployments"] = {"status": "skipped"}

    identity = (
        gcp_identity() if CONFIG.provider == "gcp" else "provider default credentials"
    )
    profile = provider.profile
    print(
        f"\n  {profile.display_name} {profile.target_label} {CONFIG.target}, region {CONFIG.region}"
    )
    print(f"  checking as: {identity}")
    checks = []
    for capability in CAPABILITIES:
        if capability not in results:
            continue
        if results[capability].get("status") == "skipped":
            checks.append(Check(INFO, capability, "skipped (no service to inspect)"))
        else:
            checks.append(classify(capability, results[capability], CONFIG.target))
    denied = [
        c
        for c in checks
        if c.level == FAIL
        and ("permission" in c.detail or "cannot use project" in c.detail)
    ]
    if len(denied) >= 3:
        for check in denied:
            check.hint = ""
        checks.append(
            Check(
                FAIL,
                f"likely cause: {identity.split(' ')[0]} cannot read {CONFIG.target}",
                f"{len(denied)} checks were denied for the same identity",
                "locally: use an account that can view the project "
                "(`gcloud auth application-default login`, or OPS_GCP_AUTH=gcloud to use your gcloud login); "
                "on Cloud Run: `make gcp-grant` gives the runtime service account the read-only roles",
            )
        )
    return checks


def gcp_identity() -> str:
    """Who the Google Cloud tools act as on this machine (best effort)."""
    import os
    import subprocess

    if (os.getenv("OPS_GCP_AUTH") or "adc").strip().lower() == "gcloud":
        out = subprocess.run(
            ["gcloud", "config", "get-value", "account"],
            capture_output=True,
            text=True,
            check=False,
        )
        return f"{out.stdout.strip() or 'unknown'} (gcloud login, OPS_GCP_AUTH=gcloud)"
    try:
        import google.auth
        import httpx
        from google.auth.transport.requests import Request

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        email = getattr(creds, "service_account_email", "") or getattr(
            creds, "_target_principal", ""
        )
        if not email:
            creds.refresh(Request())
            info = httpx.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": creds.token},
                timeout=10,
            ).json()
            email = info.get("email", "")
        return f"{email or 'unknown'} (Application Default Credentials)"
    except Exception as exc:
        return f"no Application Default Credentials ({type(exc).__name__})"


def call_deployments(provider: Any, service: str) -> Any:
    try:
        return provider.list_deployments(service_name=service, limit=1)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--quick", action="store_true", help="skip the security audit capabilities"
    )
    parser.add_argument("--skip-model", action="store_true")
    parser.add_argument("--skip-cloud", action="store_true")
    args = parser.parse_args(argv)

    print("cloud-ops-team doctor")
    sections: list[tuple[str, list[Check]]] = [("Configuration", check_config())]
    _print(*sections[-1])
    if any(c.level == FAIL for c in sections[0][1]):
        print("\nFix the configuration first (make setup), then run make doctor again.")
        return 1
    if not args.skip_model:
        sections.append(("Model (Gemini)", check_model()))
        _print(*sections[-1])
    sections.append(("GitHub", check_github()))
    _print(*sections[-1])
    if not args.skip_cloud:
        print("\nCloud access (read-only checks, may take a minute)", end="")
        sections.append(("Cloud access", check_cloud(args.quick)))
        _print("", sections[-1][1])

    counts = {
        level: sum(c.level == level for _, checks in sections for c in checks)
        for level in SYMBOL
    }
    print(
        f"\nSummary: {counts[OK]} ok, {counts[WARN]} warnings, {counts[FAIL]} failures, {counts[INFO]} info"
    )
    if counts[FAIL]:
        print("Fix the ✗ items above (each has a → hint), then run make doctor again.")
        return 1
    print("Ready. Next: make run JOB=error_watch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
