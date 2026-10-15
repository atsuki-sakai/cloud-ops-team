"""Behavioral tests for scoped, read-only infrastructure recommendations."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.providers.gcp import client, optimization


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        optimization,
        "CONFIG",
        SimpleNamespace(target="test-project", region="asia-northeast1"),
    )


def recommendation(state="ACTIVE", **fields):
    return {
        "name": "projects/test-project/locations/asia-northeast1/recommenders/example/recommendations/one",
        "stateInfo": {"state": state},
        "lastRefreshTime": "2026-10-15T01:02:03.123456789Z",
        "priority": "P2",
        "description": "Consider a smaller machine after checking peak demand.",
        "content": {
            "operationGroups": [
                {
                    "operations": [
                        {
                            "action": "replace",
                            "resource": "//sqladmin.googleapis.com/projects/test-project/instances/db",
                            "resourceType": "sqladmin.googleapis.com/Instance",
                            "path": "/settings/tier",
                            "value": "db-custom-8-32768",
                        }
                    ]
                }
            ]
        },
        **fields,
    }


def one_source(monkeypatch, records):
    def get(url, *_args, **_kwargs):
        return True, records if "/google.run.service.CostRecommender/" in url else []

    monkeypatch.setattr(optimization, "gcp_list", get)


def test_only_active_recommendations_and_scoped_read_requests(monkeypatch):
    calls = []

    def get(url, key, **kwargs):
        calls.append((url, key, kwargs))
        return True, [
            recommendation(),
            recommendation("DISMISSED"),
            recommendation("CLAIMED"),
        ]

    monkeypatch.setattr(optimization, "gcp_list", get)
    result = optimization.list_optimization_recommendations()
    assert result["status"] == "ok" and result["complete"] is True
    assert len(result["recommendations"]) == 4
    assert all(row["state"] == "ACTIVE" for row in result["recommendations"])
    assert len(calls) == 4
    for url, key, kwargs in calls:
        assert key == "recommendations"
        assert kwargs["params"] == {
            "filter": "stateInfo.state = ACTIVE",
            "pageSize": 100,
        }
        assert kwargs.get("method", "GET") == "GET"
        assert "/projects/test-project/" in url
        if "google.iam.policy.Recommender" in url:
            assert "/v1/" in url and "/locations/global/" in url
        else:
            assert "/locations/asia-northeast1/" in url
        if "cloudsql" in url:
            assert "/v1beta1/" in url
        assert "/zones/" not in url


def test_preserves_exact_money_currency_duration_and_evidence(monkeypatch):
    projection = {
        "cost": {"currencyCode": "USD", "units": "-12", "nanos": -345678901},
        "costInLocalCurrency": {"currencyCode": "JPY", "units": "-2000", "nanos": 0},
        "duration": "2592000.123456789s",
    }
    item = recommendation(
        primaryImpact={"category": "COST", "costProjection": projection},
        additionalImpact=[
            {
                "category": "RELIABILITY",
                "reliabilityProjection": {"risks": ["SERVICE_DISRUPTION"]},
            }
        ],
        xorGroupId="alternative-options",
        associatedInsights=[
            {
                "insight": "projects/test-project/locations/global/insightTypes/type/insights/one"
            }
        ],
    )
    one_source(monkeypatch, [item])
    row = optimization.list_optimization_recommendations()["recommendations"][0]
    assert row["impacts"][0]["costProjection"] == projection
    assert row["impacts"][0]["cost_estimate_complete"] is True
    assert row["last_refresh_time"] == item["lastRefreshTime"]
    assert row["exclusive_group"] == "alternative-options"
    assert row["source"].endswith("google.run.service.CostRecommender/recommendations")
    assert row["resources"] == [
        "//sqladmin.googleapis.com/projects/test-project/instances/db"
    ]
    assert (
        row["recommended_changes"][0]["operations"][0]["value"] == "db-custom-8-32768"
    )
    assert row["associated_insights"]


@pytest.mark.parametrize(
    "projection",
    [
        {"cost": {"units": "-10"}, "duration": "3600s"},
        {"cost": {"currencyCode": "USD", "units": "-10"}},
        {"cost": {"currencyCode": "USD"}, "duration": "3600s"},
    ],
)
def test_missing_estimate_fields_are_never_invented(monkeypatch, projection):
    one_source(
        monkeypatch,
        [
            recommendation(
                primaryImpact={"category": "COST", "costProjection": projection}
            )
        ],
    )
    impact = optimization.list_optimization_recommendations()["recommendations"][0][
        "impacts"
    ][0]
    assert impact["costProjection"] == projection
    assert impact["cost_estimate_complete"] is False


def test_absent_cost_projection_is_not_a_zero_saving(monkeypatch):
    one_source(monkeypatch, [recommendation(primaryImpact={"category": "SECURITY"})])
    impact = optimization.list_optimization_recommendations()["recommendations"][0][
        "impacts"
    ][0]
    assert "costProjection" not in impact
    assert "cost_estimate_complete" not in impact


def test_large_money_units_remain_exact_after_redaction(monkeypatch):
    money = {"currencyCode": "USD", "units": "-1234567890123456", "nanos": -123456789}
    one_source(
        monkeypatch,
        [
            recommendation(
                primaryImpact={
                    "category": "COST",
                    "costProjection": {"cost": money, "duration": "3600s"},
                }
            )
        ],
    )
    impact = optimization.list_optimization_recommendations()["recommendations"][0][
        "impacts"
    ][0]
    assert impact["costProjection"]["cost"] == money
    assert impact["cost_estimate_complete"] is True


def test_partial_failure_retains_only_complete_sources(monkeypatch):
    def get(url, *_args, **_kwargs):
        if "google.iam.policy.Recommender" in url:
            return False, {
                "status": "permission_denied",
                "http_status": 403,
                "message": "Denied for user@example.com",
            }
        return True, [recommendation()]

    monkeypatch.setattr(optimization, "gcp_list", get)
    result = optimization.list_optimization_recommendations()
    assert result["status"] == "partial" and result["complete"] is False
    assert result["count"] == 3
    assert result["sources"][-1]["status"] == "permission_denied"
    assert result["sources"][-1]["complete"] is False
    assert "user@example.com" not in json.dumps(result)


def test_all_sources_failed_is_not_success(monkeypatch):
    monkeypatch.setattr(
        optimization,
        "gcp_list",
        lambda *_a, **_kw: (
            False,
            {"status": "api_disabled", "message": "Enable Recommender API"},
        ),
    )
    result = optimization.list_optimization_recommendations()
    assert result["status"] == "error" and result["complete"] is False
    assert result["count"] == 0
    assert all(source["status"] == "api_disabled" for source in result["sources"])


def test_successful_empty_is_scoped_and_not_evidence_of_no_waste(monkeypatch):
    monkeypatch.setattr(optimization, "gcp_list", lambda *_a, **_kw: (True, []))
    result = optimization.list_optimization_recommendations()
    assert result["status"] == "ok" and result["complete"] is True
    assert result["count"] == 0
    assert result["scope"]["region"] == "asia-northeast1"
    assert any(
        "No recommendations does not prove" in text for text in result["limitations"]
    )
    assert any("VM zones" in text for text in result["limitations"])


def test_pagination_uses_get_and_continues_empty_page_with_token(monkeypatch):
    calls = []

    def request(method, url, *, params, json):
        calls.append((method, url, dict(params), json))
        if "google.run.service.CostRecommender" not in url:
            return True, {}
        if not params.get("pageToken"):
            return True, {"recommendations": [], "nextPageToken": "next"}
        return True, {"recommendations": [recommendation()]}

    monkeypatch.setattr(client, "gcp_request", request)
    result = optimization.list_optimization_recommendations()
    assert result["status"] == "ok" and result["count"] == 1
    assert calls[1][2]["pageToken"] == "next"
    assert all(
        method == "GET" and body is None for method, _url, _params, body in calls
    )


@pytest.mark.parametrize("empty", [False, True])
def test_page_limit_is_partial_even_for_empty_pages(monkeypatch, empty):
    def request(_method, url, **_kwargs):
        if "google.run.service.CostRecommender" in url:
            return True, {
                "recommendations": [] if empty else [recommendation()],
                "nextPageToken": "opaque-secret-token",
            }
        return True, {}

    monkeypatch.setattr(client, "gcp_request", request)
    result = optimization.list_optimization_recommendations()
    assert result["status"] == "partial" and result["complete"] is False
    assert result["count"] == 0
    assert result["sources"][0]["truncated"] is True
    assert "opaque-secret-token" not in json.dumps(result)


def test_mid_pagination_failure_does_not_publish_partial_records(monkeypatch):
    def request(_method, url, *, params, **_kwargs):
        if "google.run.service.CostRecommender" not in url:
            return True, {}
        if not params.get("pageToken"):
            return True, {
                "recommendations": [recommendation(description="Bearer SECRET")],
                "nextPageToken": "next",
            }
        return False, {
            "status": "rate_limited",
            "message": "Retry later",
            "http_status": 429,
        }

    monkeypatch.setattr(client, "gcp_request", request)
    result = optimization.list_optimization_recommendations()
    assert result["status"] == "partial" and result["count"] == 0
    assert result["sources"][0]["status"] == "rate_limited"
    assert "SECRET" not in json.dumps(result)


def test_sensitive_content_and_metadata_are_not_exposed(monkeypatch):
    item = recommendation(
        description="user@example.com Bearer abcdefghijklmnop password=verysecret 8.8.8.8",
        content={
            "overview": {"unknown": "do-not-return-overview"},
            "operationGroups": [
                {
                    "operations": [
                        {
                            "path": "/spec/template/spec/containers/0/env/0/value",
                            "value": "plain-secret",
                        },
                        {
                            "path": "/binding",
                            "value": {
                                "member": "user:user@example.com",
                                "password": "nested-secret",
                            },
                        },
                    ]
                }
            ],
        },
        stateInfo={"state": "ACTIVE", "stateMetadata": {"token": "metadata-secret"}},
    )
    one_source(monkeypatch, [item])
    result = optimization.list_optimization_recommendations()
    rendered = json.dumps(result)
    for secret in (
        "user@example.com",
        "abcdefghijklmnop",
        "verysecret",
        "8.8.8.8",
        "plain-secret",
        "nested-secret",
        "metadata-secret",
        "do-not-return-overview",
    ):
        assert secret not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.parametrize("item", [{}, {"stateInfo": {"state": "UNKNOWN"}}, "invalid"])
def test_malformed_records_do_not_look_like_successful_empty(monkeypatch, item):
    one_source(monkeypatch, [item])
    result = optimization.list_optimization_recommendations()
    assert result["status"] == "partial"
    assert result["sources"][0]["complete"] is False
    assert result["count"] == 0


@pytest.mark.parametrize(
    "path,value",
    [
        (
            "/spec/template/spec/containers/0",
            {"env": [{"name": "API_TOKEN", "value": "nested-private-value"}]},
        ),
        (
            "/spec/template",
            {
                "spec": {
                    "containers": [
                        {
                            "env": [
                                {"name": "APP_SETTING", "value": "nested-private-value"}
                            ]
                        }
                    ]
                }
            },
        ),
        (
            "/spec/template",
            {
                "containers": [
                    {
                        "env": {"APP_SETTING": "nested-private-value"},
                        "image": "example/image:v1",
                    }
                ]
            },
        ),
    ],
)
@pytest.mark.parametrize("value_field", ["value", "valueMatcher"])
def test_parent_resource_replacements_do_not_expose_nested_environment(
    monkeypatch, path, value, value_field
):
    item = recommendation(
        content={
            "operationGroups": [
                {
                    "operations": [
                        {"action": "replace", "path": path, value_field: value}
                    ]
                }
            ]
        }
    )
    one_source(monkeypatch, [item])
    result = optimization.list_optimization_recommendations()
    assert result["count"] == 1
    rendered = json.dumps(result)
    assert "nested-private-value" not in rendered
    assert "[REDACTED]" in rendered


@pytest.mark.parametrize(
    "money",
    [
        {"currencyCode": "USD", "units": "bad", "nanos": -500000000},
        {"currencyCode": "USD", "units": None, "nanos": -500000000},
        {"currencyCode": "USD", "units": True, "nanos": 0},
        {"currencyCode": "USD", "units": "-12", "nanos": "bad"},
        {"currencyCode": "USD", "units": "-12", "nanos": 1000000000},
        {"currencyCode": "USD", "units": "1", "nanos": -1},
        {"currencyCode": "USD", "units": "-1", "nanos": 1},
        {"currencyCode": "USD", "units": "9223372036854775808", "nanos": 0},
        {"currencyCode": "usd", "units": "1", "nanos": 0},
    ],
)
@pytest.mark.parametrize("field", ["cost", "costInLocalCurrency"])
def test_invalid_money_components_never_form_a_partial_estimate(
    monkeypatch, money, field
):
    projection = {
        "cost": {"currencyCode": "USD", "units": "-12", "nanos": 0},
        "duration": "2592000s",
        field: money,
    }
    one_source(
        monkeypatch,
        [
            recommendation(
                primaryImpact={"category": "COST", "costProjection": projection}
            )
        ],
    )
    impact = optimization.list_optimization_recommendations()["recommendations"][0][
        "impacts"
    ][0]
    assert impact["cost_estimate_complete"] is False
    assert impact["invalid_cost_fields"] == [field]
    assert impact["costProjection"][field] == {}


@pytest.mark.parametrize(
    "money",
    [
        {"currencyCode": "USD", "units": "0", "nanos": -500000000},
        {"currencyCode": "USD", "nanos": -500000000},
        {"currencyCode": "USD", "units": "0"},
        {"currencyCode": "USD", "nanos": 0},
        {"currencyCode": "USD", "units": 0, "nanos": 500000000},
        {"currencyCode": "USD", "units": "-9223372036854775808", "nanos": 0},
    ],
)
def test_zero_or_omitted_zero_money_components_remain_valid(monkeypatch, money):
    projection = {"cost": money, "duration": "3600s"}
    one_source(
        monkeypatch,
        [
            recommendation(
                primaryImpact={"category": "COST", "costProjection": projection}
            )
        ],
    )
    impact = optimization.list_optimization_recommendations()["recommendations"][0][
        "impacts"
    ][0]
    assert impact["costProjection"] == projection
    assert impact["cost_estimate_complete"] is True
    assert "invalid_cost_fields" not in impact
