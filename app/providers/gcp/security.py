"""Google Cloud security audits (read-only).

IAM policy, service-account keys, Cloud Run, Cloud SQL, VPC firewall,
enabled security APIs, Security Command Center and Artifact Analysis.
Results use the shared shape from ``app.core.findings``; APIs that are not
enabled come back as ``status: api_disabled`` with an enablement hint.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from app.config import CONFIG
from app.core.findings import error_result, finding, result
from app.core.redact import redact
from app.core.timeutil import utcnow
from app.providers.gcp.client import gcp_get, gcp_list, gcp_post, project_path
from app.providers.gcp.observability import RUN, _run_services_raw, compact_service

CRM = "https://cloudresourcemanager.googleapis.com/v1"
IAM = "https://iam.googleapis.com/v1"
SQLADMIN = "https://sqladmin.googleapis.com/v1"
COMPUTE = "https://compute.googleapis.com/compute/v1"
SERVICEUSAGE = "https://serviceusage.googleapis.com/v1"
SCC = "https://securitycenter.googleapis.com/v1"
CONTAINERANALYSIS = "https://containeranalysis.googleapis.com/v1"

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


def _mask_principal(member: str) -> str:
    """Keep service accounts readable, partially mask human accounts."""
    if member.startswith("user:") and "@" in member:
        local, domain = member[5:].split("@", 1)
        return f"user:{local[:1]}***@{domain}"
    return member


# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------


def audit_iam_policy() -> dict:
    """Audit the project IAM policy: public principals, primitive roles,
    risky project-level roles and Data Access audit-log configuration."""
    ok, policy = gcp_post(
        f"{CRM}/projects/{CONFIG.target}:getIamPolicy",
        {"options": {"requestedPolicyVersion": 3}},
    )
    if not ok:
        return error_result("iam_policy", policy)
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
                    finding(
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
                    finding(
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
                    finding(
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
                    finding(
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
                finding(
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
            finding(
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
    return result("iam_policy", findings, summary)


def audit_service_account_keys() -> dict:
    """Find user-managed service-account keys (and their age). Long-lived
    JSON keys are the most common credential-leak vector on GCP."""
    ok, accounts = gcp_list(
        f"{IAM}/projects/{CONFIG.target}/serviceAccounts",
        "accounts",
        params={"pageSize": 100},
    )
    if not ok:
        return error_result("sa_keys", accounts)
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
            f"{IAM}/projects/{CONFIG.target}/serviceAccounts/{email}/keys",
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
                    utcnow() - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
                ).days
            except ValueError:
                pass
            sev = "high" if (age_days or 0) > 90 else "medium"
            findings.append(
                finding(
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
    return result("sa_keys", findings, summary)


# ---------------------------------------------------------------------------
# Cloud Run
# ---------------------------------------------------------------------------


def audit_cloud_run_security() -> dict:
    """Audit Cloud Run services: public access vs ingress, runtime service
    account, plain-text secret-looking env vars, image pinning and deploy age."""
    ok, services = _run_services_raw()
    if not ok:
        return error_result("cloud_run", services)
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
                finding(
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
                finding(
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
                finding(
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
                finding(
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
                    finding(
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
            age = (utcnow() - updated).days
        except ValueError:
            age = None
        if age is not None and age > 90:
            findings.append(
                finding(
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
                finding(
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
    return result("cloud_run", findings, summary)


# ---------------------------------------------------------------------------
# Cloud SQL
# ---------------------------------------------------------------------------


def _engine_of(version: str | None) -> str:
    v = (version or "").upper()
    if v.startswith("POSTGRES"):
        return "postgresql"
    if v.startswith("MYSQL"):
        return "mysql"
    if v.startswith("SQLSERVER"):
        return "mssqlserver"
    return v.lower() or "unknown"


def _engine_major(version: str | None) -> str:
    parts = (version or "").split("_")
    return parts[1] if len(parts) > 1 else ""


def get_cloud_sql_instances() -> dict:
    """List Cloud SQL instances with state, version, maintenance and network
    settings (no credentials)."""
    ok, payload = gcp_get(f"{SQLADMIN}/projects/{CONFIG.target}/instances")
    if not ok:
        return error_result("cloud_sql", payload)
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
                "engine": _engine_of(inst.get("databaseVersion")),
                "engine_version": _engine_major(inst.get("databaseVersion")),
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
                    finding(
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
                    finding(
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
                    finding(
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
                finding(
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
                finding(
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
                finding(
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
                finding(
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
                finding(
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
                finding(
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
                finding(
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
                finding(
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
                finding(
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
    return result("cloud_sql", findings, summary)


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
        f"{COMPUTE}/projects/{CONFIG.target}/global/firewalls", "items"
    )
    if not ok:
        return error_result("firewall", rules)
    assert isinstance(rules, list)
    ok_i, agg = gcp_get(
        f"{COMPUTE}/projects/{CONFIG.target}/aggregated/instances",
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
                finding(
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
                finding(
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
                finding(
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
    return result("firewall", findings, summary)


# ---------------------------------------------------------------------------
# Enabled security services / SCC / container scanning
# ---------------------------------------------------------------------------


def audit_security_services() -> dict:
    """Check which security-relevant Google APIs are enabled and recommend
    missing ones (SCC, container scanning, Asset Inventory, Recommender...)."""
    ok, services = gcp_list(
        f"{SERVICEUSAGE}/projects/{CONFIG.target}/services",
        "services",
        params={"filter": "state:ENABLED", "pageSize": 200},
        max_pages=5,
    )
    if not ok:
        return error_result("security_services", services)
    assert isinstance(services, list)
    enabled = {(s.get("config") or {}).get("name") for s in services}
    findings: list[dict[str, Any]] = []
    for api, (desc, severity) in _RECOMMENDED_APIS.items():
        if api not in enabled:
            findings.append(
                finding(
                    "api.missing",
                    severity,
                    f"{desc} が未有効化",
                    api,
                    f"{api} が無効。",
                    f"`gcloud services enable {api} --project {CONFIG.target}` を検討。",
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
    return result("security_services", findings, summary)


def list_scc_findings(max_findings: int) -> dict:
    """List active Security Command Center findings (CRITICAL/HIGH/MEDIUM).

    Args:
        max_findings: Maximum findings to return (1-200).
    """
    max_findings = max(1, min(int(max_findings), 200))
    url = f"{SCC}/projects/{CONFIG.target}/sources/-/findings"
    params = {
        "filter": 'state="ACTIVE" AND (severity="CRITICAL" OR severity="HIGH" OR severity="MEDIUM")',
        "pageSize": max_findings,
    }
    ok, payload = gcp_get(url, params=params)
    if not ok:
        return error_result(
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
        return error_result("image_scan", services)
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
            return error_result(
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
