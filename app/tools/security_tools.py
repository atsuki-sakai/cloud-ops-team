"""Read-only security & hygiene audit tools for the monthly review.

Every ``audit_*`` tool returns ``findings`` (each with ``severity`` in
critical/high/medium/low/info, ``title``, ``resource``, ``detail`` and
``recommendation``) plus a short ``summary``. APIs that are not enabled are
reported as ``status: api_disabled`` with an enablement hint instead of
failing the whole audit.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from typing import Any

import httpx

from app.config import CONFIG
from app.tools._gcp import gcp_get, gcp_list, gcp_post, project_path
from app.tools._github import gh_rest, repo_path
from app.tools._redact import redact
from app.tools.observability_tools import (
    RUN,
    _run_services_raw,
    compact_service,
)

CRM = "https://cloudresourcemanager.googleapis.com/v1"
IAM = "https://iam.googleapis.com/v1"
SQLADMIN = "https://sqladmin.googleapis.com/v1"
COMPUTE = "https://compute.googleapis.com/compute/v1"
SERVICEUSAGE = "https://serviceusage.googleapis.com/v1"
SCC = "https://securitycenter.googleapis.com/v1"
CONTAINERANALYSIS = "https://containeranalysis.googleapis.com/v1"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
EOL_URL = "https://endoflife.date/api/{product}.json"

_SECRETISH = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key|credential|signing[_-]?key|webhook[_-]?key)"
)
_NOT_SECRET = re.compile(
    r"(?i)(ttl|mode|hours|days|seconds|minutes|url|uri|public|enabled|disabled|count|limit|path|header|name|audience|issuer|alg|version|timeout|length|size|prefix|suffix|flag|required|max|min|_id$|_ids$|kid|rotation|expiry|expires|interval|window)"
)


def _looks_secret(env_name: str) -> bool:
    return bool(_SECRETISH.search(env_name)) and not _NOT_SECRET.search(env_name)


_SENSITIVE_PORTS = {
    "22": "SSH",
    "23": "Telnet",
    "21": "FTP",
    "3389": "RDP",
    "3306": "MySQL",
    "5432": "PostgreSQL",
    "6379": "Redis",
    "27017": "MongoDB",
    "9200": "Elasticsearch",
    "5900": "VNC",
    "11211": "Memcached",
}
_RECOMMENDED_APIS: dict[str, tuple[str, str]] = {
    "securitycenter.googleapis.com": (
        "Security Command Center（設定ミス・脆弱性の継続検知。Standard tier は無料）",
        "high",
    ),
    "containerscanning.googleapis.com": (
        "Artifact Registry の自動コンテナ脆弱性スキャン",
        "high",
    ),
    "cloudasset.googleapis.com": (
        "Cloud Asset Inventory（リソース棚卸し・公開資産の検出）",
        "medium",
    ),
    "recommender.googleapis.com": ("IAM Recommender（過剰権限の自動提案）", "medium"),
    "clouderrorreporting.googleapis.com": (
        "Error Reporting（エラーの自動グルーピング）",
        "low",
    ),
    "binaryauthorization.googleapis.com": (
        "Binary Authorization（署名済みイメージのみデプロイ）",
        "low",
    ),
    "secretmanager.googleapis.com": ("Secret Manager", "info"),
    "cloudkms.googleapis.com": ("Cloud KMS", "info"),
    "iap.googleapis.com": ("Identity-Aware Proxy", "info"),
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _finding(
    check_id: str,
    severity: str,
    title: str,
    resource: str,
    detail: str,
    recommendation: str,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "severity": severity,
        "title": title,
        "resource": resource,
        "detail": redact(detail),
        "recommendation": recommendation,
    }


def _mask_principal(member: str) -> str:
    """Keep service accounts readable, partially mask human accounts."""
    if member.startswith("user:") and "@" in member:
        local, domain = member[5:].split("@", 1)
        return f"user:{local[:1]}***@{domain}"
    return member


def _result(
    check: str, findings: list[dict[str, Any]], summary: dict[str, Any]
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {
        "status": "ok",
        "check": check,
        "finding_counts": counts,
        "findings": findings,
        "summary": summary,
    }


def _error(check: str, payload: dict[str, Any], hint: str = "") -> dict[str, Any]:
    out = {"status": payload.get("status", "error"), "check": check, "error": payload}
    if hint:
        out["hint"] = hint
    return out


# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------


def audit_iam_policy() -> dict:
    """Audit the project IAM policy: public principals, primitive roles,
    risky project-level roles and Data Access audit-log configuration."""
    ok, policy = gcp_post(
        f"{CRM}/projects/{CONFIG.target_project}:getIamPolicy",
        {"options": {"requestedPolicyVersion": 3}},
    )
    if not ok:
        return _error("iam_policy", policy)
    findings: list[dict[str, Any]] = []
    roles_summary: dict[str, list[str]] = {}
    members_seen: set[str] = set()
    for binding in policy.get("bindings") or []:
        role = binding.get("role", "")
        members = binding.get("members") or []
        roles_summary[role] = [_mask_principal(m) for m in members]
        members_seen.update(members)
        for member in members:
            if member in ("allUsers", "allAuthenticatedUsers"):
                findings.append(
                    _finding(
                        "iam.public",
                        "critical",
                        "公開プリンシパルにプロジェクトロールが付与",
                        f"{member} → {role}",
                        "プロジェクトレベルで allUsers/allAuthenticatedUsers にロールが付いています。",
                        "直ちにバインディングを削除し、必要なら個別リソース（Cloud Run invoker 等）に限定する。",
                    )
                )
        if role in ("roles/owner", "roles/editor"):
            users = [m for m in members if m.startswith("user:")]
            sas = [m for m in members if m.startswith("serviceAccount:")]
            for sa in sas:
                sev = "high"
                note = ""
                if sa.endswith("-compute@developer.gserviceaccount.com"):
                    note = "（デフォルト Compute SA。Cloud Run/Build の既定 ID になりがち）"
                findings.append(
                    _finding(
                        "iam.primitive_sa",
                        sev,
                        f"サービスアカウントに基本ロール {role}",
                        sa,
                        f"{sa} に {role} が付与されています{note}。",
                        "専用 SA を作り、必要な事前定義ロールだけに置き換える。",
                    )
                )
            if role == "roles/owner" and len(users) > 2:
                findings.append(
                    _finding(
                        "iam.many_owners",
                        "medium",
                        "Owner 権限を持つユーザーが多い",
                        role,
                        f"{len(users)} 名のユーザーが roles/owner を保持。",
                        "Owner は 1〜2 名に絞り、他は Editor/Viewer や個別ロールへ。",
                    )
                )
            if role == "roles/editor" and users:
                findings.append(
                    _finding(
                        "iam.primitive_user",
                        "medium",
                        "ユーザーに基本ロール roles/editor",
                        role,
                        f"{len(users)} 名のユーザーに roles/editor。",
                        "業務に必要な事前定義ロールへ置換（最小権限）。",
                    )
                )
        if role in (
            "roles/iam.serviceAccountTokenCreator",
            "roles/iam.serviceAccountUser",
            "roles/iam.serviceAccountKeyAdmin",
            "roles/iam.securityAdmin",
            "roles/resourcemanager.projectIamAdmin",
        ):
            findings.append(
                _finding(
                    "iam.risky_role",
                    "medium",
                    f"プロジェクト全体に強い権限 {role}",
                    role,
                    f"{role} がプロジェクトレベルで {len(members)} プリンシパルに付与。",
                    "可能なら特定 SA/リソース単位の付与に縮小する。",
                )
            )
    audit_configs = policy.get("auditConfigs") or []
    all_services = next(
        (c for c in audit_configs if c.get("service") == "allServices"), None
    )
    log_types = {
        c.get("logType") for c in (all_services or {}).get("auditLogConfigs") or []
    }
    if not {"DATA_READ", "DATA_WRITE"} <= log_types:
        findings.append(
            _finding(
                "iam.audit_logs",
                "low",
                "Data Access 監査ログが全サービスで有効ではない",
                "auditConfigs",
                f"allServices の有効 logType: {sorted(t for t in log_types if t)}",
                "少なくとも DATA_WRITE を allServices で有効化（コストに注意、BigQuery/Storage は除外検討）。",
            )
        )
    summary = {
        "binding_count": len(policy.get("bindings") or []),
        "distinct_members": len(members_seen),
        "owners": roles_summary.get("roles/owner", []),
        "editors": roles_summary.get("roles/editor", []),
        "audit_log_types_all_services": sorted(t for t in log_types if t),
        "roles": {r: len(m) for r, m in roles_summary.items()},
    }
    return _result("iam_policy", findings, summary)


def audit_service_account_keys() -> dict:
    """Find user-managed service-account keys (and their age). Long-lived
    JSON keys are the most common credential-leak vector on GCP."""
    ok, accounts = gcp_list(
        f"{IAM}/projects/{CONFIG.target_project}/serviceAccounts",
        "accounts",
        params={"pageSize": 100},
    )
    if not ok:
        return _error("sa_keys", accounts)
    assert isinstance(accounts, list)
    findings: list[dict[str, Any]] = []
    with_keys = 0
    key_total = 0
    errors = []
    for account in accounts[:60]:
        if account.get("disabled"):
            continue
        email = account.get("email", "")
        ok2, keys = gcp_get(
            f"{IAM}/projects/{CONFIG.target_project}/serviceAccounts/{email}/keys",
            params={"keyTypes": "USER_MANAGED"},
        )
        if not ok2:
            errors.append({"account": email, **keys})
            continue
        user_keys = [k for k in keys.get("keys") or [] if not k.get("disabled")]
        if not user_keys:
            continue
        with_keys += 1
        for key in user_keys:
            key_total += 1
            created = key.get("validAfterTime", "")
            age_days = None
            try:
                age_days = (
                    _now() - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
                ).days
            except ValueError:
                pass
            sev = "high" if (age_days or 0) > 90 else "medium"
            findings.append(
                _finding(
                    "iam.sa_user_key",
                    sev,
                    "ユーザー管理の SA 鍵が存在",
                    email,
                    f"鍵 {str(key.get('name', '')).rsplit('/', 1)[-1][:8]}… 作成 {created[:10]}（{age_days} 日前）",
                    "Workload Identity / 短命トークンへ移行し鍵を削除。残す場合は 90 日ローテーション。",
                )
            )
    summary = {
        "service_accounts": len(accounts),
        "accounts_with_user_keys": with_keys,
        "user_managed_keys": key_total,
        "errors": errors[:5],
    }
    return _result("sa_keys", findings, summary)


# ---------------------------------------------------------------------------
# Cloud Run
# ---------------------------------------------------------------------------


def audit_cloud_run_security() -> dict:
    """Audit Cloud Run services: public access vs ingress, runtime service
    account, plain-text secret-looking env vars, image pinning and deploy age."""
    ok, services = _run_services_raw()
    if not ok:
        return _error("cloud_run", services)
    assert isinstance(services, list)
    findings: list[dict[str, Any]] = []
    rows = []
    for raw in services:
        svc = compact_service(raw)
        name = svc["name"] or ""
        ok2, policy = gcp_get(f"{RUN}/{raw.get('name')}:getIamPolicy")
        public = False
        if ok2:
            for binding in policy.get("bindings") or []:
                if binding.get("role") == "roles/run.invoker" and any(
                    m in ("allUsers", "allAuthenticatedUsers")
                    for m in binding.get("members") or []
                ):
                    public = True
        if svc["invoker_iam_disabled"]:
            findings.append(
                _finding(
                    "run.invoker_iam_disabled",
                    "high",
                    "Invoker IAM チェックが無効",
                    name,
                    "invokerIamDisabled=true。誰でも呼び出せます。",
                    "意図的でなければ無効化を解除する。",
                )
            )
        if public and svc["ingress"] == "INGRESS_TRAFFIC_ALL":
            findings.append(
                _finding(
                    "run.public",
                    "info",
                    "インターネット公開サービス",
                    name,
                    "allUsers に run.invoker。Web/API として意図どおりか確認。",
                    "内部用途なら ingress を INTERNAL/LB 経由に変更し IAM で保護する。",
                )
            )
        sa = svc["service_account"] or ""
        if not sa or sa.endswith("-compute@developer.gserviceaccount.com"):
            findings.append(
                _finding(
                    "run.default_sa",
                    "high",
                    "デフォルト Compute SA で稼働",
                    name,
                    f"serviceAccount={sa or '(default compute)'}",
                    "サービス専用 SA を作成し最小権限を付与する。",
                )
            )
        secretish = [n for n in svc["env_plain_names"] if _looks_secret(n)]
        if secretish:
            findings.append(
                _finding(
                    "run.plain_secret_env",
                    "high",
                    "秘密情報らしき環境変数が平文で設定",
                    name,
                    f"平文 env: {', '.join(secretish[:8])}",
                    "Secret Manager 参照（valueSource.secretKeyRef）に切り替える。",
                )
            )
        for image in svc["images"]:
            if not image:
                continue
            if "@sha256:" not in image:
                sev = (
                    "high"
                    if image.endswith(":latest") or ":" not in image.rsplit("/", 1)[-1]
                    else "medium"
                )
                findings.append(
                    _finding(
                        "run.image_not_pinned",
                        sev,
                        "イメージがダイジェスト固定ではない",
                        name,
                        f"image={image}",
                        "デプロイ時に @sha256 ダイジェストで固定し、再現性と改ざん耐性を確保。",
                    )
                )
        try:
            updated = dt.datetime.fromisoformat(
                str(svc["update_time"]).replace("Z", "+00:00")
            )
            age = (_now() - updated).days
        except ValueError:
            age = None
        if age is not None and age > 90:
            findings.append(
                _finding(
                    "run.stale_deploy",
                    "low",
                    "90 日以上デプロイされていない",
                    name,
                    f"最終更新 {svc['update_time']}（{age} 日前）",
                    "ベースイメージの脆弱性修正を取り込むため定期的に再ビルド・再デプロイする。",
                )
            )
        if svc["execution_environment"] == "EXECUTION_ENVIRONMENT_GEN1":
            findings.append(
                _finding(
                    "run.gen1",
                    "info",
                    "第 1 世代実行環境",
                    name,
                    "gen1 を使用。",
                    "特段の理由がなければ gen2 へ。",
                )
            )
        rows.append({**svc, "public": public, "deploy_age_days": age})
    summary = {
        "service_count": len(rows),
        "public_services": [r["name"] for r in rows if r["public"]],
        "services": rows,
    }
    return _result("cloud_run", findings, summary)


# ---------------------------------------------------------------------------
# Cloud SQL
# ---------------------------------------------------------------------------


def get_cloud_sql_instances() -> dict:
    """List Cloud SQL instances with state, version, maintenance and network
    settings (no credentials)."""
    ok, payload = gcp_get(f"{SQLADMIN}/projects/{CONFIG.target_project}/instances")
    if not ok:
        return _error("cloud_sql", payload)
    rows = []
    for inst in payload.get("items") or []:
        settings = inst.get("settings") or {}
        ip = settings.get("ipConfiguration") or {}
        backup = settings.get("backupConfiguration") or {}
        rows.append(
            {
                "name": inst.get("name"),
                "state": inst.get("state"),
                "database_version": inst.get("databaseVersion"),
                "installed_version": inst.get("databaseInstalledVersion"),
                "maintenance_version": inst.get("maintenanceVersion"),
                "available_maintenance_versions": inst.get(
                    "availableMaintenanceVersions"
                )
                or [],
                "tier": settings.get("tier"),
                "availability_type": settings.get("availabilityType"),
                "region": inst.get("region"),
                "public_ip_enabled": bool(ip.get("ipv4Enabled")),
                "private_network": ip.get("privateNetwork"),
                "authorized_networks": [
                    n.get("value") for n in ip.get("authorizedNetworks") or []
                ],
                "ssl_mode": ip.get("sslMode"),
                "require_ssl": ip.get("requireSsl"),
                "backup_enabled": bool(backup.get("enabled")),
                "pitr_enabled": bool(backup.get("pointInTimeRecoveryEnabled")),
                "backup_retention": (backup.get("backupRetentionSettings") or {}).get(
                    "retainedBackups"
                ),
                "deletion_protection": bool(settings.get("deletionProtectionEnabled")),
                "maintenance_window": settings.get("maintenanceWindow") or {},
                "database_flags": {
                    f.get("name"): f.get("value")
                    for f in settings.get("databaseFlags") or []
                },
                "password_policy_enabled": bool(
                    (settings.get("passwordValidationPolicy") or {}).get(
                        "enablePasswordPolicy"
                    )
                ),
                "storage_auto_resize": settings.get("storageAutoResize"),
                "data_disk_size_gb": settings.get("dataDiskSizeGb"),
            }
        )
    return {"status": "ok", "count": len(rows), "instances": rows}


def audit_cloud_sql() -> dict:
    """Audit Cloud SQL instances: exposure (public IP / authorized networks /
    SSL), backups & PITR, deletion protection, pending maintenance updates."""
    data = get_cloud_sql_instances()
    if data.get("status") != "ok":
        return data
    findings: list[dict[str, Any]] = []
    for inst in data["instances"]:
        name = inst["name"]
        nets = inst["authorized_networks"]
        if inst["public_ip_enabled"]:
            if any(n in ("0.0.0.0/0", "::/0") for n in nets):
                findings.append(
                    _finding(
                        "sql.world_open",
                        "critical",
                        "Cloud SQL が全世界に公開",
                        name,
                        f"authorizedNetworks={nets}",
                        "0.0.0.0/0 を即削除。Auth Proxy / Private IP に移行。",
                    )
                )
            elif nets:
                findings.append(
                    _finding(
                        "sql.authorized_networks",
                        "medium",
                        "パブリック IP + 許可ネットワーク",
                        name,
                        f"authorizedNetworks={nets}",
                        "固定 IP が本当に必要か確認。Auth Proxy / Private IP を推奨。",
                    )
                )
            else:
                findings.append(
                    _finding(
                        "sql.public_ip",
                        "low",
                        "パブリック IP が有効（接続は IAM/Auth Proxy 経由）",
                        name,
                        "許可ネットワークは無し。",
                        "Private IP + Serverless VPC Access への移行を検討。",
                    )
                )
        ssl_ok = (
            inst["ssl_mode"]
            in ("ENCRYPTED_ONLY", "TRUSTED_CLIENT_CERTIFICATE_REQUIRED")
            or inst["require_ssl"]
        )
        if not ssl_ok:
            findings.append(
                _finding(
                    "sql.ssl",
                    "high",
                    "SSL/TLS 接続が強制されていない",
                    name,
                    f"sslMode={inst['ssl_mode']} requireSsl={inst['require_ssl']}",
                    "sslMode=ENCRYPTED_ONLY を設定。",
                )
            )
        if not inst["backup_enabled"]:
            findings.append(
                _finding(
                    "sql.backup",
                    "high",
                    "自動バックアップが無効",
                    name,
                    "backupConfiguration.enabled=false",
                    "自動バックアップを有効化。",
                )
            )
        elif not inst["pitr_enabled"]:
            findings.append(
                _finding(
                    "sql.pitr",
                    "medium",
                    "ポイントインタイムリカバリが無効",
                    name,
                    "pointInTimeRecoveryEnabled=false",
                    "PITR を有効化（WAL 保持コストに注意）。",
                )
            )
        if not inst["deletion_protection"]:
            findings.append(
                _finding(
                    "sql.deletion_protection",
                    "medium",
                    "削除保護が無効",
                    name,
                    "deletionProtectionEnabled=false",
                    "削除保護を有効化。",
                )
            )
        if inst["available_maintenance_versions"]:
            findings.append(
                _finding(
                    "sql.maintenance_pending",
                    "medium",
                    "適用可能なメンテナンス更新あり",
                    name,
                    f"現在 {inst['maintenance_version']} → 利用可能 {inst['available_maintenance_versions']}",
                    "メンテナンスウィンドウで更新を適用（セルフサービスメンテナンス）。",
                )
            )
        if not inst["maintenance_window"]:
            findings.append(
                _finding(
                    "sql.maintenance_window",
                    "low",
                    "メンテナンスウィンドウ未設定",
                    name,
                    "任意の時間に更新が入る可能性。",
                    "低負荷時間帯にウィンドウを設定。",
                )
            )
        if inst["database_flags"].get("cloudsql.iam_authentication") not in (
            "on",
            "true",
        ):
            findings.append(
                _finding(
                    "sql.iam_auth",
                    "low",
                    "IAM データベース認証が無効",
                    name,
                    "cloudsql.iam_authentication 未設定",
                    "パスワードレス接続のため有効化を検討。",
                )
            )
        if not inst["password_policy_enabled"]:
            findings.append(
                _finding(
                    "sql.password_policy",
                    "low",
                    "パスワードポリシー未設定",
                    name,
                    "passwordValidationPolicy 無効",
                    "複雑性・再利用禁止ポリシーを有効化。",
                )
            )
        if inst["availability_type"] != "REGIONAL":
            findings.append(
                _finding(
                    "sql.zonal",
                    "info",
                    "ゾーン構成（HA なし）",
                    name,
                    f"availabilityType={inst['availability_type']} tier={inst['tier']}",
                    "本番 SLO 次第で REGIONAL HA を検討。",
                )
            )
    summary = {
        "instance_count": data["count"],
        "instances": [
            {k: v for k, v in i.items() if k != "database_flags"}
            for i in data["instances"]
        ],
    }
    return _result("cloud_sql", findings, summary)


# ---------------------------------------------------------------------------
# VPC firewall
# ---------------------------------------------------------------------------


def _ports_cover(ports: list[str], target: str) -> bool:
    t = int(target)
    for p in ports:
        if "-" in p:
            lo, hi = p.split("-", 1)
            if int(lo) <= t <= int(hi):
                return True
        elif p == target:
            return True
    return False


def audit_firewall_rules() -> dict:
    """Audit VPC firewall rules for ingress open to the internet on sensitive
    ports (SSH/RDP/DB) or all ports."""
    ok, rules = gcp_list(
        f"{COMPUTE}/projects/{CONFIG.target_project}/global/firewalls", "items"
    )
    if not ok:
        return _error("firewall", rules)
    assert isinstance(rules, list)
    ok_i, agg = gcp_get(
        f"{COMPUTE}/projects/{CONFIG.target_project}/aggregated/instances",
        params={"maxResults": 500},
    )
    instance_count = (
        sum(len(v.get("instances") or []) for v in (agg.get("items") or {}).values())
        if ok_i
        else None
    )
    no_vm_note = (
        "（現在 VM インスタンスは 0 台: default ネットワークの既定ルール。未使用なら削除を推奨）"
        if instance_count == 0
        else ""
    )
    exposed_sev = "medium" if instance_count == 0 else "high"
    findings: list[dict[str, Any]] = []
    open_rules = []
    for rule in rules:
        if (
            rule.get("disabled")
            or rule.get("direction") != "INGRESS"
            or not rule.get("allowed")
        ):
            continue
        ranges = rule.get("sourceRanges") or []
        if not any(r in ("0.0.0.0/0", "::/0") for r in ranges):
            continue
        name = rule.get("name", "")
        exposed: list[str] = []
        all_ports = False
        for allowed in rule["allowed"]:
            proto = allowed.get("IPProtocol")
            ports = allowed.get("ports") or []
            if proto == "all" or (proto in ("tcp", "udp") and not ports):
                all_ports = True
            for port, label in _SENSITIVE_PORTS.items():
                if ports and _ports_cover(ports, port):
                    exposed.append(f"{port}/{label}")
        open_rules.append(
            {
                "name": name,
                "network": str(rule.get("network", "")).rsplit("/", 1)[-1],
                "allowed": rule["allowed"],
                "target_tags": rule.get("targetTags"),
            }
        )
        if all_ports:
            findings.append(
                _finding(
                    "fw.all_ports",
                    exposed_sev,
                    "全ポートがインターネットに開放",
                    name,
                    f"allowed={rule['allowed']}{no_vm_note}",
                    "必要なポート・送信元に限定、または IAP TCP forwarding を利用。",
                )
            )
        elif exposed:
            findings.append(
                _finding(
                    "fw.sensitive_port",
                    exposed_sev,
                    "機微ポートがインターネットに開放",
                    name,
                    f"{', '.join(sorted(set(exposed)))} が 0.0.0.0/0 から到達可能{no_vm_note}",
                    "送信元 IP を限定するか IAP 経由に変更。未使用なら削除。",
                )
            )
        else:
            findings.append(
                _finding(
                    "fw.web_open",
                    "info",
                    "Web ポートがインターネットに開放",
                    name,
                    f"allowed={rule['allowed']}",
                    "LB/Cloud Armor 経由であることを確認。",
                )
            )
    summary = {
        "rule_count": len(rules),
        "vm_instance_count": instance_count,
        "internet_open_rules": open_rules,
    }
    return _result("firewall", findings, summary)


# ---------------------------------------------------------------------------
# Enabled security services / SCC / container scanning
# ---------------------------------------------------------------------------


def audit_security_services() -> dict:
    """Check which security-relevant Google APIs are enabled and recommend
    missing ones (SCC, container scanning, Asset Inventory, Recommender...)."""
    ok, services = gcp_list(
        f"{SERVICEUSAGE}/projects/{CONFIG.target_project}/services",
        "services",
        params={"filter": "state:ENABLED", "pageSize": 200},
        max_pages=5,
    )
    if not ok:
        return _error("security_services", services)
    assert isinstance(services, list)
    enabled = {(s.get("config") or {}).get("name") for s in services}
    findings: list[dict[str, Any]] = []
    for api, (desc, severity) in _RECOMMENDED_APIS.items():
        if api not in enabled:
            findings.append(
                _finding(
                    "api.missing",
                    severity,
                    f"{desc} が未有効化",
                    api,
                    f"{api} が無効。",
                    f"`gcloud services enable {api} --project {CONFIG.target_project}` を検討。",
                )
            )
    summary = {
        "enabled_count": len(enabled),
        "security_apis_enabled": sorted(a for a in _RECOMMENDED_APIS if a in enabled),
        "security_apis_missing": sorted(
            a for a in _RECOMMENDED_APIS if a not in enabled
        ),
        "compute_enabled": "compute.googleapis.com" in enabled,
        "container_enabled": "container.googleapis.com" in enabled,
    }
    return _result("security_services", findings, summary)


def list_scc_findings(max_findings: int) -> dict:
    """List active Security Command Center findings (CRITICAL/HIGH/MEDIUM).

    Args:
        max_findings: Maximum findings to return (1-200).
    """
    max_findings = max(1, min(int(max_findings), 200))
    url = f"{SCC}/projects/{CONFIG.target_project}/sources/-/findings"
    params = {
        "filter": 'state="ACTIVE" AND (severity="CRITICAL" OR severity="HIGH" OR severity="MEDIUM")',
        "pageSize": max_findings,
    }
    ok, payload = gcp_get(url, params=params)
    if not ok:
        return _error(
            "scc",
            payload,
            hint="Security Command Center を有効化（Standard tier は無料）: コンソール > Security > Overview から有効化、または securitycenter.googleapis.com を enable。",
        )
    rows = []
    for item in payload.get("listFindingsResults") or []:
        finding = item.get("finding") or {}
        props = finding.get("sourceProperties") or {}
        rows.append(
            {
                "category": finding.get("category"),
                "severity": finding.get("severity"),
                "resource": finding.get("resourceName"),
                "finding_class": finding.get("findingClass"),
                "event_time": finding.get("eventTime"),
                "explanation": redact(
                    str(props.get("Explanation") or finding.get("description") or "")
                )[:400],
                "recommendation": redact(str(props.get("Recommendation") or ""))[:400],
                "name": finding.get("name"),
            }
        )
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
    rows.sort(key=lambda r: order.get(r["severity"], 9))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1
    return {
        "status": "ok",
        "count": len(rows),
        "counts": counts,
        "findings": rows,
        "truncated": bool(payload.get("nextPageToken")),
    }


def scan_deployed_images() -> dict:
    """Summarise Artifact Analysis vulnerability results for the container
    images currently serving each Cloud Run service."""
    ok, services = _run_services_raw()
    if not ok:
        return _error("image_scan", services)
    assert isinstance(services, list)
    images: dict[str, list[str]] = {}
    for raw in services:
        svc_name = str(raw.get("name", "")).rsplit("/", 1)[-1]
        rev = str(raw.get("latestReadyRevision") or "")
        if not rev:
            continue
        ok2, revision = gcp_get(f"{RUN}/{rev}")
        if not ok2:
            continue
        template_images = [c.get("image", "") for c in revision.get("containers") or []]
        digests: list[str] = []
        for idx, status in enumerate(revision.get("containerStatuses") or []):
            digest = status.get("imageDigest") or ""
            if digest and "@" not in digest and idx < len(template_images):
                digest = (
                    template_images[idx].split("@", 1)[0].rsplit(":", 1)[0]
                    + "@"
                    + digest
                )
            if digest:
                digests.append(digest)
        if not digests:  # containerStatuses is not always populated; digest-pinned images are fine as-is
            digests = [img for img in template_images if "@sha256:" in img]
        for digest in digests:
            images.setdefault(digest, []).append(svc_name)
    results = []
    for image, used_by in images.items():
        params = {
            "filter": f'kind="VULNERABILITY" AND resourceUrl="https://{image}"',
            "pageSize": 1000,
        }
        ok3, occurrences = gcp_list(
            f"{CONTAINERANALYSIS}/{project_path()}/occurrences",
            "occurrences",
            params=params,
            max_pages=3,
        )
        if not ok3:
            return _error(
                "image_scan",
                occurrences,
                hint="Artifact Registry の脆弱性スキャンを有効化: `gcloud services enable containerscanning.googleapis.com`（イメージが別プロジェクトのレジストリにある場合はそのプロジェクトで）。",
            )
        assert isinstance(occurrences, list)
        counts: dict[str, int] = {}
        fixable = 0
        top = []
        for occ in occurrences:
            vuln = occ.get("vulnerability") or {}
            sev = vuln.get("effectiveSeverity") or vuln.get("severity") or "UNKNOWN"
            counts[sev] = counts.get(sev, 0) + 1
            if vuln.get("fixAvailable"):
                fixable += 1
            if sev in ("CRITICAL", "HIGH") and len(top) < 15:
                issue = (vuln.get("packageIssue") or [{}])[0]
                top.append(
                    {
                        "cve": str(occ.get("noteName", "")).rsplit("/", 1)[-1],
                        "severity": sev,
                        "package": issue.get("affectedPackage"),
                        "version": (issue.get("affectedVersion") or {}).get("fullName"),
                        "fixed_version": (issue.get("fixedVersion") or {}).get(
                            "fullName"
                        ),
                        "cvss": vuln.get("cvssScore"),
                        "fix_available": vuln.get("fixAvailable"),
                    }
                )
        results.append(
            {
                "image": image,
                "used_by": sorted(set(used_by)),
                "total": len(occurrences),
                "by_severity": counts,
                "fix_available": fixable,
                "top_findings": top,
                "scanned": bool(occurrences),
            }
        )
    return {
        "status": "ok",
        "image_count": len(results),
        "images": results,
        "note": "total=0 の場合はスキャン未実施（API 無効 / 別プロジェクト / 古いイメージ）の可能性があります。",
    }


# ---------------------------------------------------------------------------
# Threat intelligence & version hygiene
# ---------------------------------------------------------------------------

_kev_cache: tuple[float, dict[str, Any]] | None = None


def fetch_cisa_kev(days: int) -> dict:
    """Fetch CISA's Known Exploited Vulnerabilities catalog and return entries
    added in the last ``days`` days, highlighting those matching the stack
    keywords configured for this project.

    Args:
        days: Look-back window in days (1-365).
    """
    global _kev_cache
    days = max(1, min(int(days), 365))
    now = time.time()
    if _kev_cache and now - _kev_cache[0] < 3600:
        data = _kev_cache[1]
    else:
        try:
            resp = httpx.get(KEV_URL, timeout=60, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "error", "message": f"KEV fetch failed: {exc}"}
        _kev_cache = (now, data)
    cutoff = (_now() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    keywords = [k.lower() for k in CONFIG.stack_keywords]

    def compact(v: dict[str, Any]) -> dict[str, Any]:
        return {
            "cve": v.get("cveID"),
            "vendor": v.get("vendorProject"),
            "product": v.get("product"),
            "name": v.get("vulnerabilityName"),
            "date_added": v.get("dateAdded"),
            "due_date": v.get("dueDate"),
            "ransomware": v.get("knownRansomwareCampaignUse"),
            "description": str(v.get("shortDescription") or "")[:220],
        }

    recent = sorted(
        (
            v
            for v in data.get("vulnerabilities") or []
            if str(v.get("dateAdded", "")) >= cutoff
        ),
        key=lambda v: v.get("dateAdded", ""),
        reverse=True,
    )
    matches = [
        v
        for v in recent
        if any(
            k
            in f"{v.get('vendorProject', '')} {v.get('product', '')} {v.get('vulnerabilityName', '')}".lower()
            for k in keywords
        )
    ]
    return {
        "status": "ok",
        "catalog_version": data.get("catalogVersion"),
        "released": data.get("dateReleased"),
        "window_days": days,
        "recent_count": len(recent),
        "ransomware_count": sum(
            1
            for v in recent
            if str(v.get("knownRansomwareCampaignUse", "")).lower() == "known"
        ),
        "stack_keywords": list(CONFIG.stack_keywords),
        "stack_matches": [compact(v) for v in matches[:40]],
        "recent": [compact(v) for v in recent[:40]],
    }


def check_eol_status(product: str, version: str) -> dict:
    """Look up end-of-life / latest-release status on endoflife.date.

    Args:
        product: endoflife.date slug, e.g. ``postgresql``, ``nodejs``, ``rust``,
            ``debian``, ``alpine``, ``python``, ``nextjs``, ``react``, ``ubuntu``.
        version: Version in use (e.g. ``17``, ``22.11.0``, ``1.82``).
    """
    slug = product.strip().lower()
    try:
        resp = httpx.get(
            EOL_URL.format(product=slug), timeout=30, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        return {"status": "error", "message": f"endoflife.date fetch failed: {exc}"}
    if resp.status_code == 404:
        return {
            "status": "not_found",
            "message": f"unknown product slug '{slug}'",
            "hint": "https://endoflife.date/api/all.json で slug を確認",
        }
    if resp.status_code >= 400:
        return {"status": "error", "http_status": resp.status_code}
    cycles = resp.json()
    ver = version.strip().lstrip("v")
    parts = ver.split(".")
    candidates = [ver, ".".join(parts[:2]), parts[0]]
    match = next(
        (c for cand in candidates for c in cycles if str(c.get("cycle")) == cand), None
    )
    if match is None:
        return {
            "status": "not_found",
            "product": slug,
            "version": version,
            "known_cycles": [str(c.get("cycle")) for c in cycles[:10]],
        }
    today = _now().date()
    eol = match.get("eol")
    is_eol: bool | None
    days_until_eol = None
    if isinstance(eol, bool):
        is_eol = eol
    elif isinstance(eol, str):
        try:
            eol_date = dt.date.fromisoformat(eol)
            is_eol = eol_date <= today
            days_until_eol = (eol_date - today).days
        except ValueError:
            is_eol = None
    else:
        is_eol = None
    newer = [
        str(c.get("cycle")) for c in cycles if cycles.index(c) < cycles.index(match)
    ]
    return {
        "status": "ok",
        "product": slug,
        "version": version,
        "cycle": str(match.get("cycle")),
        "is_eol": is_eol,
        "eol": eol,
        "days_until_eol": days_until_eol,
        "latest_in_cycle": match.get("latest"),
        "latest_release_date": match.get("latestReleaseDate"),
        "lts": match.get("lts"),
        "support": match.get("support"),
        "newest_cycle": str(cycles[0].get("cycle")) if cycles else None,
        "newer_cycles": newer,
        "url": f"https://endoflife.date/{slug}",
    }


_SECURITY_WORKFLOW = re.compile(
    r"(?i)security|scan|audit|drift|trivy|codeql|osv|depend"
)
_VERSION_FILES = (
    ".nvmrc",
    ".node-version",
    "rust-toolchain.toml",
    "rust-toolchain",
    ".python-version",
    "package.json",
    ".github/dependabot.yml",
    "renovate.json",
)


def get_repo_security_posture() -> dict:
    """Summarise the GitHub repository's security hygiene: security features,
    branch protection, latest runs of security workflows, Dockerfile base
    images and toolchain version pins (for EOL checks), Dependabot PRs."""
    ok, repo = gh_rest("GET", repo_path())
    if not ok:
        return _error("repo_posture", repo)
    default_branch = repo.get("default_branch", "main")
    sec = repo.get("security_and_analysis") or {}
    ok_va, _ = gh_rest("GET", f"{repo_path()}/vulnerability-alerts")
    ok_bp, protection = gh_rest(
        "GET", f"{repo_path()}/branches/{default_branch}/protection"
    )
    ok_rs, rulesets = gh_rest("GET", f"{repo_path()}/rulesets", params={"per_page": 20})

    workflows_out = []
    ok_wf, workflows = gh_rest(
        "GET", f"{repo_path()}/actions/workflows", params={"per_page": 100}
    )
    if ok_wf:
        for wf in workflows.get("workflows") or []:
            if not _SECURITY_WORKFLOW.search(
                f"{wf.get('name', '')} {wf.get('path', '')}"
            ):
                continue
            ok_r, runs = gh_rest(
                "GET",
                f"{repo_path()}/actions/workflows/{wf['id']}/runs",
                params={"per_page": 1},
            )
            latest = ((runs or {}).get("workflow_runs") or [{}])[0] if ok_r else {}
            workflows_out.append(
                {
                    "name": wf.get("name"),
                    "path": wf.get("path"),
                    "state": wf.get("state"),
                    "latest_status": latest.get("status"),
                    "latest_conclusion": latest.get("conclusion"),
                    "latest_run_at": latest.get("created_at"),
                    "latest_event": latest.get("event"),
                    "url": latest.get("html_url"),
                }
            )

    dockerfiles = []
    version_pins: dict[str, str] = {}
    ok_tree, tree = gh_rest(
        "GET", f"{repo_path()}/git/trees/{default_branch}", params={"recursive": "1"}
    )
    if ok_tree:
        paths = [
            t.get("path", "") for t in tree.get("tree") or [] if t.get("type") == "blob"
        ]
        docker_paths = [
            p
            for p in paths
            if "node_modules" not in p
            and (
                p.endswith("Dockerfile")
                or "/Dockerfile." in p
                or p.startswith("Dockerfile.")
            )
        ][:6]
        for path in docker_paths:
            ok_c, content = gh_rest(
                "GET",
                f"{repo_path()}/contents/{path}",
                params={"ref": default_branch},
                accept="application/vnd.github.raw+json",
            )
            text = content.get("raw", "") if isinstance(content, dict) else ""
            if ok_c and text:
                from_lines = [
                    line.strip()
                    for line in text.splitlines()
                    if line.strip().upper().startswith("FROM ")
                ]
                dockerfiles.append({"path": path, "from": from_lines})
        for vf in _VERSION_FILES:
            if vf in paths:
                ok_v, content = gh_rest(
                    "GET",
                    f"{repo_path()}/contents/{vf}",
                    params={"ref": default_branch},
                    accept="application/vnd.github.raw+json",
                )
                text = content.get("raw", "") if isinstance(content, dict) else ""
                if ok_v and text:
                    if vf == "package.json":
                        m = re.search(r'"engines"\s*:\s*\{[^}]*\}', text, re.S)
                        version_pins[vf] = (
                            m.group(0)[:200] if m else "(no engines field)"
                        )
                    elif vf.endswith(".yml") or vf.endswith(".json"):
                        version_pins[vf] = "(present)"
                    else:
                        version_pins[vf] = text.strip()[:120]
    ok_dp, dependabot = gh_rest(
        "GET",
        "/search/issues",
        params={
            "q": f"repo:{CONFIG.github_repo} is:pr is:open author:app/dependabot",
            "per_page": 1,
        },
    )
    ok_rn, renovate = gh_rest(
        "GET",
        "/search/issues",
        params={
            "q": f"repo:{CONFIG.github_repo} is:pr is:open author:app/renovate",
            "per_page": 1,
        },
    )

    findings: list[dict[str, Any]] = []
    if not ok_va:
        findings.append(
            _finding(
                "repo.dependabot_alerts",
                "medium",
                "Dependabot alerts が無効",
                CONFIG.github_repo,
                "vulnerability-alerts が有効化されていません。",
                "Settings > Code security で Dependabot alerts / security updates を有効化。",
            )
        )
    if (sec.get("secret_scanning") or {}).get("status") not in (None, "enabled"):
        findings.append(
            _finding(
                "repo.secret_scanning",
                "medium",
                "Secret scanning が無効",
                CONFIG.github_repo,
                "secret_scanning=disabled",
                "Secret scanning + push protection を有効化。",
            )
        )
    if not ok_bp and not (ok_rs and rulesets):
        findings.append(
            _finding(
                "repo.branch_protection",
                "high",
                f"{default_branch} にブランチ保護/ルールセットがない",
                CONFIG.github_repo,
                "保護なしで直接 push 可能。",
                "PR 必須 + 必須チェック + レビュー 1 名以上を設定。",
            )
        )
    for wf in workflows_out:
        if wf["latest_conclusion"] in ("failure", "timed_out", "cancelled"):
            findings.append(
                _finding(
                    "repo.security_workflow_failing",
                    "medium",
                    f"セキュリティ系ワークフロー失敗: {wf['name']}",
                    wf["path"],
                    f"最新実行 {wf['latest_run_at']} → {wf['latest_conclusion']}",
                    "失敗原因を確認し、スキャン結果が継続的に得られる状態に戻す。",
                )
            )
    summary = {
        "default_branch": default_branch,
        "visibility": repo.get("visibility"),
        "security_and_analysis": sec,
        "dependabot_alerts_enabled": ok_va,
        "branch_protection": bool(ok_bp and protection),
        "rulesets": [r.get("name") for r in rulesets]
        if ok_rs and isinstance(rulesets, list)
        else [],
        "security_workflows": workflows_out,
        "dockerfiles": dockerfiles,
        "version_pins": version_pins,
        "open_dependabot_prs": (dependabot or {}).get("total_count") if ok_dp else None,
        "open_renovate_prs": (renovate or {}).get("total_count") if ok_rn else None,
        "pushed_at": repo.get("pushed_at"),
    }
    return _result("repo_posture", findings, summary)


# ---------------------------------------------------------------------------
# Web threat research (Google Search grounding, single bounded call)
# ---------------------------------------------------------------------------

_RESEARCH_MODEL = "gemini-3.8-flash"
_RESEARCH_TIMEOUT_MS = 90_000


def web_threat_research(focus: str, days: int) -> dict:
    """Research recent, actively exploited vulnerabilities, attack campaigns and
    malware trends relevant to ``focus`` using Google Search grounding.

    One bounded call (90 s timeout, no retries) that returns a Japanese
    summary plus the grounding sources. Call it once per audit with the whole
    stack in ``focus``.

    Args:
        focus: Technologies / platforms to cover, e.g. ``"Google Cloud (Cloud Run,
            Cloud SQL, IAM), PostgreSQL 17, Node.js 22, Next.js, Rust, Debian bookworm"``.
        days: Look-back window in days (7-90).
    """
    from google import genai
    from google.genai import types as genai_types

    days = max(7, min(int(days), 90))
    today = _now().date()
    since = today - dt.timedelta(days=days)
    prompt = (
        "あなたは脅威インテリジェンスの調査担当です。Google 検索を使い、"
        f"{since.isoformat()} から {today.isoformat()} の期間に公表・観測された、次の技術スタックに関係する\n"
        "「実際に悪用されている脆弱性（CISA KEV 掲載や in-the-wild の報告）」「攻撃キャンペーン」「マルウェア / ランサムウェアの流行」"
        "「クラウド（特に Google Cloud / Cloud Run / Cloud SQL / IAM / サービスアカウント鍵）を狙う手口」を調べてください。\n\n"
        f"対象スタック: {focus}\n\n"
        "出力形式（日本語、最大 10 件、新しい順）:\n"
        "- [YYYY-MM-DD] 項目名 — 影響する製品 / バージョン — 何が起きているか（1〜2 文）— 推奨対応 — 出典 URL\n\n"
        "期間外の古い情報や関連性の低いものは含めないでください。該当がなければ「該当なし」と明記してください。"
    )
    try:
        client = genai.Client()
        response = client.models.generate_content(
            model=_RESEARCH_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                temperature=0.2,
                max_output_tokens=3000,
                http_options=genai_types.HttpOptions(timeout=_RESEARCH_TIMEOUT_MS),
            ),
        )
    except Exception as exc:
        return {
            "status": "error",
            "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            "hint": "Web 調査はスキップし、CISA KEV の結果のみで脅威動向をまとめてください。",
        }
    text = response.text or ""
    sources: list[dict[str, str]] = []
    queries: list[str] = []
    try:
        meta = response.candidates[0].grounding_metadata
        for chunk in meta.grounding_chunks or []:
            web = getattr(chunk, "web", None)
            if web and web.uri:
                sources.append({"title": web.title or "", "uri": web.uri})
        queries = list(meta.web_search_queries or [])
    except (AttributeError, IndexError, TypeError):
        pass
    return {
        "status": "ok",
        "period": {"from": since.isoformat(), "to": today.isoformat()},
        "focus": focus,
        "summary": redact(text)[:6000],
        "sources": sources[:20],
        "search_queries": queries[:10],
    }
