"""Read-only, explicitly scoped Google Cloud Recommender recommendations."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from app.config import CONFIG
from app.core.redact import redact
from app.providers.gcp.client import gcp_list

RECOMMENDER = "https://recommender.googleapis.com"
MAX_PAGES = 5

# Cloud SQL documents the v1beta1 endpoint. IAM is global; Run/SQL are regional.
# https://docs.cloud.google.com/recommender/docs/recommenders
# https://docs.cloud.google.com/sql/docs/mysql/recommender-sql-idle
# https://docs.cloud.google.com/sql/docs/mysql/recommender-sql-overprovisioned
# https://docs.cloud.google.com/policy-intelligence/docs/review-apply-role-recommendations
_SOURCES = (
    ("google.run.service.CostRecommender", "region", "v1", "roles/run.viewer"),
    (
        "google.cloudsql.instance.IdleRecommender",
        "region",
        "v1beta1",
        "roles/recommender.cloudsqlViewer",
    ),
    (
        "google.cloudsql.instance.OverprovisionedRecommender",
        "region",
        "v1beta1",
        "roles/recommender.cloudsqlViewer",
    ),
    ("google.iam.policy.Recommender", "global", "v1", "roles/recommender.iamViewer"),
)
_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|credential|authorization|api.?key|private.?key)"
)
_SENSITIVE_PATH = re.compile(
    r"(?i)(/env(?:/|$)|/annotations(?:/|$)|password|secret|token|credential)"
)
_STATES = {"ACTIVE", "CLAIMED", "SUCCEEDED", "FAILED", "DISMISSED"}


def _safe(value: Any) -> Any:
    """Mask structured secrets as well as text, including untrusted dict keys."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            redact(str(key)): "[REDACTED]"
            if _SECRET_KEY.search(str(key))
            or str(key).casefold() in {"env", "envfrom", "environment"}
            else _safe(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def _impact(value: dict[str, Any]) -> dict[str, Any]:
    """Preserve API Money units/nanos and duration without float conversions."""
    out: dict[str, Any] = {
        "category": redact(str(value.get("category", "CATEGORY_UNSPECIFIED")))
    }
    cost = value.get("costProjection")
    if isinstance(cost, dict):
        projection: dict[str, Any] = {}
        invalid_fields = []
        if re.fullmatch(r"\d+(?:\.\d{1,9})?s", str(cost.get("duration", ""))):
            projection["duration"] = cost["duration"]
        for field in ("cost", "costInLocalCurrency"):
            if field not in cost:
                continue
            money = cost[field]
            if not isinstance(money, dict):
                invalid_fields.append(field)
                continue
            # Financial numeric strings are not PII. Validate the whole Money
            # before preserving it; dropping only a bad units/nanos component
            # would silently turn an invalid amount into a different amount.
            normalized: dict[str, Any] = {}
            valid = True
            if "currencyCode" in money:
                if isinstance(money["currencyCode"], str) and re.fullmatch(
                    r"[A-Z]{3}", money["currencyCode"]
                ):
                    normalized["currencyCode"] = money["currencyCode"]
                else:
                    valid = False
            if "units" in money:
                units = money["units"]
                if (
                    type(units) in (str, int)
                    and re.fullmatch(r"-?[0-9]{1,19}", str(units))
                    and -(2**63) <= int(units) < 2**63
                ):
                    normalized["units"] = units
                else:
                    valid = False
            if "nanos" in money:
                if type(money["nanos"]) is int and abs(money["nanos"]) < 1_000_000_000:
                    normalized["nanos"] = money["nanos"]
                else:
                    valid = False
            # Protobuf omits zero-valued components. An omitted units field
            # alongside valid nanos is zero, not an invalid/missing integer.
            if (
                valid
                and int(normalized.get("units", 0)) * normalized.get("nanos", 0) < 0
            ):
                valid = False
            projection[field] = normalized if valid else {}
            if not valid:
                invalid_fields.append(field)
        out["costProjection"] = projection
        out["cost_estimate_complete"] = bool(
            not invalid_fields
            and projection.get("duration")
            and projection.get("cost", {}).get("currencyCode")
            and any(key in projection.get("cost", {}) for key in ("units", "nanos"))
        )
        if invalid_fields:
            out["invalid_cost_fields"] = invalid_fields
    for key in (
        "securityProjection",
        "reliabilityProjection",
        "sustainabilityProjection",
    ):
        if isinstance(value.get(key), dict):
            out[key] = _safe(value[key])
    return out


def _recommendation(item: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    changes = []
    resources = set(item.get("targetResources") or [])
    for group in (item.get("content") or {}).get("operationGroups", []):
        operations = []
        for operation in group.get("operations", []):
            if operation.get("resource"):
                resources.add(operation["resource"])
            change = {
                key: operation[key]
                for key in (
                    "action",
                    "resourceType",
                    "resource",
                    "path",
                    "sourcePath",
                    "sourceResource",
                    "pathFilters",
                    "value",
                    "valueMatcher",
                )
                if key in operation
            }
            if _SENSITIVE_PATH.search(str(operation.get("path", ""))):
                for key in ("value", "valueMatcher"):
                    if key in change:
                        change[key] = "[REDACTED]"
            operations.append(_safe(change))
        changes.append({"operations": operations})
    impacts = []
    if isinstance(item.get("primaryImpact"), dict):
        impacts.append({"primary": True, **_impact(item["primaryImpact"])})
    for impact in item.get("additionalImpact", []):
        impacts.append({"primary": False, **_impact(impact)})
    out = _safe(
        {
            "name": item.get("name", ""),
            "source": source["url"],
            "recommender": source["recommender"],
            "location": source["location"],
            "state": "ACTIVE",
            "priority": item.get("priority", "PRIORITY_UNSPECIFIED"),
            "description": item.get("description", ""),
            "subtype": item.get("recommenderSubtype", ""),
            "last_refresh_time": item.get("lastRefreshTime"),
            "resources": sorted(resources),
            "recommended_changes": changes,
            "associated_insights": [
                insight["insight"]
                for insight in item.get("associatedInsights", [])
                if isinstance(insight, dict) and insight.get("insight")
            ],
            "exclusive_group": item.get("xorGroupId", ""),
        }
    )
    out["impacts"] = impacts
    return out


def list_optimization_recommendations() -> dict[str, Any]:
    """List ACTIVE Run/SQL cost recommendations in CONFIG.region and global IAM.

    No recommendation is applied, claimed or dismissed. ``complete`` covers
    only the listed sources, never all infrastructure or all project regions.
    A source with incomplete pagination contributes no raw partial records.
    """
    sources: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    for recommender, scope, version, role in _SOURCES:
        location = CONFIG.region if scope == "region" else "global"
        url = (
            f"{RECOMMENDER}/{version}/projects/{quote(CONFIG.target, safe='')}"
            f"/locations/{quote(location, safe='')}/recommenders/{recommender}/recommendations"
        )
        source: dict[str, Any] = {
            "recommender": recommender,
            "location": location,
            "url": url,
            "required_viewer_role": role,
        }
        try:
            ok, payload = gcp_list(
                url,
                "recommendations",
                params={"filter": "stateInfo.state = ACTIVE", "pageSize": 100},
                max_pages=MAX_PAGES,
            )
        except (AttributeError, TypeError, ValueError):
            ok, payload = (
                False,
                {"status": "error", "message": "Malformed recommendation response."},
            )
        rows: list[dict[str, Any]] = []
        if ok and isinstance(payload, list):
            try:
                for item in payload:
                    if (
                        not isinstance(item, dict)
                        or (item.get("stateInfo") or {}).get("state") not in _STATES
                    ):
                        raise ValueError("Missing recommendation state")
                    if item["stateInfo"]["state"] == "ACTIVE":
                        rows.append(_recommendation(item, source))
            except (AttributeError, KeyError, TypeError, ValueError):
                ok, payload = (
                    False,
                    {"status": "error", "message": "Malformed recommendation record."},
                )
        elif ok:
            ok, payload = (
                False,
                {"status": "error", "message": "Malformed recommendation list."},
            )
        if ok:
            source.update(status="ok", complete=True, count=len(rows))
            recommendations.extend(rows)
        else:
            error = payload if isinstance(payload, dict) else {}
            source.update(
                status=error.get("status", "error"),
                complete=False,
                message=redact(
                    str(error.get("message", "Recommendation retrieval failed."))
                ),
            )
            for key in ("http_status", "truncated", "incomplete"):
                if key in error:
                    source[key] = error[key]
        sources.append(source)
    completed = sum(source["complete"] for source in sources)
    status = "ok" if completed == len(sources) else "partial" if completed else "error"
    return {
        "status": status,
        "check": "optimization_recommendations",
        "complete": completed == len(sources),
        "scope": _safe(
            {
                "project": CONFIG.target,
                "region": CONFIG.region,
                "iam_location": "global",
            }
        ),
        "sources": _safe(sources),
        "recommendations": recommendations,
        "count": len(recommendations),
        "limitations": [
            "Only the listed recommenders and configured region were queried; other regions, VM zones, folders, organizations and billing accounts are outside this scope.",
            "No recommendations does not prove that resources are efficient or permissions are appropriate. Availability depends on usage history and recommender eligibility.",
            "Cost projections are approximate API estimates for the returned currency and duration; negative costs mean savings. Do not sum mutually exclusive recommendations.",
        ],
    }
