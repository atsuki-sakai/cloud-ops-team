# gcp-ops-team — GCP 運用保守エージェントチーム

> **English summary** — gcp-ops-team is an autonomous operations team for Google Cloud projects, built with
> [ADK](https://adk.dev/) and [agents-cli](https://github.com/google/agents-cli). Three specialist agents run on a schedule:
> **error_watch** (hourly) groups Cloud Logging errors by fingerprint, correlates them with 5xx rates, Scheduler failures and
> recent deploys, and files GitHub issues with a root-cause analysis; **security_audit** (monthly) reviews IAM, service-account keys,
> Cloud Run, Cloud SQL, firewall rules, enabled security APIs, SCC findings, container vulnerabilities, EOL status, CISA KEV and
> web threat intel; **pr_review** (weekly) checks whether the PRs linked to those issues were merged, CI passed and the error did not
> recur, then marks issues `ops:verified` or `ops:stale`. Humans (with AI coding tools) implement the fixes. The team is read-only
> against GCP, dry-run by default, deduplicates by log fingerprints stored in the issues themselves, and runs on Cloud Run triggered by
> Cloud Scheduler. Design details (Japanese): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Licensed under Apache-2.0.

Google Cloud プロジェクトの運用保守を、[ADK](https://adk.dev/) + [agents-cli](https://github.com/google/agents-cli) で作った
3 人の専門家エージェントが分担します。エージェントは **調査と GitHub ISSUE の起票・検証だけ** を行い、修正は人間（＋AI コーディング）が PR で実装します。

| ジョブ | 周期（既定） | 担当 | やること |
|---|---|---|---|
| `error_watch` | 毎時 | error_watch（SRE） | Cloud Logging のエラーをフィンガープリント単位に集約し、5xx 率・Scheduler 失敗・直近デプロイと突き合わせて原因分析。`[ops/error]` ISSUE を起票（同じエラーは再発コメント、24h に 1 回） |
| `monthly_security_audit` | 毎月 1 日 09:00 | security_audit（クラウドセキュリティ） | IAM / SA 鍵 / Cloud Run / Cloud SQL / ファイアウォール / 有効 API / SCC / コンテナ脆弱性 / EOL / CISA KEV / Web 脅威動向を診断。critical・high を `[ops/security]` ISSUE、全体を月次レポート ISSUE に |
| `weekly_pr_review` | 毎週月曜 09:00 | pr_review（リリースマネージャー） | チーム起票 ISSUE に紐づく PR のマージ・CI・ログ再発を検証し `ops:verified` / `ops:stale` を付与。週次レポート ISSUE を発行 |

自然言語でも話しかけられます（「直近 24 時間のエラーを見て」「脆弱性診断して」「PR の進捗を確認して」）。

## 仕組みの概要

```
Cloud Scheduler ──(OIDC)──▶ Cloud Run: POST /apps/app/trigger/pubsub
                                  │  {"data": {"job": "error_watch", "hours": 2}}
                                  ▼
                        gcp_ops_team（リーダー）
                     ┌────────────┼────────────┐
                     ▼            ▼            ▼
               error_watch  security_audit  pr_review      ← mode="single_turn" のサブエージェント
                     │            │            │
            Logging/Monitoring  IAM/SQL/FW/SCC  GitHub PR/CI     ← すべて読み取り専用ツール
            Run/Scheduler       KEV/EOL/Web検索  Logging(再発)
                     └────────────┼────────────┘
                                  ▼
                GitHub ISSUE（起票・再発コメント・検証済みラベル・週次/月次レポート）
```

- 専門家は ADK の `mode="single_turn"` サブエージェントとしてリーダーのツールになり、同じセッション内でインライン実行されます（ツール呼び出しが全部ストリーム・トレースされる）。
- 重複排除は GitHub ISSUE 自体を台帳にします。ログを正規化した **fingerprint** を ISSUE 本文の `<!-- ops-meta -->` に埋め込み、起票前に突合します。
- 週次レビューは、ISSUE に `closes #N` で紐づいた PR のマージ後に、同じエラーが再発していないかを Cloud Logging で数えて検証します。

設計の詳細（構成図、シーケンス、ツールカタログ、ISSUE ライフサイクル、安全設計、認証、デプロイ構成）は **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** にまとめています。

## 安全設計

- **読み取り専用**: GCP を変更するツールは存在しません。GitHub も ISSUE の作成・コメント・ラベルだけ。
- **`OPS_DRY_RUN=true`（既定）**: GitHub に書き込まず「起票予定」を返します。本番で有効化するときだけ `false` に。
- **自動クローズは既定で無効**: 週次レビューは `ops:verified` ラベルとコメントまで。`OPS_AUTO_CLOSE_ISSUES=true` で実際にクローズ。
- **暴走防止**: 1 実行の起票は `OPS_MAX_ISSUES_PER_RUN`（既定 10）まで。
- **秘匿情報のマスク**: ログ抜粋のトークン・鍵・メール・公開 IP・カード番号をマスクしてから ISSUE に載せます。環境変数は名前のみ扱います。

## クイックスタート（ローカル）

```bash
uv tool install google-agents-cli      # 未インストールの場合
agents-cli install                     # uv sync
cp .env.example .env                   # 値を編集
```

`.env` の主な設定（全項目は `.env.example` と docs/ARCHITECTURE.md §11）:

| 変数 | 意味 |
|---|---|
| `GCP_TARGET_PROJECT` / `GCP_TARGET_REGION` | 監視・診断対象のプロジェクトとリージョン |
| `GITHUB_REPO` | ISSUE / PR を扱うリポジトリ（`owner/repo`） |
| `GITHUB_TOKEN` | 未設定ならローカルでは `gh auth token` を使用 |
| `OPS_GCP_AUTH` | `adc`（既定）または `gcloud`。ローカルで ADC が別アカウントのときは `gcloud` |
| `OPS_DRY_RUN` / `OPS_AUTO_CLOSE_ISSUES` / `OPS_MAX_ISSUES_PER_RUN` | 安全弁（上記） |

```bash
# ジョブを 1 回実行（dry-run なら GitHub には書き込まれない）
agents-cli run '{"data": {"job": "error_watch", "hours": 24}, "attributes": {}}'
uv run python scripts/run_job.py monthly_security_audit --kev-days 30   # 長いジョブ向け。全ツール呼び出しを表示

# 対話で試す
agents-cli playground

# Scheduler / Pub/Sub push と同じ形式でローカルサーバーに投げる
uv run uvicorn app.fast_api_app:app --port 8000 &
./scripts/trigger_local.sh error_watch '"hours":2'
```

## 必要な権限

**GCP（対象プロジェクト、すべて viewer 系）** — `scripts/grant_target_roles.sh` がランタイム SA に付与します:
`serviceusage.serviceUsageConsumer`, `browser`, `logging.viewer`, `monitoring.viewer`, `run.viewer`, `cloudscheduler.viewer`,
`cloudsql.viewer`, `compute.networkViewer`, `iam.securityReviewer`, `securitycenter.findingsViewer`,
`containeranalysis.occurrences.viewer`, `artifactregistry.reader`。
ランタイム SA にはさらに `aiplatform.user`（Vertex AI）、`secretmanager.secretAccessor`、`logging.logWriter`、`cloudtrace.agent` が必要です。

API が無効な項目（Security Command Center、コンテナスキャンなど）は「未有効化」として診断結果に記録され、月次レポートで有効化を提案します。

**GitHub** — fine-grained token の場合: Issues (write), Pull requests (read), Actions (read), Contents (read), Metadata (read)。
ブランチ保護や Dependabot 設定の確認まで行うには Administration (read) も付けてください（無くても診断は続行します）。

## デプロイと定期実行（Cloud Run）

```bash
# 1. ランタイム SA と GitHub トークンの Secret
gcloud iam service-accounts create gcp-ops-team --project <project>
echo -n "<fine-grained PAT>" | gcloud secrets create gcp-ops-team-github-token --data-file=- --project <project>
for r in roles/aiplatform.user roles/secretmanager.secretAccessor roles/logging.logWriter roles/cloudtrace.agent roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding <project> --member serviceAccount:gcp-ops-team@<project>.iam.gserviceaccount.com --role $r; done
TARGET_PROJECT=<target> AGENT_SA=gcp-ops-team@<project>.iam.gserviceaccount.com ./scripts/grant_target_roles.sh

# 2. デプロイ（ソースから Cloud Build。OPS_DRY_RUN=false で本番起票を有効化）
PROJECT_ID=<project> GITHUB_REPO=owner/repo OPS_DRY_RUN=true ./scripts/deploy_cloud_run.sh

# 3. Cloud Scheduler を配線（HTTP ターゲット + OIDC、attempt deadline 30 分、再試行 0）
PROJECT_ID=<project> AGENT_URL=https://<cloud-run-url> CLOUD_RUN_SERVICE=gcp-ops-team ./scripts/setup_schedules.sh

# 4. 手動実行
gcloud scheduler jobs run gcp-ops-error-watch --location asia-northeast1 --project <project>
```

`agents-cli deploy` ではなく `scripts/deploy_cloud_run.sh` を使うのは、agents-cli がローカル `.env` を丸ごと環境変数に流し込むためです
（ローカル専用の `GOOGLE_APPLICATION_CREDENTIALS` やカンマ入りの `OPS_STACK_KEYWORDS` が壊れる）。
周期は `ERROR_WATCH_CRON` / `WEEKLY_CRON` / `MONTHLY_CRON` で変更できます（例: `ERROR_WATCH_CRON="0 9 * * *" ERROR_WATCH_HOURS=24` で 1 日 1 回）。
Terraform / CI/CD が必要になったら `agents-cli scaffold enhance . --deployment-target cloud_run` で後から追加できます。

## 運用手順（Runbook）

| やりたいこと | コマンド |
|---|---|
| 直近の実行を見る | `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="gcp-ops-team" AND httpRequest.requestUrl:"trigger/pubsub"' --limit 10 --format="value(timestamp,httpRequest.status,httpRequest.latency)"` |
| エージェントが作った ISSUE | `gh issue list -R owner/repo --label ops-agent --state all` |
| 起票を止める / 再開する | `gcloud run services update gcp-ops-team --region asia-northeast1 --update-env-vars OPS_DRY_RUN=true`（`false` で再開） |
| ジョブを一時停止 | `gcloud scheduler jobs pause gcp-ops-error-watch --location asia-northeast1`（`resume` で再開） |
| GitHub トークンの更新 | `echo -n "<token>" \| gcloud secrets versions add gcp-ops-team-github-token --data-file=-` → 次のリクエストから反映（`:latest`） |
| 対応完了の流れ | PR 本文に `closes #<番号>` → マージ → 月曜の週次レビューが再発 0 件と CI 成功を確認して `ops:verified` を付与 → 人間がクローズ |

## ISSUE の規約

- タイトル接頭辞: `[ops/error]` `[ops/security]` `[ops/report]`
- ラベル: 全件 `ops-agent` + 種別 `ops:error` / `ops:security` / `ops:report` + 重要度 `priority: critical` / `priority: high` / `major` / `minor`（無ければ作成）
- 本文末尾の `<!-- ops-meta: {...} -->` に fingerprint・service・search_text を保持し、重複検知と週次の再発確認に使います。

## 開発

```bash
uv run pytest tests/unit                                   # 純粋ロジックのテスト（ネットワーク不要）
uv run pytest tests/integration                            # ストリーミング / A2A（LLM 呼び出しあり）
agents-cli lint                                            # ruff / codespell / ty
agents-cli eval run                                        # 振る舞いの評価（tests/eval/datasets/）
agents-cli eval run --dataset tests/eval/holdout/holdout-dataset.json   # 月次診断など重いホールドアウト
```

```
app/
  agent.py              # リーダー + 3 専門家（single_turn サブエージェント）の配線
  prompts.py            # 日本語の指示（役割・手順・予算・ISSUE テンプレ）
  config.py             # 環境変数 → OpsConfig
  fast_api_app.py       # ADK サーバー（Pub/Sub 形式トリガー有効）
  tools/
    observability_tools.py  # Logging / Cloud Run / Scheduler / Monitoring（読み取り）
    security_tools.py       # IAM / SA 鍵 / Cloud Run / Cloud SQL / FW / API / SCC / イメージ / KEV / EOL / GitHub 設定 / Web 脅威調査
    github_tools.py         # ISSUE 作成・重複検知・コメント・ラベル・PR / ワークフロー参照
    _gcp.py _github.py _redact.py _fingerprint.py
scripts/                # deploy_cloud_run.sh / setup_schedules.sh / grant_target_roles.sh / trigger_local.sh / run_job.py
tests/unit, tests/integration, tests/eval
docs/ARCHITECTURE.md    # 設計の詳細
.agents-cli-spec.md     # 仕様
```

## コントリビュート

Issue / PR を歓迎します。変更の前に `uv run pytest tests/unit` と `agents-cli lint` を通してください。
振る舞いに関わる変更は `agents-cli eval run` のスコアも確認してください。

## ライセンス

[Apache License 2.0](LICENSE)。`agents-cli` のスキャフォールド由来のファイル（Google LLC の Apache-2.0 ヘッダー付き）を含みます。
