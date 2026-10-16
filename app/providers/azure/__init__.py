"""Microsoft Azure provider — skeleton, contributions welcome.

Every capability currently returns ``status: not_supported``; implement them
one at a time with ``azure-identity`` (DefaultAzureCredential) and the
``azure-monitor-query`` / ``azure-mgmt-*`` SDKs. Guide: ``docs/providers.md``.

Suggested mapping:

| Capability | Azure source |
|---|---|
| summarize_error_groups, list_error_logs, get_log_context, count_matching_errors | LogsQueryClient (KQL) on the Log Analytics workspace ``OPS_AZURE_WORKSPACE_ID`` (AppExceptions, AppRequests, ContainerAppConsoleLogs_CL) |
| list_services | azure-mgmt-web (App Service / Functions), azure-mgmt-appcontainers (Container Apps) |
| list_deployments | Container Apps revisions, App Service deployments |
| list_scheduled_jobs | Container Apps Jobs, Logic Apps, Functions timer triggers |
| list_databases | azure-mgmt-postgresqlflexibleservers / mysqlflexibleservers / sql (engine, version) |
| get_request_error_rates | MetricsQueryClient (Http5xx, requests/failed) |
| get_alerting_setup | azure-mgmt-monitor alert rules, Application Insights availability tests |
| audit_iam | azure-mgmt-authorization role assignments (Owner/Contributor at subscription scope, guests) |
| audit_credentials | Microsoft Graph applications/servicePrincipals password & key credentials (expiry, age) |
| audit_compute | App Service / Container Apps: public ingress, managed identity, plain-text app settings |
| audit_databases | public network access, firewall rules (0.0.0.0-255.255.255.255), TLS, backup retention |
| audit_network | azure-mgmt-network NSG rules (Any/Internet on sensitive ports) |
| audit_security_services | azure-mgmt-security Defender for Cloud pricing tiers |
| list_security_findings | Defender for Cloud assessments / alerts |
| scan_deployed_images | Defender for Containers vulnerability assessments (ACR) |
"""

from __future__ import annotations

from app.providers.base import CloudProvider, ProviderProfile

PROFILE = ProviderProfile(
    key="azure",
    display_name="Microsoft Azure",
    target_label="subscription",
    target_label_ja="サブスクリプション",
    cli="az",
    stack_keywords=("Azure",),
    glossary={
        "summarize_error_groups": "Azure Monitor Log Analytics（KQL）/ Application Insights",
        "list_services": "App Service / Container Apps / Functions",
        "list_deployments": "Container Apps リビジョン / App Service デプロイ履歴",
        "list_scheduled_jobs": "Container Apps Jobs / Logic Apps / Functions タイマー",
        "list_databases": "Azure Database for PostgreSQL/MySQL / Azure SQL",
        "get_request_error_rates": "Azure Monitor メトリクス（Http5xx / requests/failed）",
        "get_alerting_setup": "Azure Monitor アラートルール / 可用性テスト",
        "audit_iam": "Azure RBAC ロール割り当て（Owner/Contributor・ゲスト）",
        "audit_credentials": "サービスプリンシパルのシークレット・証明書",
        "audit_compute": "App Service / Container Apps（公開・マネージド ID・平文アプリ設定）",
        "audit_databases": "PostgreSQL/MySQL/SQL（パブリックアクセス・ファイアウォール・TLS・バックアップ）",
        "audit_network": "NSG ルール",
        "audit_security_services": "Defender for Cloud プランの有効化",
        "list_security_findings": "Defender for Cloud の推奨事項・アラート",
        "scan_deployed_images": "Defender for Containers（ACR イメージ評価）",
        "investigate_access": "Activity Logs・アクセスログ・WAF の不審な兆候（未実装）",
        "list_optimization_recommendations": "Azure Advisor のコスト・構成・権限推奨（未実装）",
    },
)


class AzureProvider(CloudProvider):
    profile = PROFILE
