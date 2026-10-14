# クラウドプロバイダー

English: [docs/en/providers.md](en/providers.md)

プロバイダーは、1 つのクラウドを、エージェントが使う読み取り専用の **機能（capability）** の決まった一覧に対応させる部品です。
エージェント・プロンプト・ツール名はどのクラウドでも同じで、変わるのはプロバイダーだけです（`OPS_CLOUD_PROVIDER` で選びます）。

| プロバイダー | キー | 状態 | コード |
|---|---|---|---|
| Google Cloud | `gcp` | 実装済み（18 の機能すべて） | [`app/providers/gcp/`](../app/providers/gcp/) |
| Amazon Web Services | `aws` | 雛形。すべての機能が `not_supported` を返します | [`app/providers/aws/__init__.py`](../app/providers/aws/__init__.py) |
| Microsoft Azure | `azure` | 雛形。すべての機能が `not_supported` を返します | [`app/providers/azure/__init__.py`](../app/providers/azure/__init__.py) |

雛形のプロバイダーでも役に立ちます。リポジトリの診断、CISA KEV、サポート期限（EOL）の確認、最新の攻撃動向の調査、週次の PR 確認は
クラウドへのアクセスが不要で、未対応の機能はエージェントがすべて「確認できなかった項目」として報告します。
機能は 1 つずつ実装できます。コントリビューションを歓迎します。

## 守るべき約束

`app/providers/base.py` に `CloudProvider` が定義されています。これを継承して `profile` を設定し、対応する機能のメソッドを上書きします。

```python
from app.providers.base import CloudProvider, ProviderProfile

PROFILE = ProviderProfile(
    key="aws",
    display_name="Amazon Web Services",
    target_label="account",          # project / account / subscription のいずれか
    target_label_ja="アカウント",     # 日本語のプロンプトで使用
    cli="aws",                       # 対処例のコマンドで使用
    stack_keywords=("Amazon", "AWS"),
    glossary={"list_services": "ECS services / Lambda functions", ...},  # 機能ごとに 1 項目
)

class AwsProvider(CloudProvider):
    profile = PROFILE

    def list_services(self) -> dict:
        ...  # self.target はアカウント ID、self.region はリージョン
```

ルール:

1. **読み取り専用。** リソースを変更する API は決して呼びません。
2. **`status` を持つ dict を返す。** 値は `ok`、または `api_disabled`・`permission_denied`・`not_found`・`rate_limited`・`error` のいずれかです。
   失敗時は短い `message` と、役に立つ場合は人間向けの直し方を示す `hint`（有効にすべき API、不足しているロールなど）を付けます。
   API の失敗で例外を投げないでください。ラッパーが例外を `status: error` に変換しますが、明確なメッセージのほうが役に立ちます。
3. **応答を小さく保つ。** 一覧には上限を設け（GCP プロバイダーはエラーグループ最大 30 件、ログ 200 件、デプロイ 20 件まで）、
   長い文字列は切り詰め、ページングはサーバー側で行います。
4. **伏せ字にする。** ログの抜粋や自由記述のテキストは `app.core.redact.redact` を通します。環境変数は名前だけを返し、値は返しません。
5. **共通の形を使う。** 監査は `app.core.findings.finding(...)` で作った検出結果を `app.core.findings.result(check, findings, summary)` で返します。
   エラーグループのフィンガープリントは `app.core.fingerprint` で作ります。
6. **設定** は `app.config.CONFIG`（`target`、`region`）と、このファイルに記載するプロバイダー固有の環境変数
   （たとえば `OPS_AWS_LOG_GROUPS`）から読みます。

## 機能一覧

| 機能 | シグネチャ | 使う担当 |
|---|---|---|
| `summarize_error_groups` | `(hours: int)` | エラー監視 |
| `list_error_logs` | `(hours: int, service_name: str, limit: int)` | エラー監視 |
| `get_log_context` | `(service_name: str, around_time: str, window_minutes: int, trace: str)` | エラー監視 |
| `count_matching_errors` | `(service_name: str, search_text: str, since: str)` | エラー監視、PR 確認 |
| `list_services` | `()` | エラー監視 |
| `list_deployments` | `(service_name: str, limit: int)` | エラー監視 |
| `list_scheduled_jobs` | `()` | エラー監視 |
| `list_databases` | `()` | エラー監視、セキュリティ診断 |
| `get_request_error_rates` | `(hours: int)` | エラー監視 |
| `get_alerting_setup` | `()` | エラー監視、セキュリティ診断 |
| `audit_iam` | `()` | セキュリティ診断 |
| `audit_credentials` | `()` | セキュリティ診断 |
| `audit_compute` | `()` | セキュリティ診断 |
| `audit_databases` | `()` | セキュリティ診断 |
| `audit_network` | `()` | セキュリティ診断 |
| `audit_security_services` | `()` | セキュリティ診断 |
| `list_security_findings` | `(max_findings: int)` | セキュリティ診断 |
| `scan_deployed_images` | `()` | セキュリティ診断 |

### 共通形式の結果

**必須（required）** と書いたフィールドは、プロンプトと PR 確認が前提にしているものです。モデルの判断に役立つフィールドは自由に追加してかまいません。

**`summarize_error_groups`**

```json
{"status": "ok", "target": "...", "window": {"start": "...Z", "end": "...Z", "hours": 24},
 "total_entries": 175, "truncated": false, "group_count": 4, "by_service": {"worker": 88},
 "groups": [{
   "fingerprint": "15685eff3c9da72b",        // 必須。fingerprint(resource_type, service, message_template(msg))
   "resource_type": "cloud_run_revision",
   "service": "worker",                      // 必須
   "template": "http <n> post ...",
   "count": 86, "first_seen": "...", "last_seen": "...",   // 必須
   "severities": ["ERROR"], "revisions": ["worker-00088"],
   "sample_message": "HTTP 500 POST https://.../api/v1/worker/reconcile",  // 必須。伏せ字済み
   "sample_traces": ["..."],
   "search_text": "/api/v1/worker/reconcile"   // 必須。search_text_from(url, message)
 }]}
```

グループ化するのは多くても数千件までにし、件数の多い上位 30 グループを返します。`count_matching_errors` が同じエラーを再び
見つけられるように、`app.core.fingerprint.search_text_from(url, message)` を使ってください。

**`list_error_logs`、`get_log_context`**: `{"status", "count", "entries": [{"timestamp", "severity", "service", "revision",
"message", "http_status", "trace", "insert_id"}]}`（メッセージは伏せ字済み）。

**`count_matching_errors`**: `{"status", "since", "count"（必須）, "capped", "first_seen", "last_seen", "latest_samples"}`。
`search_text` はメッセージとリクエスト URL の両方に照合します。

**`list_services`**: `{"status", "target", "region", "count", "services": [{"name"（必須）, "uri", "ready"（必須）,
"update_time"（必須）, "images", "service_account", "scaling", "env_plain_names", "env_secret_names", "labels"}]}`。

**`list_deployments`**: `{"status", "service", "count", "revisions": [{"name", "create_time"（必須）, "images", "ready"}]}`。
新しい順に並べます。

**`list_scheduled_jobs`**: `{"status", "count", "failing_jobs"（必須）, "jobs": [{"name", "schedule", "time_zone", "state",
"last_attempt_time", "last_status_code", "last_status_message", "failing", "target"}]}`。

**`list_databases`**: `{"status", "count", "instances": [{"name", "state", "engine"（必須。`postgresql`・`mysql`・`mssqlserver`
などの endoflife.date のスラッグ）, "engine_version"（必須。メジャーバージョン）, "availability_type", "public_ip_enabled",
"backup_enabled", "pitr_enabled", "deletion_protection", "available_maintenance_versions"}]}`。

**`get_request_error_rates`**: `{"status", "window_hours", "services": [{"service", "requests", "5xx", "4xx", "5xx_ratio",
"4xx_ratio"}]}`。

**`get_alerting_setup`**: `{"status", "alert_policy_count", "alert_policies": [{"display_name", "enabled", "conditions",
"notification_channels"}], "uptime_check_count", "uptime_checks": [...]}`。

**監査（`audit_*`）**: `app.core.findings.result(...)` の形で返します。

```json
{"status": "ok", "check": "network", "finding_counts": {"high": 1},
 "findings": [{"check_id": "fw.sensitive_port", "severity": "high", "title": "...", "resource": "default-allow-ssh",
               "detail": "...", "recommendation": "..."}],
 "summary": {...}}
```

`check_id` は変わらない値にしてください。セキュリティ診断は `"<check_id>:<resource>"` をフィンガープリントとして ISSUE を登録します。
重要度の目安: `critical`（データの公開や管理権限の露出）、`high`（悪用可能な設定ミス）、`medium`（強化の余地）、
`low`（ベストプラクティス）、`info`（意図的な設定、または情報提供）。

**`list_security_findings`**: `{"status", "count", "counts", "findings": [{"category", "severity", "resource", "event_time",
"explanation", "recommendation"}], "truncated"}`。

**`scan_deployed_images`**: `{"status", "image_count", "images": [{"image", "used_by", "total", "by_severity",
"fix_available", "top_findings": [{"cve", "severity", "package", "version", "fixed_version"}]}]}`。

## 各クラウドのサービスとの対応

### Google Cloud（実装済み）

| 機能 | 取得元 |
|---|---|
| ログ | Cloud Logging `entries:list`（`severity>=ERROR`）。`resource.labels.*` でグループ化 |
| サービス / デプロイ | Cloud Run Admin API v2 のサービスとリビジョン |
| 定期実行ジョブ | Cloud Scheduler のジョブ |
| データベース | Cloud SQL Admin のインスタンス |
| エラー率 / アラート | Cloud Monitoring の `run.googleapis.com/request_count`、アラートポリシー、稼働時間チェック |
| IAM / 認証情報 | Resource Manager の `getIamPolicy`、IAM のサービスアカウントキー |
| コンピュート | Cloud Run のサービスと `getIamPolicy` |
| データベースの監査 | Cloud SQL の設定、メンテナンスバージョン |
| ネットワーク | Compute のファイアウォール（重要度の判断に VM の台数も使用） |
| セキュリティサービス | Service Usage で有効になっている API |
| セキュリティ検出結果 | Security Command Center の `sources/-/findings` |
| イメージ | トラフィックを受けているダイジェストに対する Container Analysis のオカレンス |

### AWS（未実装。boto3 を使用）

| 機能 | 取得元の候補 |
|---|---|
| ログ | `OPS_AWS_LOG_GROUPS` に対する `logs.start_query` / `get_query_results`（CloudWatch Logs Insights）。HTTP ステータスは ALB のアクセスログ |
| サービス | `ecs.list_services` と `describe_services`、`lambda.list_functions`、`apprunner.list_services` |
| デプロイ | `ecs.describe_services`（`deployments`）、`lambda.list_versions_by_function` |
| 定期実行ジョブ | `scheduler.list_schedules`、`events.list_rules`（失敗した呼び出しのメトリクスも） |
| データベース | `rds.describe_db_instances` / `describe_db_clusters`（`Engine`、`EngineVersion`） |
| エラー率 | `cloudwatch.get_metric_data`（`HTTPCode_Target_5XX_Count`、API Gateway の `5XXError`） |
| アラート | `cloudwatch.describe_alarms`、`route53.list_health_checks` |
| IAM | `iam.get_account_summary`（root の MFA）、認証情報レポート、`AdministratorAccess` のアタッチ状況、`accessanalyzer.list_findings` |
| 認証情報 | `iam.list_users` で一覧を取り、`list_access_keys`、`get_access_key_last_used` の順に確認 |
| コンピュート | ECS タスク定義（シークレットらしき値が平文の環境変数になっていないか）、Lambda の設定、公開されたロードバランサー |
| データベースの監査 | `PubliclyAccessible`、`StorageEncrypted`、`BackupRetentionPeriod`、`DeletionProtection`、`describe_pending_maintenance_actions` |
| ネットワーク | `ec2.describe_security_groups`（22、3389、DB のポートが 0.0.0.0/0 または ::/0 に開いていないか） |
| セキュリティサービス | `guardduty.list_detectors`、`securityhub.describe_hub`、`inspector2.batch_get_account_status`、Config のレコーダー、CloudTrail の証跡 |
| セキュリティ検出結果 | `securityhub.get_findings`（`RecordState=ACTIVE`、重要度 CRITICAL/HIGH/MEDIUM） |
| イメージ | 稼働中のサービスが使う ECR イメージに対する `inspector2.list_findings` |

監視対象は `OPS_TARGET` または `AWS_ACCOUNT_ID`、リージョンは `OPS_TARGET_REGION`、`AWS_REGION`、`AWS_DEFAULT_REGION` のいずれかで指定します。
認証情報は boto3 の既定の取得順に従います（ECS / Lambda では IAM ロール、手元では `AWS_PROFILE`）。読み取り専用の ID には、
AWS 管理ポリシーの `ReadOnlyAccess` と `SecurityAudit` を使えます。

### Azure（未実装。azure-identity、azure-monitor-query、azure-mgmt-* を使用）

| 機能 | 取得元の候補 |
|---|---|
| ログ | `OPS_AZURE_WORKSPACE_ID` に対する `LogsQueryClient`（KQL）: `AppExceptions`、`AppRequests`、`ContainerAppConsoleLogs_CL` |
| サービス | `azure-mgmt-web`（App Service、Functions）、`azure-mgmt-appcontainers`（Container Apps） |
| デプロイ | Container Apps のリビジョン、App Service のデプロイ |
| 定期実行ジョブ | Container Apps Jobs の実行履歴、Logic Apps の実行、Functions のタイマートリガー |
| データベース | PostgreSQL / MySQL フレキシブルサーバー、Azure SQL（`version`） |
| エラー率 | `MetricsQueryClient`（`Http5xx`、`requests/failed`） |
| アラート | `azure-mgmt-monitor` のメトリックアラートとログアラートのルール、Application Insights の可用性テスト |
| IAM | `azure-mgmt-authorization` のロール割り当て（サブスクリプション範囲の Owner / Contributor、ゲストのプリンシパル） |
| 認証情報 | Microsoft Graph のアプリケーション / サービスプリンシパルのパスワードとキー（作成からの経過期間、有効期限） |
| コンピュート | 公開されたイングレス、マネージド ID、平文のアプリ設定 |
| データベースの監査 | パブリックネットワークアクセス、0.0.0.0–255.255.255.255 を許可するファイアウォールルール、TLS、バックアップの保持期間 |
| ネットワーク | `azure-mgmt-network` の NSG ルール（重要なポートが Any / Internet に開いていないか） |
| セキュリティサービス | `azure-mgmt-security` の Defender for Cloud の価格レベル |
| セキュリティ検出結果 | Defender for Cloud の評価とアラート |
| イメージ | Defender for Containers の脆弱性評価（ACR） |

監視対象は `OPS_TARGET` または `AZURE_SUBSCRIPTION_ID`、リージョンは `OPS_TARGET_REGION` または `AZURE_LOCATION` で指定します。
認証情報は `DefaultAzureCredential` を使います（Azure 上ではマネージド ID、手元では `az login`）。読み取り専用の ID には、
`Reader` と `Security Reader` のロールに加えて、ワークスペースに対する `Log Analytics Reader` を付けます。

## 機能を実装する手順

1. 上の表から機能を 1 つ選び、同じメソッドの GCP 実装を読んで、返すべき形を確認します。
2. SDK を任意の依存関係として追加し（`[project.optional-dependencies] aws = ["boto3>=1.35"]`）、プロバイダーのモジュール内で
   import します。こうすると、ほかのクラウドの利用者はその SDK を入れずに済みます。
3. プロバイダークラスのメソッドを上書きします。読み取り専用を守り、共通形式のフィールドを返します。
4. 「(未対応 / not supported)」の表示は自動で外れます。`capabilities()` が上書きされたメソッドを検出し、プロンプトの用語集と
   `get_run_context` が自動で更新されます。
5. SDK の応答をスタブにしたユニットテストを追加します（ユニットテストではネットワークを使いません）。
   `tests/unit/test_providers.py` の契約テストが、シグネチャ、プロファイル、未対応時の振る舞いを確認します。
6. `OPS_CLOUD_PROVIDER=aws OPS_DRY_RUN=true uv run python scripts/run_job.py error_watch --hours 24` で、お試しモードのまま通しで試します。
7. プロバイダー固有の環境変数を、このファイル（日本語版と英語版）と `.env.example` に記載します。

## 外部パッケージのプロバイダー

このリポジトリをフォークしなくても、別のパッケージとしてプロバイダーを配布できます。

```toml
# あなたのパッケージの pyproject.toml
[project.entry-points."cloud_ops_team.providers"]
oci = "my_package.provider:OciProvider"
```

cloud-ops-team と同じ環境にインストールし、`OPS_CLOUD_PROVIDER=oci` を設定します。テストや組み込み用途では
`app.providers.register_provider("fake", FakeProvider)` を呼び出します。
