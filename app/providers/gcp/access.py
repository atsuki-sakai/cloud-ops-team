"""Bounded, read-only investigation of security signals in Cloud Logging.

Raw log content never leaves this module. Identity references are project-scoped
SHA-256 pseudonyms, not anonymization; operators can correlate their own logs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import re
from collections import Counter
from typing import Any
from urllib.parse import unquote, urlsplit

from app.core.findings import finding, result
from app.core.redact import redact
from app.core.timeutil import iso_utc, parse_time, utcnow
from app.providers.gcp.client import gcp_post, project_path

MAX_PAGES = 5
PAGE_SIZE = 200
HTTP_DENIED_MAX_PAGES = 30
HTTP_DENIED_PAGE_SIZE = 1000
MAX_FINDINGS = 100
AUDIT_TYPE = "type.googleapis.com/google.cloud.audit.AuditLog"
_METHOD_PATTERN = "(?i)(setiampolicy|createserviceaccountkey|uploadserviceaccountkey|deletesink|updatesink|createexclusion|updateexclusion)$"
_ARMOR_FILTER = (
    '(jsonPayload.enforcedSecurityPolicy.outcome="DENY" OR '
    'jsonPayload.enforcedEdgeSecurityPolicy.outcome="DENY" OR '
    'jsonPayload.statusDetails="denied_by_security_policy")'
)
_FILTERS = {
    "audit": (
        f'protoPayload.@type="{AUDIT_TYPE}" AND '
        "(protoPayload.status.code=7 OR protoPayload.status.code=16 OR "
        "protoPayload.authorizationInfo.granted=false OR "
        f'protoPayload.methodName=~"{_METHOD_PATTERN}")'
    ),
    # Keep probes independent of high-volume authentication failures. The two
    # searches can overlap; investigation deduplicates observed HTTP events.
    "http_probe": (
        'httpRequest.requestUrl=~"(?i)([.]env|[.]git|wp-login[.]php|phpmyadmin|[.][.]/|%2e|%2f)"'
    ),
    "http_denied": "(httpRequest.status=401 OR httpRequest.status=403)",
    "armor": _ARMOR_FILTER,
}
_LIMITATIONS = [
    "定期的なログ調査であり即時検知ではない。侵害の有無はこの調査だけでは確定できない。",
    "Data Access監査ログ、HTTPリクエストログ、Cloud Armorログは無効・未設定・サンプリング・除外・保持期限切れの可能性がある。有効化設定と収集網羅性は未確認。",
    "対象プロジェクトで閲覧可能なログの選択した兆候のみを検索する。ゼロ件は安全を意味せず、別プロジェクトへの集約や閲覧権限不足・到着遅延も確認する。",
    "主体参照はプロジェクト単位の決定的ハッシュ。共有IPやプロキシを同一利用者と断定しない。既定の件数閾値はベースライン学習やリクエスト率の評価ではない。",
]
_ERROR_STATUSES = {
    "api_disabled",
    "permission_denied",
    "not_found",
    "rate_limited",
    "error",
}
_HINT_KEYS = {
    "cloud_run_revision": ("service_name", "location"),
    "cloud_run_job": ("job_name", "location"),
    "gce_instance": ("instance_id", "zone"),
    "k8s_container": ("cluster_name", "namespace_name"),
    "k8s_cluster": ("cluster_name", "location"),
    "http_load_balancer": ("backend_service_name", "forwarding_rule_name"),
}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _reference(kind: str, value: Any) -> str:
    raw = json.dumps([project_path(), kind, value], sort_keys=True, ensure_ascii=True)
    return f"{kind}-{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def _event_reference(entry: dict[str, Any]) -> str:
    identity = (
        [entry.get("logName"), entry["insertId"], entry["timestamp"]]
        if entry.get("insertId")
        else entry
    )
    return _reference("entry", identity)


def _resource(entry: dict[str, Any]) -> tuple[str, dict[str, str]]:
    resource = _mapping(entry.get("resource"))
    labels = _mapping(resource.get("labels"))
    resource_type = resource.get("type", "")
    if not isinstance(resource_type, str):
        resource_type = ""
    hints = {
        key: labels[key]
        for key in _HINT_KEYS.get(resource_type, ())
        if isinstance(labels.get(key), str)
        and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}", labels[key])
        and redact(labels[key]) == labels[key]
    }
    # Cloud Run revisions are deployments of one service, not separate incidents.
    identity = (
        {"type": resource_type, "hints": hints}
        if resource_type == "cloud_run_revision" and "service_name" in hints
        else resource
    )
    audit_resource = _mapping(entry.get("protoPayload")).get("resourceName")
    return _reference("resource", [identity, audit_resource]), hints


def _subject(entry: dict[str, Any]) -> tuple[str, str]:
    audit = _mapping(entry.get("protoPayload"))
    auth = _mapping(audit.get("authenticationInfo"))
    principal = auth.get("principalSubject") or auth.get("principalEmail")
    if isinstance(principal, str) and principal and principal != "redacted":
        return _reference("principal", principal), "principal"
    raw_ip = _mapping(audit.get("requestMetadata")).get("callerIp") or _mapping(
        entry.get("httpRequest")
    ).get("remoteIp")
    if isinstance(raw_ip, str):
        try:
            return _reference("source", str(ipaddress.ip_address(raw_ip))), "source_ip"
        except ValueError:
            pass
    return "unknown", "unknown"


def _probe_category(url: Any) -> str:
    if not isinstance(url, str):
        return ""
    try:
        path = unquote(urlsplit(url).path).lower()
    except ValueError:
        return ""
    if re.search(r"(?:^|/)\.env(?:[./]|$)", path):
        return "environment_file"
    if re.search(r"(?:^|/)\.git(?:/|$)", path):
        return "git_metadata"
    if "../" in path:
        return "path_traversal"
    if re.search(r"(?:^|/)(wp-login\.php|phpmyadmin)(?:/|$)", path):
        return "admin_login_probe"
    return ""


def _armor_denied(entry: dict[str, Any]) -> bool:
    payload = _mapping(entry.get("jsonPayload"))
    return (
        any(
            _mapping(payload.get(key)).get("outcome") == "DENY"
            for key in ("enforcedSecurityPolicy", "enforcedEdgeSecurityPolicy")
        )
        or payload.get("statusDetails") == "denied_by_security_policy"
    )


def _signals(source: str, entry: dict[str, Any]) -> list[tuple[str, str]]:
    """Return only constant categories; never return attacker-controlled text."""
    if source == "armor":
        return (
            [("access_armor_denied", "enforced_policy_denial")]
            if _armor_denied(entry)
            else []
        )
    if source in ("http_probe", "http_denied"):
        if _armor_denied(entry):
            return []  # Already investigated in the independent Armor search.
        http = _mapping(entry.get("httpRequest"))
        probe = _probe_category(http.get("requestUrl"))
        if probe:
            return [("access_http_probe", probe)]
        return (
            [("access_http_denied", "http_401_403")]
            if http.get("status") in (401, 403)
            else []
        )
    audit = _mapping(entry.get("protoPayload"))
    if audit.get("@type") != AUDIT_TYPE:
        return []
    code = _mapping(audit.get("status")).get("code", 0)
    authorizations = audit.get("authorizationInfo")
    denied = code in (7, 16) or any(
        _mapping(item).get("granted") is False
        for item in (authorizations if isinstance(authorizations, list) else [])
    )
    if denied:
        return [("access_audit_denied", "audit_permission_or_authentication_denied")]
    method = audit.get("methodName", "")
    if not isinstance(method, str) or code != 0:
        return []
    if method.lower().endswith("setiampolicy"):
        # A requested policy alone does not prove that a new binding was granted.
        for field in ("metadata", "serviceData"):
            deltas = _mapping(_mapping(audit.get(field)).get("policyDelta")).get(
                "bindingDeltas"
            )
            if isinstance(deltas, list) and any(
                _mapping(item).get("action") == "ADD"
                and _mapping(item).get("member")
                in ("allUsers", "allAuthenticatedUsers")
                for item in deltas
            ):
                return [("access_public_iam_added", "public_iam_binding_added")]
        return [("access_sensitive_operation", "iam_policy_changed")]
    if re.search(r"(?i)(create|upload)serviceaccountkey$", method):
        return [
            ("access_sensitive_operation", "service_account_key_created_or_uploaded")
        ]
    if re.search(
        r"(?i)(deletesink|updatesink|createexclusion|updateexclusion)$", method
    ):
        return [("access_sensitive_operation", "logging_configuration_changed")]
    return []


def _fetch(
    source: str, start: dt.datetime, end: dt.datetime
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    filter_ = f'timestamp >= "{iso_utc(start)}" AND timestamp < "{iso_utc(end)}" AND ({_FILTERS[source]})'
    max_pages = HTTP_DENIED_MAX_PAGES if source == "http_denied" else MAX_PAGES
    page_size = HTTP_DENIED_PAGE_SIZE if source == "http_denied" else PAGE_SIZE
    body: dict[str, Any] = {
        "resourceNames": [project_path()],
        "filter": filter_,
        "orderBy": "timestamp desc",
        "pageSize": page_size,
    }
    coverage: dict[str, Any] = {
        "source": source,
        "status": "ok",
        "complete": False,
        "pages": 0,
        "max_pages": max_pages,
        "page_size": page_size,
        "fetched_entries": 0,
        "usable_entries": 0,
        "invalid_entries": 0,
        "outside_window": 0,
        "duplicate_entries": 0,
        "logging_configuration": "unverified",
        "filter": filter_,
    }
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(max_pages):
        ok, payload = gcp_post(
            "https://logging.googleapis.com/v2/entries:list", json=dict(body)
        )
        coverage["pages"] = page + 1
        if not ok:
            status = _mapping(payload).get("status")
            coverage["status"] = (
                status
                if isinstance(status, str) and status in _ERROR_STATUSES
                else "error"
            )
            coverage["reason"] = (
                "ログ取得に失敗。API有効化、閲覧権限、認証、割当制限を確認する。"
            )
            break
        if (
            not isinstance(payload, dict)
            or "raw" in payload
            or not isinstance(payload.get("entries", []), list)
        ):
            coverage.update(status="error", reason="ログAPIの応答形式を解釈できない。")
            break
        entries = payload.get("entries", [])
        coverage["fetched_entries"] += len(entries)
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("timestamp"), str):
                coverage["invalid_entries"] += 1
                continue
            time = parse_time(item["timestamp"])
            if time is None:
                coverage["invalid_entries"] += 1
                continue
            if not start <= time < end:
                coverage["outside_window"] += 1
                continue
            if item.get("insertId"):
                key = _event_reference(item)
                if key in seen:
                    coverage["duplicate_entries"] += 1
                    continue
                seen.add(key)
            rows.append(item)
        token = payload.get("nextPageToken")
        if not token:
            coverage["complete"] = True
            break
        if not isinstance(token, str):
            coverage.update(
                status="error", reason="継続ページ情報の形式を解釈できない。"
            )
            break
        body["pageToken"] = token
    else:
        coverage.update(
            status="partial", reason="ページ上限到達。検索未完了で件数は下限値。"
        )
    if coverage["invalid_entries"] and coverage["status"] == "ok":
        coverage.update(
            status="partial",
            complete=False,
            reason="時刻不明などで解析できないログがある。",
        )
    if coverage["outside_window"]:
        coverage["window_note"] = (
            "API応答に含まれた対象期間外のログを集計から除外した。"
        )
    coverage["usable_entries"] = len(rows)
    coverage["observation"] = (
        "matching_logs_observed" if rows else "no_usable_matching_logs"
    )
    return rows, coverage


def _make_finding(check_id: str, group: dict[str, Any]) -> dict[str, Any]:
    count = group["count"]
    known = group["subject_kind"] != "unknown"
    threshold = 10 if check_id == "access_http_probe" else 20
    if check_id == "access_public_iam_added":
        severity, title = "high", "公開主体へのIAM権限追加が記録された"
    elif check_id == "access_sensitive_operation":
        severity, title = "medium", "IAM・認証鍵・ログ構成の重要操作が記録された"
    else:
        severity = (
            "high"
            if known and count >= threshold
            else "medium"
            if count >= threshold or check_id == "access_http_probe"
            else "info"
        )
        title = {
            "access_http_probe": "機密ファイル・管理画面の探索候補",
            "access_http_denied": "HTTP認証・アクセス拒否の観測",
            "access_audit_denied": "クラウドAPIの認証・権限拒否の観測",
            "access_armor_denied": "Cloud Armorによる拒否の観測",
        }[check_id]
    resource = f"{group['resource_ref']}/{group['subject_ref']}"
    item = finding(
        check_id,
        severity,
        title,
        resource,
        f"{count}件。{group['first_seen']} 〜 {group['last_seen']}。ログに記録された事実であり、不正アクセス成功・侵害を示す確証ではない。",
        "対象期間の元ログと変更申請・サービス仕様を照合し、主体と対象を確認する。誤設定・正当な操作・共有IPの可能性を確認する。不正と確認できた場合の認証情報失効や権限見直し・WAF調整は担当者への提案に留め、変更前後の確認と復旧手順を用意する。",
    )
    item["evidence"] = {
        **group,
        "categories": dict(sorted(group["categories"].items())),
    }
    if "http_status_counts" in group:
        item["evidence"]["http_status_counts"] = dict(
            sorted(group["http_status_counts"].items())
        )
    return item


def investigate_access(hours: int) -> dict[str, Any]:
    """Investigate recorded access signals over 1-168 hours without cloud writes."""
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 168:
        return {
            "status": "error",
            "check": "access_investigation",
            "message": "hours must be an integer between 1 and 168",
            "coverage": [],
            "limitations": list(_LIMITATIONS),
        }
    end = utcnow().replace(microsecond=0)
    start = end - dt.timedelta(hours=hours)
    coverage = []
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    http_sources: dict[str, str] = {}
    for source in _FILTERS:
        rows, observed = _fetch(source, start, end)
        coverage.append(observed)
        for entry in rows:
            if source in ("http_probe", "http_denied"):
                # Preserve the first observation even if its query later fails.
                # With no insertId, compare the complete event across searches;
                # repeated entries within one search retain their prior meaning.
                event = _event_reference(entry)
                previous_source = http_sources.setdefault(event, source)
                if previous_source != source:
                    observed["cross_search_duplicates"] = (
                        observed.get("cross_search_duplicates", 0) + 1
                    )
                    continue
            resource, hints = _resource(entry)
            subject, kind = _subject(entry)
            timestamp = iso_utc(parse_time(entry["timestamp"]))
            for check_id, category in _signals(source, entry):
                group = groups.setdefault(
                    (check_id, resource, subject),
                    {
                        "source": "http" if source.startswith("http_") else source,
                        "resource_ref": resource,
                        "resource_hint": hints,
                        "subject_ref": subject,
                        "subject_kind": kind,
                        "count": 0,
                        "first_seen": timestamp,
                        "last_seen": timestamp,
                        "categories": Counter(),
                    },
                )
                group["count"] += 1
                group["first_seen"] = min(group["first_seen"], timestamp)
                group["last_seen"] = max(group["last_seen"], timestamp)
                group["categories"][category] += 1
                if source in ("http_probe", "http_denied", "armor"):
                    status = _mapping(entry.get("httpRequest")).get("status")
                    status_key = (
                        str(status)
                        if type(status) is int and 100 <= status <= 599
                        else "unknown"
                    )
                    group.setdefault("http_status_counts", Counter())[status_key] += 1
    findings = [_make_finding(key[0], group) for key, group in groups.items()]
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    findings.sort(
        key=lambda item: (
            order[item["severity"]],
            -item["evidence"]["count"],
            item["resource"],
        )
    )
    out = result(
        "access_investigation",
        findings[:MAX_FINDINGS],
        {
            "finding_groups": len(findings),
            "omitted_findings": max(0, len(findings) - MAX_FINDINGS),
            "observed_signal_count": sum(group["count"] for group in groups.values()),
            "thresholds": {
                "http_probe_per_subject_resource": 10,
                "denials_per_subject_resource": 20,
            },
            "conclusion": "観測された兆候の調査結果。ログ収集の網羅性と侵害の有無は未確認。",
        },
    )
    out.update(
        window={"start": iso_utc(start), "end_exclusive": iso_utc(end), "hours": hours},
        coverage=coverage,
        limitations=list(_LIMITATIONS),
        complete=all(item["complete"] for item in coverage)
        and len(findings) <= MAX_FINDINGS,
    )
    if not out["complete"]:
        out["status"] = (
            "partial"
            if any(item["complete"] or item["usable_entries"] for item in coverage)
            else "error"
        )
    return out
