"""Shared Cloud Run Admin API (v2) pieces for the read-only GCP modules.

URL builders are separate from the calls so each caller keeps calling
``gcp_list`` / ``gcp_get`` through its own module (tests patch those names).
"""

from __future__ import annotations

from typing import Any

from app.core.payloads import Payload
from app.providers.gcp.client import gcp_list, location_path

RUN = "https://run.googleapis.com/v2"


def run_parent() -> str:
    return f"{RUN}/{location_path()}"


def services_url() -> str:
    return f"{run_parent()}/services"


def revisions_url(service: str) -> str:
    return f"{services_url()}/{service}/revisions"


def list_services_raw() -> tuple[bool, Payload]:
    """All Cloud Run services in the target region (complete list or error)."""
    return gcp_list(services_url(), "services", params={"pageSize": 100})


def short_name(name: str | None) -> str | None:
    """Last path segment of a resource name (``.../revisions/x`` → ``x``)."""
    return name.rsplit("/", 1)[-1] if name else name


def ready_condition(resource: dict[str, Any]) -> dict[str, Any]:
    """The ``Ready`` condition of a service or revision (empty when absent)."""
    return next(
        (c for c in resource.get("conditions") or [] if c.get("type") == "Ready"), {}
    )


def is_ready(condition: dict[str, Any]) -> bool:
    return condition.get("state") == "CONDITION_SUCCEEDED"
