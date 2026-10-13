"""Cloud-neutral, agent-facing tools.

Each function is a thin wrapper around the selected provider's capability
(see ``app/providers/base.py``). The docstrings are what the model sees, so
they describe the *normalized* behaviour and mention per-cloud equivalents.
A provider that has not implemented a capability returns
``status: not_supported``; provider exceptions become ``status: error``.
"""

from __future__ import annotations

from typing import Any

from app.providers import get_provider


def _call(capability: str, **kwargs: Any) -> dict:
    provider = get_provider()
    try:
        out = getattr(provider, capability)(**kwargs)
    except Exception as exc:  # never let a provider bug crash the agent run
        return {
            "status": "error",
            "provider": provider.profile.key,
            "capability": capability,
            "message": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
    if isinstance(out, dict):
        out.setdefault("provider", provider.profile.key)
    return out


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def summarize_error_groups(hours: int) -> dict:
    """Group error-level log entries of the last ``hours`` hours by fingerprint.

    Call this first in an error-watch run. Each group has a stable
    ``fingerprint`` (used for issue de-duplication), count, first/last seen,
    a redacted sample message, affected service and deployments, and a
    ``search_text`` usable later with ``count_matching_errors``.
    (GCP: Cloud Logging; AWS: CloudWatch Logs; Azure: Log Analytics.)

    Args:
        hours: Look-back window in hours (1-168).
    """
    return _call("summarize_error_groups", hours=hours)


def list_error_logs(hours: int, service_name: str, limit: int) -> dict:
    """List individual error-level log entries, newest first (redacted).

    Args:
        hours: Look-back window in hours (1-168).
        service_name: Service / job / database name to filter on, or an empty string for all.
        limit: Maximum entries to return (1-200).
    """
    return _call("list_error_logs", hours=hours, service_name=service_name, limit=limit)


def get_log_context(
    service_name: str, around_time: str, window_minutes: int, trace: str
) -> dict:
    """Fetch surrounding log lines (all severities) to investigate an error.

    Pass a ``trace`` id when available (all entries of that request are
    returned); otherwise pass ``service_name`` and ``around_time``.

    Args:
        service_name: Service / job / database name. Empty string if using trace.
        around_time: ISO-8601 timestamp at the centre of the window. Empty string if using trace.
        window_minutes: Minutes before and after ``around_time`` (1-120).
        trace: Trace / request id. Empty string if unused.
    """
    return _call(
        "get_log_context",
        service_name=service_name,
        around_time=around_time,
        window_minutes=window_minutes,
        trace=trace,
    )


def count_matching_errors(service_name: str, search_text: str, since: str) -> dict:
    """Count error-level entries containing ``search_text`` since a time.

    Used to confirm a fix: after the fixing PR was merged at time T, call with
    ``since=T`` and the issue's ``search_text`` to see whether it recurred.

    Args:
        service_name: Service / job / database name, or an empty string for all.
        search_text: Literal text (or URL path) stored in the issue metadata.
        since: ISO-8601 timestamp to count from.
    """
    return _call(
        "count_matching_errors",
        service_name=service_name,
        search_text=search_text,
        since=since,
    )


def list_services() -> dict:
    """List the compute services that run the workload with health info:
    readiness, latest deployment and time, images, runtime identity, scaling
    and environment variable *names* (never values).
    (GCP: Cloud Run; AWS: ECS / Lambda / App Runner; Azure: App Service / Container Apps.)
    """
    return _call("list_services")


def list_deployments(service_name: str, limit: int) -> dict:
    """List recent deployments / revisions of a service, newest first.
    Useful to correlate an error spike with a release.

    Args:
        service_name: Service name from ``list_services``.
        limit: Maximum deployments to return (1-20).
    """
    return _call("list_deployments", service_name=service_name, limit=limit)


def list_scheduled_jobs() -> dict:
    """List scheduled jobs with their last attempt status; failing jobs are
    flagged with ``failing: true``.
    (GCP: Cloud Scheduler; AWS: EventBridge Scheduler; Azure: Container Apps Jobs / Logic Apps.)
    """
    return _call("list_scheduled_jobs")


def list_databases() -> dict:
    """List managed database instances with state, ``engine`` and
    ``engine_version`` (for EOL checks), availability, backup and maintenance
    settings. Never returns credentials.
    """
    return _call("list_databases")


def get_request_error_rates(hours: int) -> dict:
    """Per-service request totals and 5xx / 4xx ratios from the cloud's metrics.

    Args:
        hours: Window in hours (1-168).
    """
    return _call("get_request_error_rates", hours=hours)


def get_alerting_setup() -> dict:
    """List alerting policies and uptime / availability checks, to judge
    whether an error class is already covered by an alert."""
    return _call("get_alerting_setup")


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


def audit_iam() -> dict:
    """Audit identity and access: public principals, over-privileged
    (owner/admin/editor-like) grants, risky account-wide roles and audit
    logging configuration. Returns findings with severity."""
    return _call("audit_iam")


def audit_credentials() -> dict:
    """Find long-lived credentials (service-account keys, IAM access keys,
    service-principal secrets) and their age."""
    return _call("audit_credentials")


def audit_compute() -> dict:
    """Audit compute services: public exposure, runtime identity, plain-text
    secret-looking environment variables, image pinning and deploy age."""
    return _call("audit_compute")


def audit_databases() -> dict:
    """Audit managed databases: network exposure, TLS enforcement, backups /
    point-in-time recovery, deletion protection and pending maintenance."""
    return _call("audit_databases")


def audit_network() -> dict:
    """Audit firewall / security-group rules for ingress open to the internet
    on sensitive ports (SSH, RDP, databases) or on all ports."""
    return _call("audit_network")


def audit_security_services() -> dict:
    """Check which security services are enabled (posture management,
    container scanning, threat detection, asset inventory) and recommend
    missing ones."""
    return _call("audit_security_services")


def list_security_findings(max_findings: int) -> dict:
    """List active CRITICAL / HIGH / MEDIUM findings from the cloud's security
    posture service (GCP: Security Command Center; AWS: Security Hub;
    Azure: Defender for Cloud).

    Args:
        max_findings: Maximum findings to return (1-200).
    """
    return _call("list_security_findings", max_findings=max_findings)


def scan_deployed_images() -> dict:
    """Summarise vulnerability scan results for the container images that are
    currently serving traffic."""
    return _call("scan_deployed_images")
