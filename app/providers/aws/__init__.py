"""Amazon Web Services provider — skeleton, contributions welcome.

Every capability currently returns ``status: not_supported``. The team still
runs end to end on AWS (repository posture, CISA KEV, EOL checks, web threat
research and the weekly PR review work without cloud access), and each
capability below can be implemented independently. Add ``boto3`` as an
optional dependency when implementing. Guide: ``docs/providers.md``.

Suggested mapping (boto3 client.method):

| Capability | AWS source |
|---|---|
| summarize_error_groups, list_error_logs, get_log_context, count_matching_errors | logs.start_query / get_query_results (CloudWatch Logs Insights) on ``OPS_AWS_LOG_GROUPS`` |
| list_services | ecs.list_services + describe_services, lambda.list_functions, apprunner.list_services |
| list_deployments | ecs.describe_services(deployments), lambda.list_versions_by_function |
| list_scheduled_jobs | scheduler.list_schedules, events.list_rules |
| list_databases | rds.describe_db_instances / describe_db_clusters (engine, engine_version) |
| get_request_error_rates | cloudwatch.get_metric_data (ALB HTTPCode_Target_5XX_Count, API Gateway 5XXError) |
| get_alerting_setup | cloudwatch.describe_alarms, route53.list_health_checks |
| audit_iam | iam.get_account_summary, get_credential_report, list_attached_*_policies (AdministratorAccess), accessanalyzer.list_findings |
| audit_credentials | iam.list_users + list_access_keys + get_access_key_last_used |
| audit_compute | ecs task definitions (plain-text secret env), lambda function config, public ALB listeners |
| audit_databases | rds: PubliclyAccessible, StorageEncrypted, BackupRetentionPeriod, DeletionProtection, describe_pending_maintenance_actions |
| audit_network | ec2.describe_security_groups (0.0.0.0/0 on 22/3389/DB ports) |
| audit_security_services | guardduty.list_detectors, securityhub.describe_hub, inspector2.batch_get_account_status, config.describe_configuration_recorders, cloudtrail.describe_trails |
| list_security_findings | securityhub.get_findings (ACTIVE, CRITICAL/HIGH/MEDIUM) |
| scan_deployed_images | inspector2.list_findings (ECR images used by running services) |
"""

from __future__ import annotations

from app.providers.base import CloudProvider, ProviderProfile

PROFILE = ProviderProfile(
    key="aws",
    display_name="Amazon Web Services",
    target_label="account",
    target_label_ja="アカウント",
    cli="aws",
    stack_keywords=("Amazon", "AWS"),
    glossary={
        "summarize_error_groups": "CloudWatch Logs（Logs Insights）",
        "list_services": "ECS サービス / Lambda 関数 / App Runner",
        "list_deployments": "ECS デプロイ / Lambda バージョン",
        "list_scheduled_jobs": "EventBridge Scheduler / EventBridge ルール",
        "list_databases": "RDS / Aurora",
        "get_request_error_rates": "CloudWatch メトリクス（ALB / API Gateway の 5XX）",
        "get_alerting_setup": "CloudWatch アラーム / Route 53 ヘルスチェック",
        "audit_iam": "IAM（ルートユーザー・AdministratorAccess・IAM Access Analyzer）",
        "audit_credentials": "IAM ユーザーのアクセスキー",
        "audit_compute": "ECS / Lambda / App Runner（公開・実行ロール・平文環境変数）",
        "audit_databases": "RDS（公開アクセス・暗号化・バックアップ・保留メンテナンス）",
        "audit_network": "セキュリティグループ",
        "audit_security_services": "GuardDuty / Security Hub / Inspector / Config / CloudTrail の有効化",
        "list_security_findings": "Security Hub",
        "scan_deployed_images": "Amazon Inspector（ECR イメージ）",
        "investigate_access": "CloudTrail・アクセスログ・AWS WAF の不審な兆候（未実装）",
        "list_optimization_recommendations": "Compute Optimizer / Cost Optimization Hub / IAM Access Analyzer（未実装）",
    },
)


class AwsProvider(CloudProvider):
    profile = PROFILE
