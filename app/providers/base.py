"""The cloud provider contract.

A provider adapts one cloud (GCP, AWS, Azure, ...) to the fixed set of
read-only *capabilities* the specialists use. Agent-facing tools in
``app/tools/cloud.py`` are thin wrappers around these methods, so prompts and
tool names stay identical across clouds.

Rules for implementations:
- Read-only. Never change cloud resources.
- Return a JSON-serializable ``dict`` with a ``status`` key (``ok``,
  ``api_disabled``, ``permission_denied``, ``not_found``, ``error`` ...).
  Do not raise for API failures; the model reports them.
- Audits (``audit_*``) return ``app.core.findings.result(...)``.
- Redact log excerpts with ``app.core.redact.redact`` and never return secret
  values (environment variable *names* only).
- Unimplemented capabilities inherit the default, which returns
  ``status: not_supported``.

See ``docs/providers.md`` for the normalized result fields of each capability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

OBSERVABILITY_CAPABILITIES: tuple[str, ...] = (
    "summarize_error_groups",
    "list_error_logs",
    "get_log_context",
    "count_matching_errors",
    "list_services",
    "list_deployments",
    "list_scheduled_jobs",
    "list_databases",
    "get_request_error_rates",
    "get_alerting_setup",
)
SECURITY_CAPABILITIES: tuple[str, ...] = (
    "audit_iam",
    "audit_credentials",
    "audit_compute",
    "audit_databases",
    "audit_network",
    "audit_security_services",
    "list_security_findings",
    "scan_deployed_images",
)
CAPABILITIES: tuple[str, ...] = OBSERVABILITY_CAPABILITIES + SECURITY_CAPABILITIES


@dataclass(frozen=True)
class ProviderProfile:
    """Static description of a provider, injected into prompts."""

    key: str
    display_name: str
    target_label: str
    target_label_ja: str
    cli: str
    stack_keywords: tuple[str, ...]
    glossary: dict[str, str] = field(default_factory=dict)
    notes: str = ""


class CloudProvider:
    """Base class: every capability returns ``not_supported`` until overridden."""

    profile: ProviderProfile

    def __init__(self, target: str, region: str) -> None:
        self.target = target
        self.region = region

    # ------------------------------------------------------------------ meta
    @classmethod
    def capabilities(cls) -> list[str]:
        """Capabilities this provider actually implements."""
        return [
            c for c in CAPABILITIES if getattr(cls, c) is not getattr(CloudProvider, c)
        ]

    def describe(self) -> dict[str, Any]:
        implemented = self.capabilities()
        return {
            "provider": self.profile.key,
            "provider_name": self.profile.display_name,
            "target_type": self.profile.target_label,
            "target": self.target,
            "region": self.region,
            "capabilities": implemented,
            "unsupported_capabilities": [
                c for c in CAPABILITIES if c not in implemented
            ],
        }

    def _not_supported(self, capability: str) -> dict[str, Any]:
        return {
            "status": "not_supported",
            "provider": self.profile.key,
            "capability": capability,
            "message": f"The {self.profile.display_name} provider does not implement '{capability}' yet.",
            "hint": "Report it as 'not checked'. Implementation guide: docs/providers.md",
        }

    # --------------------------------------------------------- observability
    def summarize_error_groups(self, hours: int) -> dict[str, Any]:
        return self._not_supported("summarize_error_groups")

    def list_error_logs(
        self, hours: int, service_name: str, limit: int
    ) -> dict[str, Any]:
        return self._not_supported("list_error_logs")

    def get_log_context(
        self, service_name: str, around_time: str, window_minutes: int, trace: str
    ) -> dict[str, Any]:
        return self._not_supported("get_log_context")

    def count_matching_errors(
        self, service_name: str, search_text: str, since: str
    ) -> dict[str, Any]:
        return self._not_supported("count_matching_errors")

    def list_services(self) -> dict[str, Any]:
        return self._not_supported("list_services")

    def list_deployments(self, service_name: str, limit: int) -> dict[str, Any]:
        return self._not_supported("list_deployments")

    def list_scheduled_jobs(self) -> dict[str, Any]:
        return self._not_supported("list_scheduled_jobs")

    def list_databases(self) -> dict[str, Any]:
        return self._not_supported("list_databases")

    def get_request_error_rates(self, hours: int) -> dict[str, Any]:
        return self._not_supported("get_request_error_rates")

    def get_alerting_setup(self) -> dict[str, Any]:
        return self._not_supported("get_alerting_setup")

    # -------------------------------------------------------------- security
    def audit_iam(self) -> dict[str, Any]:
        return self._not_supported("audit_iam")

    def audit_credentials(self) -> dict[str, Any]:
        return self._not_supported("audit_credentials")

    def audit_compute(self) -> dict[str, Any]:
        return self._not_supported("audit_compute")

    def audit_databases(self) -> dict[str, Any]:
        return self._not_supported("audit_databases")

    def audit_network(self) -> dict[str, Any]:
        return self._not_supported("audit_network")

    def audit_security_services(self) -> dict[str, Any]:
        return self._not_supported("audit_security_services")

    def list_security_findings(self, max_findings: int) -> dict[str, Any]:
        return self._not_supported("list_security_findings")

    def scan_deployed_images(self) -> dict[str, Any]:
        return self._not_supported("scan_deployed_images")
