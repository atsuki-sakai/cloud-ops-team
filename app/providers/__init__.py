"""Provider registry.

Built-in providers are imported lazily by key. Third-party packages can add
providers without forking by exposing an entry point::

    [project.entry-points."cloud_ops_team.providers"]
    oci = "my_package.provider:OciProvider"

Select one with ``OPS_CLOUD_PROVIDER=<key>``.
"""

from __future__ import annotations

import functools
import importlib
from collections.abc import Callable
from importlib.metadata import entry_points

from app.config import CONFIG
from app.providers.base import CAPABILITIES, CloudProvider, ProviderProfile

ENTRY_POINT_GROUP = "cloud_ops_team.providers"
BUILTIN_PROVIDERS: dict[str, str] = {
    "gcp": "app.providers.gcp:GcpProvider",
    "aws": "app.providers.aws:AwsProvider",
    "azure": "app.providers.azure:AzureProvider",
}
_registered: dict[str, Callable[..., CloudProvider]] = {}

__all__ = [
    "CAPABILITIES",
    "CloudProvider",
    "ProviderProfile",
    "available_providers",
    "get_provider",
    "load_provider_class",
    "register_provider",
]


def register_provider(key: str, factory: Callable[..., CloudProvider]) -> None:
    """Register a provider factory at runtime (tests, embedding)."""
    _registered[key] = factory
    get_provider.cache_clear()


def _entry_point_providers() -> dict[str, str]:
    try:
        return {ep.name: ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)}
    except Exception:  # pragma: no cover - broken metadata should not break startup
        return {}


def available_providers() -> list[str]:
    return sorted({*BUILTIN_PROVIDERS, *_entry_point_providers(), *_registered})


def load_provider_class(key: str) -> Callable[..., CloudProvider]:
    if key in _registered:
        return _registered[key]
    spec = BUILTIN_PROVIDERS.get(key) or _entry_point_providers().get(key)
    if not spec:
        raise ValueError(
            f"Unknown cloud provider '{key}'. Available: {', '.join(available_providers())}. "
            "Set OPS_CLOUD_PROVIDER accordingly."
        )
    module_name, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module_name), attr)


@functools.cache
def get_provider() -> CloudProvider:
    """The provider selected by ``OPS_CLOUD_PROVIDER`` (default ``gcp``)."""
    return load_provider_class(CONFIG.provider)(
        target=CONFIG.target, region=CONFIG.region
    )
