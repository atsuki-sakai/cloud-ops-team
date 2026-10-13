"""Google Cloud provider (fully implemented).

Logs and metrics come from Cloud Logging / Cloud Monitoring, services from
Cloud Run, jobs from Cloud Scheduler, databases from Cloud SQL and security
signals from IAM, VPC firewall, Service Usage, Security Command Center and
Artifact Analysis. All calls are read-only REST calls with Application
Default Credentials (see ``client.py``).
"""

from __future__ import annotations

from typing import Any

from app.providers.base import CloudProvider, ProviderProfile
from app.providers.gcp import observability as obs
from app.providers.gcp import security as sec

PROFILE = ProviderProfile(
    key="gcp",
    display_name="Google Cloud",
    target_label="project",
    target_label_ja="プロジェクト",
    cli="gcloud",
    stack_keywords=("Google Cloud", "Cloud Run", "Cloud SQL", "Kubernetes"),
    glossary={
        "summarize_error_groups": "Cloud Logging（severity>=ERROR）",
        "list_services": "Cloud Run サービス",
        "list_deployments": "Cloud Run リビジョン",
        "list_scheduled_jobs": "Cloud Scheduler ジョブ",
        "list_databases": "Cloud SQL インスタンス",
        "get_request_error_rates": "Cloud Monitoring の Cloud Run request_count（5xx / 4xx）",
        "get_alerting_setup": "Cloud Monitoring のアラートポリシーと Uptime Check",
        "audit_iam": "プロジェクトの IAM ポリシー（公開プリンシパル・基本ロール・強権限ロール・監査ログ）",
        "audit_credentials": "サービスアカウントのユーザー管理鍵",
        "audit_compute": "Cloud Run（公開設定・実行 SA・平文の秘密っぽい環境変数・イメージ固定・デプロイ鮮度）",
        "audit_databases": "Cloud SQL（公開 IP・SSL・バックアップ/PITR・削除保護・保留メンテナンス）",
        "audit_network": "VPC ファイアウォールルール",
        "audit_security_services": "セキュリティ系 API（SCC・コンテナスキャン・Asset Inventory など）の有効化",
        "list_security_findings": "Security Command Center",
        "scan_deployed_images": "Artifact Analysis（稼働中コンテナイメージの脆弱性）",
    },
)


class GcpProvider(CloudProvider):
    profile = PROFILE

    # observability
    def summarize_error_groups(self, hours: int) -> dict[str, Any]:
        return obs.summarize_error_groups(hours)

    def list_error_logs(
        self, hours: int, service_name: str, limit: int
    ) -> dict[str, Any]:
        return obs.list_error_logs(hours, service_name, limit)

    def get_log_context(
        self, service_name: str, around_time: str, window_minutes: int, trace: str
    ) -> dict[str, Any]:
        return obs.get_log_context(service_name, around_time, window_minutes, trace)

    def count_matching_errors(
        self, service_name: str, search_text: str, since: str
    ) -> dict[str, Any]:
        return obs.count_matching_errors(service_name, search_text, since)

    def list_services(self) -> dict[str, Any]:
        return obs.get_cloud_run_services()

    def list_deployments(self, service_name: str, limit: int) -> dict[str, Any]:
        return obs.get_cloud_run_revisions(service_name, limit)

    def list_scheduled_jobs(self) -> dict[str, Any]:
        return obs.get_scheduler_jobs()

    def list_databases(self) -> dict[str, Any]:
        return sec.get_cloud_sql_instances()

    def get_request_error_rates(self, hours: int) -> dict[str, Any]:
        return obs.get_request_error_rates(hours)

    def get_alerting_setup(self) -> dict[str, Any]:
        return obs.get_alerting_setup()

    # security
    def audit_iam(self) -> dict[str, Any]:
        return sec.audit_iam_policy()

    def audit_credentials(self) -> dict[str, Any]:
        return sec.audit_service_account_keys()

    def audit_compute(self) -> dict[str, Any]:
        return sec.audit_cloud_run_security()

    def audit_databases(self) -> dict[str, Any]:
        return sec.audit_cloud_sql()

    def audit_network(self) -> dict[str, Any]:
        return sec.audit_firewall_rules()

    def audit_security_services(self) -> dict[str, Any]:
        return sec.audit_security_services()

    def list_security_findings(self, max_findings: int) -> dict[str, Any]:
        return sec.list_scc_findings(max_findings)

    def scan_deployed_images(self) -> dict[str, Any]:
        return sec.scan_deployed_images()
