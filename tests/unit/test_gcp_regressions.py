"""GCP response regressions with synthetic API responses, no cloud access."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.fingerprint import fingerprint, message_template
from app.core.redact import redact
from app.providers.gcp import client
from app.providers.gcp import observability as obs
from app.providers.gcp import security as sec


@pytest.fixture(autouse=True)
def forbid_cloud_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("This regression test must not access credentials or the network")

    monkeypatch.setattr(client, "_access_token", forbidden)
    monkeypatch.setattr(client.httpx, "request", forbidden)
    config = SimpleNamespace(target="test-project", region="test-region")
    for module in (client, obs, sec):
        monkeypatch.setattr(module, "CONFIG", config)


def log_entry(message: str) -> dict:
    return {
        "timestamp": "2026-10-14T00:00:00Z",
        "severity": "ERROR",
        "textPayload": message,
        "resource": {"type": "cloud_run_revision", "labels": {"service_name": "api"}},
    }


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_pagination_continues_after_empty_pages(monkeypatch, method):
    pages = iter(
        [
            {"nextPageToken": "page-2"},
            {"entries": [], "nextPageToken": "page-3"},
            {"entries": [{"message": "found"}]},
        ]
    )
    tokens = []

    def request(_method, _url, *, params, json):
        tokens.append((params if method == "GET" else json).get("pageToken"))
        return True, next(pages)

    monkeypatch.setattr(client, "gcp_request", request)
    ok, entries = client.gcp_list(
        "https://example.invalid", "entries", method=method, max_pages=3
    )
    assert ok and entries == [{"message": "found"}]
    assert tokens == [None, "page-2", "page-3"]


def test_unfinished_pagination_is_not_success_or_raw_partial_data(monkeypatch):
    pages = iter(
        [
            {
                "entries": [{"message": "password=SyntheticPrivateValue"}],
                "nextPageToken": "page-2",
            },
            {"nextPageToken": "page-3"},
        ]
    )
    monkeypatch.setattr(
        client, "gcp_request", lambda *args, **kwargs: (True, next(pages))
    )
    ok, payload = client.gcp_list("https://example.invalid", "entries", max_pages=2)
    assert not ok
    assert payload["status"] == "error" and payload["complete"] is False
    assert payload["truncated"] is True and payload["count"] == 1
    assert payload["next_page_token"] == "page-3"
    assert "SyntheticPrivateValue" not in json.dumps(payload)


def test_unfinished_empty_log_search_does_not_report_resolved(monkeypatch):
    monkeypatch.setattr(
        client, "gcp_request", lambda *args, **kwargs: (True, {"nextPageToken": "more"})
    )
    out = obs.count_matching_errors("api", "connection failed", "2026-10-01T00:00:00Z")
    assert out["status"] == "error"
    assert out["count"] == 0 and out["capped"] is True and out["complete"] is False


def test_completed_empty_log_search_is_a_confirmed_zero(monkeypatch):
    monkeypatch.setattr(client, "gcp_request", lambda *args, **kwargs: (True, {}))
    out = obs.count_matching_errors("api", "connection failed", "2026-10-01T00:00:00Z")
    assert out["status"] == "ok"
    assert out["count"] == 0 and out["capped"] is False and out["complete"] is True


@pytest.mark.parametrize("preview", ["errors", "context"])
@pytest.mark.parametrize("token", [None, "next-page"])
def test_bounded_log_previews_return_entries_and_completion_metadata(
    monkeypatch, preview, token
):
    limit = 2 if preview == "errors" else 80
    entries = [log_entry("request failed password=SyntheticPrivateValue")] * limit

    def request(method, url, **kwargs):
        assert method == "POST" and url.endswith("/entries:list")
        assert kwargs["json"]["pageSize"] == limit
        return True, {"entries": entries, "nextPageToken": token}

    monkeypatch.setattr(client, "gcp_request", request)
    if preview == "errors":
        out = obs.list_error_logs(24, "api", limit)
    else:
        out = obs.get_log_context("api", "2026-10-14T00:00:00Z", 5, "")
    assert out["status"] == "ok" and out["count"] == limit
    assert out["truncated"] is bool(token) and out["complete"] is (not token)
    assert out["next_page_token"] == token
    assert "SyntheticPrivateValue" not in json.dumps(out)


@pytest.mark.parametrize("preview", ["errors", "context"])
def test_empty_unfinished_log_preview_is_explicit(monkeypatch, preview):
    monkeypatch.setattr(
        client, "gcp_request", lambda *args, **kwargs: (True, {"nextPageToken": "more"})
    )
    if preview == "errors":
        out = obs.list_error_logs(24, "api", 2)
    else:
        out = obs.get_log_context("api", "2026-10-14T00:00:00Z", 5, "")
    assert out["status"] == "ok" and out["count"] == 0
    assert out["truncated"] is True and out["complete"] is False


@pytest.mark.parametrize("token", [None, "next-page"])
def test_bounded_revision_preview_keeps_limit_and_continuation(monkeypatch, token):
    revisions = [
        {"name": "middle", "createTime": "2026-10-13T00:00:00Z"},
        {"name": "newest", "createTime": "2026-10-14T00:00:00Z"},
    ]

    def request(method, url, **kwargs):
        assert method == "GET" and url.endswith("/services/api/revisions")
        assert kwargs["params"]["pageSize"] == 2
        return True, {"revisions": revisions, "nextPageToken": token}

    monkeypatch.setattr(client, "gcp_request", request)
    out = obs.get_cloud_run_revisions("api", 2)
    assert out["status"] == "ok" and out["count"] == 2
    assert [r["name"] for r in out["revisions"]] == ["newest", "middle"]
    assert out["truncated"] is bool(token) and out["complete"] is (not token)
    assert out["next_page_token"] == token


def test_group_masks_all_public_text_and_preserves_fingerprint(monkeypatch):
    message = "login failed password=SuperSecretAlphaOmega user=alice@example.com"
    entry = log_entry(message)
    monkeypatch.setattr(obs, "_list_entries", lambda *args, **kwargs: (True, [entry]))
    out = obs.summarize_error_groups(24)
    group = out["groups"][0]
    public = json.dumps(out).lower()
    assert "supersecretalphaomega" not in public and "alice@example.com" not in public
    assert group["fingerprint"] == fingerprint(
        "cloud_run_revision", "api", message_template(message)
    )
    assert group["search_text_available"] is True
    assert group["search_text"] in message
    assert redact(group["search_text"]) == group["search_text"]

    def request(_method, _url, **kwargs):
        assert group["search_text"] in kwargs["json"]["filter"]
        return True, {"entries": [entry]}

    monkeypatch.setattr(client, "gcp_request", request)
    recurrence = obs.count_matching_errors(
        "api", group["search_text"], "2026-10-01T00:00:00Z"
    )
    assert recurrence["status"] == "ok" and recurrence["count"] == 1


@pytest.mark.parametrize(
    "message, private",
    [
        (
            "request failed Bearer SyntheticBearerCredential",
            "SyntheticBearerCredential",
        ),
        (
            "request failed ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        ),
        ("request failed sk_live_ABCDEFGH12345678", "sk_live_ABCDEFGH12345678"),
    ],
)
def test_group_masks_tokens_before_template_normalization(
    monkeypatch, message, private
):
    monkeypatch.setattr(
        obs, "_list_entries", lambda *args, **kwargs: (True, [log_entry(message)])
    )
    out = obs.summarize_error_groups(24)
    assert private.lower() not in json.dumps(out).lower()
    assert out["groups"][0]["search_text"] in message


def test_safe_request_search_path_is_kept(monkeypatch):
    entry = log_entry("request failed password=SyntheticPrivateValue")
    entry["httpRequest"] = {
        "requestUrl": "https://api.example/api/v1/jobs/12345/run?token=SyntheticPrivateValue"
    }
    monkeypatch.setattr(obs, "_list_entries", lambda *args, **kwargs: (True, [entry]))
    assert obs.summarize_error_groups(24)["groups"][0]["search_text"] == "/api/v1/jobs"


def test_no_safe_search_literal_is_reported_and_not_broadened(monkeypatch):
    monkeypatch.setattr(
        obs,
        "_list_entries",
        lambda *args, **kwargs: (
            True,
            [log_entry("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456")],
        ),
    )
    group = obs.summarize_error_groups(24)["groups"][0]
    assert group["search_text"] == "" and group["search_text_available"] is False
    out = obs.count_matching_errors("api", group["search_text"], "2026-10-01T00:00:00Z")
    assert out["status"] == "error" and out["complete"] is False


@pytest.mark.parametrize(
    "source", [{}, {"sourceRanges": ["0.0.0.0/0"]}, {"sourceRanges": ["::/0"]}]
)
def test_firewall_detects_implicit_and_explicit_public_ssh(monkeypatch, source):
    rule = {
        "name": "ssh",
        "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}],
        **source,
    }
    monkeypatch.setattr(sec, "gcp_list", lambda *args, **kwargs: (True, [rule]))
    monkeypatch.setattr(
        sec,
        "gcp_get",
        lambda *args, **kwargs: (True, {"items": {"zone": {"instances": [{}]}}}),
    )
    findings = sec.audit_firewall_rules()["findings"]
    assert [(f["check_id"], f["severity"]) for f in findings] == [
        ("fw.sensitive_port", "high")
    ]


@pytest.mark.parametrize(
    "source",
    [
        {"sourceTags": ["internal"]},
        {"sourceServiceAccounts": ["internal@example.iam.gserviceaccount.com"]},
        {"sourceRanges": ["10.0.0.0/8"]},
    ],
)
def test_firewall_does_not_treat_restricted_sources_as_public(monkeypatch, source):
    rule = {
        "name": "ssh",
        "direction": "INGRESS",
        "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}],
        **source,
    }
    monkeypatch.setattr(sec, "gcp_list", lambda *args, **kwargs: (True, [rule]))
    monkeypatch.setattr(sec, "gcp_get", lambda *args, **kwargs: (True, {"items": {}}))
    assert sec.audit_firewall_rules()["findings"] == []


@pytest.mark.parametrize("protocol", ["tcp", "6", "udp", "17", "all"])
def test_firewall_normalizes_numeric_protocols_with_all_ports(monkeypatch, protocol):
    rule = {
        "name": "all-ports",
        "direction": "INGRESS",
        "sourceRanges": ["0.0.0.0/0"],
        "allowed": [{"IPProtocol": protocol}],
    }
    monkeypatch.setattr(sec, "gcp_list", lambda *args, **kwargs: (True, [rule]))
    monkeypatch.setattr(
        sec,
        "gcp_get",
        lambda *args, **kwargs: (True, {"items": {"zone": {"instances": [{}]}}}),
    )
    assert sec.audit_firewall_rules()["findings"][0]["check_id"] == "fw.all_ports"


@pytest.mark.parametrize(
    "version, backup, enabled",
    [
        ("MYSQL_8_0", {"binaryLogEnabled": True}, True),
        (
            "MYSQL_8_0",
            {"binaryLogEnabled": False, "pointInTimeRecoveryEnabled": True},
            False,
        ),
        ("POSTGRES_16", {"pointInTimeRecoveryEnabled": True}, True),
        (
            "POSTGRES_16",
            {"pointInTimeRecoveryEnabled": False, "binaryLogEnabled": True},
            False,
        ),
    ],
)
def test_pitr_uses_engine_specific_setting(monkeypatch, version, backup, enabled):
    instance = {
        "name": "database",
        "databaseVersion": version,
        "settings": {"backupConfiguration": {"enabled": True, **backup}},
    }
    monkeypatch.setattr(
        sec, "gcp_get", lambda *args, **kwargs: (True, {"items": [instance]})
    )
    assert sec.get_cloud_sql_instances()["instances"][0]["pitr_enabled"] is enabled
    findings = sec.audit_cloud_sql()["findings"]
    assert ("sql.pitr" in {f["check_id"] for f in findings}) is (not enabled)


def service(name="api", traffic=None):
    path = f"projects/test-project/locations/test-region/services/{name}"
    return {
        "name": path,
        "latestReadyRevision": f"{path}/revisions/new",
        "trafficStatuses": traffic
        if traffic is not None
        else [{"revision": "old", "percent": 90}, {"revision": "new", "percent": 10}],
    }


def test_image_scan_covers_split_traffic_and_deduplicates_images(monkeypatch):
    services = [
        service(),
        service("worker", [{"revision": "same-image", "percent": 100}]),
    ]
    monkeypatch.setattr(sec, "_run_services_raw", lambda: (True, services))
    revisions_read, images_scanned = [], []

    def get(url, **kwargs):
        revisions_read.append(url.rsplit("/", 1)[-1])
        digest = "sha256:old" if url.endswith("/old") else "sha256:new"
        return True, {
            "containers": [{"image": "registry.example:5000/repo/app:tag"}],
            "containerStatuses": [{"imageDigest": digest}],
        }

    def list_images(url, item_key, **kwargs):
        images_scanned.append(kwargs["params"]["filter"])
        return True, []

    monkeypatch.setattr(sec, "gcp_get", get)
    monkeypatch.setattr(sec, "gcp_list", list_images)
    out = sec.scan_deployed_images()
    assert out["status"] == "ok" and out["complete"] is True
    assert set(revisions_read) == {"old", "new", "same-image"}
    assert len(images_scanned) == 2
    by_image = {i["image"]: i["used_by"] for i in out["images"]}
    assert by_image["registry.example:5000/repo/app@sha256:old"] == ["api"]
    assert by_image["registry.example:5000/repo/app@sha256:new"] == ["api", "worker"]


@pytest.mark.parametrize("failure", ["permission_denied", "missing_digest"])
def test_image_scan_reports_unchecked_revisions_as_unknown(monkeypatch, failure):
    monkeypatch.setattr(sec, "_run_services_raw", lambda: (True, [service()]))

    def get(url, **kwargs):
        if url.endswith("/old"):
            if failure == "permission_denied":
                return False, {
                    "status": "permission_denied",
                    "message": "Access denied",
                }
            return True, {"containers": [{"image": "repo/app:tag"}]}
        return True, {"containers": [{"image": "repo/app@sha256:new"}]}

    monkeypatch.setattr(sec, "gcp_get", get)
    monkeypatch.setattr(sec, "gcp_list", lambda *args, **kwargs: (True, []))
    out = sec.scan_deployed_images()
    assert out["status"] == "error" and out["complete"] is False
    assert out["image_count"] == 1
    assert out["errors"][0]["revision"].endswith("/old")
    assert out["errors"][0]["status"] in ("permission_denied", "unknown")


def test_image_scan_includes_tagged_revision_and_pinned_sidecar(monkeypatch):
    traffic = [
        {"revision": "active", "percent": 100},
        {"revision": "tagged", "percent": 0, "tag": "preview"},
        {"revision": "inactive", "percent": 0},
    ]
    monkeypatch.setattr(
        sec, "_run_services_raw", lambda: (True, [service(traffic=traffic)])
    )
    read = []

    def get(url, **kwargs):
        read.append(url.rsplit("/", 1)[-1])
        return True, {
            "containers": [
                {"name": "main", "image": "repo/main:tag"},
                {"name": "sidecar", "image": "repo/sidecar@sha256:sidecar"},
            ],
            "containerStatuses": [{"name": "main", "imageDigest": "sha256:main"}],
        }

    monkeypatch.setattr(sec, "gcp_get", get)
    monkeypatch.setattr(sec, "gcp_list", lambda *args, **kwargs: (True, []))
    out = sec.scan_deployed_images()
    assert set(read) == {"active", "tagged"}
    assert out["status"] == "ok" and out["image_count"] == 2


def test_image_scan_failure_is_not_reported_as_zero_vulnerabilities(monkeypatch):
    monkeypatch.setattr(
        sec,
        "_run_services_raw",
        lambda: (True, [service(traffic=[{"revision": "new", "percent": 100}])]),
    )
    monkeypatch.setattr(
        sec,
        "gcp_get",
        lambda *args, **kwargs: (
            True,
            {"containers": [{"image": "repo/app@sha256:new"}]},
        ),
    )
    monkeypatch.setattr(
        sec,
        "gcp_list",
        lambda *args, **kwargs: (
            False,
            {"status": "api_disabled", "message": "API disabled"},
        ),
    )
    out = sec.scan_deployed_images()
    assert out["status"] == "error" and out["complete"] is False
    assert out["images"][0]["scanned"] is False and "total" not in out["images"][0]
    assert out["errors"][0]["status"] == "api_disabled"
