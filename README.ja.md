# cloud-ops-team

**クラウドのための AI 運用チーム。** 3 人の専門家エージェントが本番エラーを監視し、毎月セキュリティを診断し、
毎週「マージされた PR が本当に問題を直したか」を検証します。やり取りはすべて GitHub ISSUE で行います。

[English README](README.md) · [アーキテクチャ（日本語）](docs/ja/architecture.md) · [プロバイダー（GCP / AWS / Azure）](docs/providers.md) ·
[設定](docs/configuration.md) · [Google Cloud へのデプロイ](docs/deployment-gcp.md)

| ジョブ | 周期（既定） | 担当 | やること |
|---|---|---|---|
| `error_watch` | 毎日 08:00 | SRE | エラーログをフィンガープリント単位に集約し、5xx 率・失敗中のスケジュールジョブ・直近デプロイと突き合わせて原因仮説を立て、証跡と確認方法つきの `[ops/error]` ISSUE を起票。同じエラーは重複起票せず再発コメント |
| `monthly_security_audit` | 毎月 1 日 09:00 | クラウドセキュリティ | IAM・長期認証情報・コンピュート・DB・ネットワーク・セキュリティサービス・検出結果・コンテナ CVE・EOL・CISA KEV・最新の脅威動向を診断。critical / high を起票し月次レポートを発行 |
| `weekly_pr_review` | 毎週月曜 09:00 | リリースマネージャー | チーム起票 ISSUE ごとに、紐づく PR のマージ・CI・修正後のエラー再発を検証し `ops:verified` / `ops:stale` を付与。週次レポートを発行 |

自然言語でも使えます（「直近 24 時間のエラーを見て」「脆弱性診断して」「PR の進捗は？」）。

## 仕組み

- **読み取り専用**: クラウドを変更するツールは存在しません。GitHub への書き込みも ISSUE・コメント・ラベルだけで、
  既定（`OPS_DRY_RUN=true`）では書き込みません。
- **エージェントはクラウド非依存、クラウドは差し替え可能**: エージェントは 18 個の共通ツール（`list_services`、`audit_network` など）だけを使い、
  プロバイダーがそれを各クラウドの API に対応付けます。Google Cloud は実装済み、AWS / Azure は雛形があり機能単位で追加できます。
- **データベース不要**: ISSUE 自体が台帳です。ログのフィンガープリントを ISSUE に埋め込み、重複排除と修正後の再発確認に使います。
- **[Google ADK](https://adk.dev/) + [agents-cli](https://github.com/google/agents-cli)** で構築。専門家は `single_turn` サブエージェントとして動き、
  全ツール呼び出しがストリーム・トレースされます。

## 対応クラウド

| クラウド | 状態 |
|---|---|
| Google Cloud | ✅ 実装済み（Cloud Logging / Run / Scheduler / SQL / Monitoring、IAM・SA 鍵・FW・SCC・Artifact Analysis） |
| AWS | 🚧 雛形（CloudWatch Logs・ECS / Lambda・RDS・Security Hub・Inspector などを実装予定） |
| Azure | 🚧 雛形（Log Analytics・App Service / Container Apps・Defender for Cloud などを実装予定） |

どのクラウドでも、リポジトリ診断・CISA KEV・EOL チェック・Web 脅威調査・週次 PR レビューはすでに動きます。
プロバイダーの追加方法は [docs/providers.md](docs/providers.md) を参照してください。

## クイックスタート（ローカル）

```bash
git clone https://github.com/atsuki-sakai/cloud-ops-team && cd cloud-ops-team
agents-cli install
cp .env.example .env               # OPS_TARGET・GITHUB_REPO などを設定（既定は OPS_DRY_RUN=true）

uv run python scripts/run_job.py error_watch --hours 24          # 1 回実行し、全ツール呼び出しを表示
uv run python scripts/run_job.py monthly_security_audit --kev-days 30
agents-cli playground                                            # 対話で試す
```

## デプロイ（Google Cloud）

```bash
PROJECT_ID=my-project GITHUB_TOKEN=github_pat_xxx ./scripts/gcp/bootstrap.sh
PROJECT_ID=my-project GITHUB_REPO=owner/repo ./scripts/gcp/deploy_cloud_run.sh
PROJECT_ID=my-project AGENT_URL=https://... CLOUD_RUN_SERVICE=cloud-ops-team ./scripts/gcp/setup_schedules.sh
```

周期はすべて環境変数で変更できます。例えばエラー監視を毎時にするなら `ERROR_WATCH_CRON="0 * * * *" ERROR_WATCH_HOURS=1` を付けて再実行します。
手順と運用 Runbook は [docs/deployment-gcp.md](docs/deployment-gcp.md)、全設定は [docs/configuration.md](docs/configuration.md) にあります。

## 主な設定

| 変数 | 既定 | 意味 |
|---|---|---|
| `OPS_CLOUD_PROVIDER` | `gcp` | `gcp` / `aws` / `azure` |
| `OPS_TARGET` / `OPS_TARGET_REGION` | — | 監視対象（プロジェクト / アカウント / サブスクリプション）とリージョン |
| `GITHUB_REPO` | — | `owner/repo` |
| `OPS_DRY_RUN` | `true` | GitHub に書き込まずプレビューを返す |
| `OPS_AUTO_CLOSE_ISSUES` | `false` | 検証済み ISSUE を週次レビューが自動クローズ |
| `OPS_REPORT_LANGUAGE` | `ja` | レポート・ISSUE の言語（`ja` / `en`） |

## コントリビュート

Issue / PR を歓迎します（特に AWS / Azure の機能実装）。[CONTRIBUTING.md](CONTRIBUTING.md) を参照してください。
脆弱性の報告は [SECURITY.md](SECURITY.md) の手順でお願いします。

## ライセンス

[Apache License 2.0](LICENSE)。agents-cli のスキャフォールド由来のファイル（Google LLC、Apache-2.0）を含みます。
