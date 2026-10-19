# 設定一覧

English: [docs/en/configuration.md](en/configuration.md)

設定はすべて環境変数で行います。手元では `.env`（`make setup` で作成します。ひな形は [`.env.example`](../.env.example)）、
デプロイ後は Cloud Run サービスの環境変数（`make gcp-deploy` が `.env` の許可リスト内の値を設定します）を使います。
設定を変えたら `make doctor` で設定とアクセス権を点検し、実行結果も確認してください。
現在の保証範囲と人による補完確認は [既知の制限](known-limitations.md) にまとめています。

## エージェントの設定（`app/config.py`）

### 何を監視するか

| 変数 | 既定値 | 説明 |
|---|---|---|
| `OPS_CLOUD_PROVIDER` | `gcp` | `gcp`、`aws`、`azure`、またはサードパーティのプロバイダーのキー（[providers.md](providers.md)） |
| `OPS_TARGET` | プロバイダーごとに異なる（下表） | 監視対象の範囲。GCP のプロジェクト、AWS のアカウント ID、Azure のサブスクリプション ID のいずれか |
| `OPS_TARGET_REGION` | プロバイダーごとに異なる（下表） | 監視するワークロードのリージョン |
| `GITHUB_REPO` | — | ISSUE と PR を管理しているリポジトリ（`owner/repo`） |
| `GITHUB_TOKEN` | 手元では `gh auth token` の値 | GitHub のトークン（`GH_TOKEN` でも受け付けます） |

`OPS_TARGET` / `OPS_TARGET_REGION` を設定していない場合は、プロバイダーごとに次の変数から読み取ります。

| プロバイダー | 監視対象の読み取り元 | リージョンの読み取り元 | 既定のリージョン |
|---|---|---|---|
| `gcp` | `GCP_TARGET_PROJECT`、`GOOGLE_CLOUD_PROJECT` | `GCP_TARGET_REGION` | `asia-northeast1` |
| `aws` | `AWS_ACCOUNT_ID` | `AWS_REGION`、`AWS_DEFAULT_REGION` | `ap-northeast-1` |
| `azure` | `AZURE_SUBSCRIPTION_ID` | `AZURE_LOCATION` | `japaneast` |

### 安全のための設定

| 変数 | 既定値 | 説明 |
|---|---|---|
| `OPS_DRY_RUN` | `true` | お試しモード。GitHub には一切書き込まず、作成する予定の内容をプレビューとして返します |
| `OPS_AUTO_CLOSE_ISSUES` | `false` | 週次レビューで修正を確認できた ISSUE を自動でクローズします（`false` のときは `ops:verified` ラベルとコメントを付けるだけです） |
| `OPS_MAX_ISSUES_PER_RUN` | `10` | `create_ops_issue` の新規起票上限。実行単位で複数担当が予算を共有し、同じセッションの次の依頼でもリセットします。`publish_report` は対象外です |
| `OPS_RECURRENCE_COOLDOWN_HOURS` | `24` | ISSUE の `updated_at` からこの時間以内なら再発コメントを省略します。人による編集も影響し、複数インスタンス間で投稿を一度に抑える保証はありません |

真偽値は `true/false`、`yes/no`、`on/off`、`1/0`（大文字小文字を区別しません）を受け付けます。
未設定・空値は既定値を使い、それ以外の値は設定エラーとして起動を止めます。
`make setup` が保存する `.env` とそのバックアップは所有者だけが読み書きでき、
`.env.backup-*` は Git とデプロイのアップロード対象から除外されます。

通常起票の予算は実行ごとにリセットします。レポート起票への共通予算の適用と、実行間の重複防止は未実装です。
自動クローズを有効にする前に、PR のリポジトリ・修正関係、CI、配置、ログの観測範囲を人が確認してください。
参照 PR と修正 PR の識別には [既知の制限](known-limitations.md) が残っています。

### 実行履歴と証拠の保存

| 変数 | 既定値 | 説明 |
|---|---|---|
| `OPS_EXECUTION_DB_PATH` | 空（無効） | 専用の永続領域に置く SQLite の絶対パス。設定時は全登録ジョブと対話経由の担当ジョブを保存し、保存できない場合は後続処理を止めます |
| `OPS_EXECUTION_MAX_EVIDENCE_BYTES` | `8388608` | 保護済み証拠 1 件の上限（8 MiB）。超過を無断で切り詰めて保存完了にはしません |
| `OPS_EXECUTION_RETENTION_SECONDS` | `2592000` | 証拠本文の保持期間（30 日）。期限後は読出不可。本文の削除は運用者が `purge_expired(scope)` を定期実行します |

上限・保持期間は正の整数のみを受け付け、誤入力は設定エラーにします。
保存先の所有者・権限、配信の識別と再実行、終了状態は[実行履歴ガイド](execution-history.md)を参照してください。
標準デプロイスクリプトは保存領域の作成やこの 3 変数の転送を行いません。
Cloud Run の一時領域で複数インスタンスや再配置をまたぐ永続性は保証できません。

### 動作

| 変数 | 既定値 | 説明 |
|---|---|---|
| `OPS_REPORT_LANGUAGE` | `ja` | レポート・ISSUE・コメントの言語。`ja` または `en` |
| `OPS_ERROR_LOOKBACK_HOURS` | `24` | ジョブが `hours` を指定しないときに、エラー監視がさかのぼる時間 |
| `OPS_TIMEZONE` | `Asia/Tokyo` | レポートに書く時刻のタイムゾーン |
| `OPS_STACK_KEYWORDS` | プロバイダーの既定値と一般的な OS・ランタイム | CISA KEV（実際に悪用が確認された脆弱性の一覧）の絞り込みと、Web での脅威調査の焦点に使うキーワード（カンマ区切り） |
| `OPS_MODEL` | `gemini-3.8-flash` | すべてのエージェントが使うモデル |
| `OPS_RESEARCH_MODEL` | `OPS_MODEL` の値 | Web での脅威調査に使うモデル（Google 検索によるグラウンディングに対応している必要があります） |

### 自分のリポジトリとプロジェクトに合わせる

| 変数 | 既定値 | 説明 |
|---|---|---|
| `OPS_ERROR_LABELS` | `bug` | エラーの ISSUE に追加で付けるラベル（カンマ区切り。空にすると付けません） |
| `OPS_SECURITY_LABELS` | `security` | セキュリティの ISSUE に追加で付けるラベル |
| `OPS_SEVERITY_LABELS` | `critical=severity: critical,high=severity: high,medium=severity: medium,low=severity: low` | 重要度ごとに付けるラベル（1 つの重要度に複数付けるときは `high=a\|b`） |
| `OPS_CONTEXT_FILE` | `config/ops-context.md` | 指示文の末尾に追加するプロジェクトの事情。独自パスはローカルで利用できますが、標準デプロイスクリプトはこの変数を転送しません（[デプロイ時の補完設定](deployment-gcp.md)） |

どの ISSUE にも、チームが内部で使う `ops-agent` と、`ops:error` / `ops:security` / `ops:report` のいずれかのラベルが必ず付きます。

### 定期実行の呼び出しの認証（サーバー）

| 変数 | 既定値 | 説明 |
|---|---|---|
| `OPS_TRIGGER_AUDIENCE` | `make gcp-deploy` がサービスの URL を設定 | Cloud Scheduler が送る Google 署名付きの OIDC トークンを、ADK がこの audience で検証します。空の場合、認証は Cloud Run の IAM に任せます（ローカル開発時も同様） |
| `OPS_TRIGGER_SERVICE_ACCOUNTS` | `<JOB_PREFIX>-invoker@<project>` | audience を設定したときに、ジョブの起動を許可する呼び出し元 |

A2A カードの接続先は `APP_URL`（サービスの外部 URL）から組み立てます。
未設定時は `http://0.0.0.0:8000/a2a/app` を広告します。`make gcp-deploy` は `APP_URL` も転送しないため、
A2A を使う場合は [デプロイ手順](deployment-gcp.md) に従って配置後に設定し、カードの接続先を確認してください。

### モデルへのアクセス

| 変数 | 説明 |
|---|---|
| `GOOGLE_GENAI_USE_ENTERPRISE=true`、`GOOGLE_CLOUD_PROJECT`、`GOOGLE_CLOUD_LOCATION=global` | ADC（アプリケーションのデフォルト認証情報）で Vertex AI 上の Gemini を使います（既定） |
| `GEMINI_API_KEY` | Vertex AI の代わりに Gemini API を使います（どのクラウドからでも使えます） |

### Google Cloud プロバイダー

| 変数 | 既定値 | 説明 |
|---|---|---|
| `OPS_GCP_AUTH` | `adc` | `adc` は実行用サービスアカウント、または `gcloud auth application-default login` の認証情報を使います。`gcloud` は `gcloud auth print-access-token` のトークンを借ります（手元の ADC が別のアカウントになっているときに便利です） |

## 定期実行の設定（`make gcp-schedule`）

定期実行ジョブは、`app/specialists/` の各担当（専門家）が宣言しているジョブから生成されます。一覧は `make jobs` で確認できます。
どの値も上書きできます。`make gcp-schedule` を再実行すると反映され、既存のジョブはそのまま更新されます。

登録済みジョブは全サーバー入口で同じ引数検証を通ります。`hours`は1〜168、週次レビューの`days`は1〜90、
月次診断の`kev_days`は1〜365の整数です。JSONの数値文字列・bool・未知の項目・対象やリポジトリの上書きを拒否し、
範囲外の値を別の値へ丸めて実行しません。対話から選んだ担当も、同じ検証と固定収集へ進みます。

| 変数 | 既定値 | 説明 |
|---|---|---|
| `ERROR_WATCH_CRON` / `ERROR_WATCH_HOURS` | `0 8 * * *` / `24` | エラー監視（毎日 8:00）と、さかのぼる時間。さかのぼる時間は実行間隔と同じにしてください |
| `ACCESS_WATCH_CRON` / `ACCESS_WATCH_HOURS` | `30 8 * * *` / `24` | 不審アクセス調査（毎日 8:30）と、さかのぼる時間。調査窓は実行間隔以上に設定してください |
| `INFRASTRUCTURE_REVIEW_CRON` | `0 10 * * 1` | インフラ・費用・権限の見直し（毎週月曜 10:00） |
| `WEEKLY_CRON` / `WEEKLY_DAYS` | `0 9 * * 1` / `7` | 週次の ISSUE・PR レビュー |
| `MONTHLY_CRON` / `MONTHLY_KEV_DAYS` | `0 9 1 * *` / `30` | 月次セキュリティ診断 |
| `<SCHEDULE_KEY>_CRON` | ジョブの既定値 | 自分で追加したジョブ用。例: スケジュールキーが `deploy-watch` なら `DEPLOY_WATCH_CRON` |
| `TZ_NAME` | `Asia/Tokyo` | cron 式のタイムゾーン |
| `ONLY_JOBS` | すべて | 更新するジョブのスケジュールキー（カンマ区切り）。例: `error-watch` |
| `JOB_PREFIX` / `INVOKER_SA` | `cloud-ops` / `<prefix>-invoker` | ジョブ名（`<prefix>-error-watch` など）と、サービスを呼び出すサービスアカウント |
| `PROJECT_ID`、`REGION`、`CLOUD_RUN_SERVICE` / `SERVICE`、`AGENT_URL` | `.env` の値。URL は自動で検出 | ジョブの呼び出し先 |
| `ATTEMPT_DEADLINE` | `1800s` | 1 ジョブあたりの最大実行時間（Cloud Run のリクエストタイムアウトにも同じ値を設定します） |
| `SKIP_SERVICE_UPDATE` | — | `1` にすると、サービスへの呼び出し権限の付与とタイムアウトの更新を省略します |

例:

```bash
ERROR_WATCH_CRON="0 * * * *" ERROR_WATCH_HOURS=1 ONLY_JOBS=error-watch make gcp-schedule        # 毎時
ERROR_WATCH_CRON="0 */6 * * *" ERROR_WATCH_HOURS=6 ONLY_JOBS=error-watch make gcp-schedule      # 6 時間ごと
ERROR_WATCH_CRON="30 9 * * 1-5" ERROR_WATCH_HOURS=72 ONLY_JOBS=error-watch make gcp-schedule    # 平日のみ。月曜は週末分も見る
MONTHLY_CRON="0 9 15 * *" ONLY_JOBS=monthly-security-audit make gcp-schedule                    # 診断を毎月 15 日に
ACCESS_WATCH_CRON="0 9 * * *" ACCESS_WATCH_HOURS=24 ONLY_JOBS=access-watch make gcp-schedule    # 不審アクセス調査を毎朝 9 時に
INFRASTRUCTURE_REVIEW_CRON="0 10 * * 5" ONLY_JOBS=infrastructure-review make gcp-schedule      # インフラ見直しを金曜に
```

設定を変えずにジョブを一時停止・再開するには、次のようにします。

```bash
gcloud scheduler jobs pause  cloud-ops-error-watch --location asia-northeast1
gcloud scheduler jobs resume cloud-ops-error-watch --location asia-northeast1
```

さかのぼる時間を実行間隔より長くすると、同じエラーを再び取得します。既存 ISSUE はフィンガープリントで検索しますが、
複数実行が同時に検索・起票すると重複が残る可能性があります。実行の重なりを避け、ISSUE と再発コメントを確認してください。
観測範囲を広げると、モデルのトークン消費も増えます。

`ACCESS_WATCH_HOURS` は定期ジョブの `hours` を設定します。手動では `make run JOB=access_watch ARGS="--hours 24"` で指定します。
`ACCESS_WATCH_CRON`・`INFRASTRUCTURE_REVIEW_CRON`・`TZ_NAME` を変更したら `make gcp-schedule` を再実行してください。
`OPS_TIMEZONE` はレポートの表示時刻、`TZ_NAME` は定期実行の時刻に使うため、そろえて設定すると分かりやすくなります。

不審アクセス調査とインフラ見直しは既存の `OPS_DRY_RUN`、起票上限、伏せ字、重複防止を使います。
費用改善案はレポートにまとめ、セキュリティの critical / high は既存の `ops:security` ISSUE として扱います。
データ収集の前提と追加の閲覧権限は [デプロイ手順](deployment-gcp.md#アクセス調査とインフラ見直しの取得条件) を確認してください。

## デプロイの設定（`make gcp-deploy`）

`make gcp-deploy` は `.env` を読み込んでから `scripts/gcp/deploy_cloud_run.sh` を実行します。
エージェント設定のうち `scripts/gcp/deploy_env.py` の許可リストに含まれるものに加えて、次の変数を受け付けます。
`OPS_CONTEXT_FILE` と `APP_URL` は現在このリストに含まれず、配置後の補完設定が必要です。

| 変数 | 既定値 | 説明 |
|---|---|---|
| `PROJECT_ID` | `GOOGLE_CLOUD_PROJECT` | デプロイ先のプロジェクト |
| `REGION` | `OPS_TARGET_REGION` | デプロイ先のリージョン |
| `SERVICE` | `cloud-ops-team` | Cloud Run のサービス名 |
| `RUNTIME_SA` | `<SERVICE>@<PROJECT_ID>.iam.gserviceaccount.com` | サービスを実行するサービスアカウント |
| `TARGET_PROJECT` / `TARGET_REGION` | `OPS_TARGET` / `OPS_TARGET_REGION` | 監視対象のプロジェクトとリージョン |
| `GITHUB_TOKEN_SECRET` | `<SERVICE>-github-token` | GitHub トークンを保存した Secret Manager のシークレット名 |
| `MEMORY` / `CPU` | `2Gi` / `1` | インスタンスのメモリと CPU |
| `MIN_INSTANCES` / `MAX_INSTANCES` | `0` / `2` | インスタンス数の下限と上限 |
| `CONCURRENCY` | `4` | 1 インスタンスが同時に処理するリクエスト数 |
| `TIMEOUT` | `1800` | リクエストのタイムアウト（秒） |

このスクリプトは許可した設定を一時ファイルに書き、`--env-vars-file` で渡すため、
カンマ・縦棒・空値を含むラベル設定もそのまま保持します。一時ファイルは終了時に削除します。
デプロイのたびにサービスの環境変数がすべて置き換わります。
GitHub に書き込ませたいサービスでは、デプロイのたびに `OPS_DRY_RUN=false` を指定してください。
