# 変更履歴

English: [CHANGELOG.en.md](CHANGELOG.en.md)

主な変更をここに記録します。書式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に従い、
バージョン番号は [セマンティック バージョニング](https://semver.org/lang/ja/) に従います。

## [Unreleased]

### 修正
- モデルHTTP通信の無通信待機を60秒で制限し、HTTPX経由でSDKのタイムアウト再試行を有効にしました。エージェント全体や応答を受け取り始めたストリームは再試行しません。
- JSON・構造化データ内の秘密値、ISSUE/レポートのタイトルとメタデータの伏せ字漏れを修正しました。秘密値や伏せ字を含む検索語は再発ゼロの根拠に使いません。
- PR マージで自動クローズされた ISSUE が週次の修正検証から漏れる問題を修正しました。直近30日以内の未検証closed案件も確認します。
- お試しモードの誤入力で GitHub 書き込みが有効になる問題と、秘密ファイルのバックアップが除外対象外になる問題を修正しました。
- ログ集約の伏せ字漏れと、取得途中のログを「再発 0 件」と判断できてしまう問題を修正しました。
- ISSUE の対象環境の照合、100 件を超える一覧の取得、検証コメント・ラベルの失敗時のクローズ停止を修正しました。
- ファイアウォールの省略値・数値プロトコル、稼働中の全リビジョンのイメージ診断、MySQL の PITR 判定を修正しました。
- GitHub の権限不足を設定不備と断定する問題、raw ファイルのバージョン情報欠落、EOL API の不正応答処理を修正しました。
- 複数ラベルを含むデプロイ設定が引数の区切りと衝突する問題を修正し、オフラインの回帰テストを追加しました。

### 変更
- README と設計・設定・プロバイダー・デプロイ手順を、現在の起票上限・重複制御・監査範囲・実行結果の保存方式に合わせて更新しました。
  独自コンテキストパスと A2A URL の配置時の制約も記載しています。これはドキュメント更新で、以下の未修正項目の実装修正は含みません。
- 定期ジョブを厳密なJSON検証と固定収集で直接実行し、自然言語の依頼も担当別の受付から同じ経路へ接続します。分析AIに渡すツール・説明・履歴を工程ごとに限定しました。
- 期間・件数・対象・リポジトリの制約を実行時に検証し、収集不足や省略を実行単位の証拠に残します。取得関数・モデル・指紋の既存契約は維持します。
- ドキュメントを **日本語メイン** に切り替えました。`README.md`、`docs/*.md`、`CONTRIBUTING.md`、`CHANGELOG.md` が日本語で、
  英語版は `README.en.md`、`docs/en/*.md`、`CONTRIBUTING.en.md`、`CHANGELOG.en.md` です。
  `SECURITY.md` と ISSUE・PR のテンプレートは日英併記にし、`CODE_OF_CONDUCT.md` には日本語の要約を加えました。

### 追加
- [既知の制約と改善計画](docs/known-limitations.md) を日英で追加しました。追加レビューの 11 件のうち解消済み 1 件・未修正 10 件、条件付きリスク 2 件、
  暫定対処、責務の分割・共通化と今後必要な検証をまとめています。
- 担当・ツール・引数・前提条件・業務完了を旧構成と比較する評価CLIとケースを追加しました。証拠不足や未許可ツールの提示・呼び出しは公開判定を不合格にします。
- 毎日 08:30 に直近 24 時間を調べる `access_watch` を追加しました。監査ログ、HTTP 認証拒否・探索パス、Cloud Armor の拒否から、証拠・確度・未確認範囲付きで不審な兆候を報告します。
- 毎週月曜 10:00 の `infrastructure_review` を追加しました。GCP Recommender の費用・IAM 推奨事項と既存の構成監査をまとめ、影響・リスク・実施・確認・ロールバック案を提案します。クラウドの自動変更は行いません。
- 共通機能 `investigate_access(hours)` / `list_optimization_recommendations()`、定期実行の設定、導入先でのログ収集・追加閲覧権限の案内を追加しました。AWS / Azure はこの 2 機能も `not_supported` を返します。
- 設定一覧、Google Cloud へのデプロイ手順書、プロバイダーガイドの日本語版を追加しました。
  日本語のアーキテクチャ解説も、英語版のすべての内容をカバーするようにしました。

## [0.3.0] - 2026-10-14

### 追加
- **自分のプロジェクトで使える仕組み**: `make setup`（質問に答えて `.env` を作成）、`make doctor`（設定・Gemini・GitHub と、
  実際に使うアカウントでのクラウドの各機能を点検し、足りないものごとに直し方を表示）、`make run`、`make jobs`、`make chat`、
  `.env` を読む `make gcp-bootstrap / gcp-grant / gcp-deploy / gcp-schedule / gcp-logs`。
- **担当（専門家）の登録簿**（`app/specialists/`）: 担当ごとに 1 つのモジュールで、指示文・ツール・ジョブを定義します。
  雛形の `_template.py` も用意しました。リーダーの担当一覧と振り分け表、Cloud Scheduler のジョブはここから自動生成されます。
- **プロジェクトの事情**: `config/ops-context.md`（テンプレート付き）の内容が、すべての指示文に追加されます。
- ISSUE のラベルを設定で変えられるようにしました（`OPS_ERROR_LABELS`、`OPS_SECURITY_LABELS`、`OPS_SEVERITY_LABELS`）。
- 定期実行の呼び出し口の認証: `OPS_TRIGGER_AUDIENCE` と `OPS_TRIGGER_SERVICE_ACCOUNTS`。`make gcp-deploy` が自動で設定します
  （ADK 2.12 では呼び出し口の認証設定が必須です）。
- カスタマイズガイド（日本語と英語）とフォルダ構成の解説を追加しました。

### 変更
- 重要度ラベルの既定値を `severity: critical|high|medium|low` に変え、セキュリティの ISSUE には `security` だけを付けるようにしました。
  0.2 のラベルを使い続けるには `OPS_SEVERITY_LABELS=critical=priority: critical,high=priority: high,medium=major,low=minor`
  と `OPS_SECURITY_LABELS=security,infrastructure` を設定してください。
- 定期実行ジョブは担当のジョブ定義から作られるようになりました（`scripts/jobs.py`）。`setup_schedules.sh` はサービスの URL を
  自分で見つけ、新しいジョブには `<SCHEDULE_KEY>_CRON` で実行時刻を指定できます。
- GCP 用スクリプトは `.env` の名前（`GOOGLE_CLOUD_PROJECT`、`OPS_TARGET`、`OPS_TARGET_REGION`）も読むようにしました。
  デプロイスクリプトは GitHub トークンのシークレットがあるかを事前に確認します。
- 依存関係: google-adk 2.12、google-genai 2.30、opentelemetry-resourcedetector-gcp 1.14 に更新し、`eval` の追加依存で
  google-cloud-aiplatform 2.x を使えるようにしました（ロックは 2.4）。CI は actions/checkout v7 と astral-sh/setup-uv v7 を使います。

## [0.2.0] - 2026-10-13

### 変更
- プロジェクト名を `gcp-ops-team` から **cloud-ops-team** に変えました（ルートエージェントは `cloud_ops_team`）。
- **プロバイダー構成**を導入しました。エージェントはクラウドに依存しない 18 個の機能ツール（`list_services`、`audit_network`、
  `list_security_findings` など）を使い、実際の処理は `OPS_CLOUD_PROVIDER` で選んだプロバイダーに任せます。
  Google Cloud のコードは `app/providers/gcp/` に移しました。
- プロンプトはクラウドに依存しない形にし、プロバイダーの情報（表示名、監視対象の種類、CLI、用語集）から組み立てます。
- エラー監視の既定を **毎日 8:00、過去 24 時間分** にしました。すべての予定は設定で変えられます
  （`ERROR_WATCH_CRON`、`ERROR_WATCH_HOURS`、`WEEKLY_CRON`、`MONTHLY_CRON`、`ONLY_JOBS`、`JOB_PREFIX` など）。
- デプロイスクリプトを英語のドキュメント付きで `scripts/gcp/` に移しました。`deploy_cloud_run.sh` は汎用の `OPS_TARGET` 系の変数を使います。
- 重複の判定を、各 ISSUE に記録した監視対象の範囲内で行うようにしました。複数の環境で 1 つのリポジトリを共有できます
  （0.1.0 が作成した ISSUE も引き続き認識します）。

### 追加
- AWS と Azure のプロバイダーの雛形と、サービスの対応表を追加しました。未実装の機能は `not_supported` を返し、
  「未確認」として報告されます。
- `cloud_ops_team.providers` のエントリーポイントで外部のプロバイダーを追加できます。テスト用に `register_provider` もあります。
- `get_run_context` が、プロバイダーの実装済みの機能と未対応の機能を報告します。
- レポート・ISSUE・コメントの言語を選ぶ `OPS_REPORT_LANGUAGE`（`ja` / `en`）と、`OPS_MODEL`、`OPS_RESEARCH_MODEL` を追加しました。
- 初回セットアップ用の `scripts/gcp/bootstrap.sh` を追加しました。
- プロバイダーの契約テスト、フィンガープリントのゴールデンテスト、ツールの決まりごとのテストと、GitHub Actions の CI を追加しました。
- ドキュメント: アーキテクチャ（英語と日本語）、プロバイダー、設定、Google Cloud へのデプロイ、コントリビューションガイド、
  セキュリティポリシー、行動規範、ISSUE と PR のテンプレート。

### 修正
- `setup_schedules.sh` が既存のジョブを更新できなかった問題を直しました（更新時は `--update-headers` が必要）。

## [0.1.0] - 2026-10-13

### 追加
- `gcp-ops-team` として最初の公開リリース。Google Cloud 向けのエラー監視、月次セキュリティ診断、週次 PR レビュー
  （Cloud Logging、Cloud Run、Cloud Scheduler、Cloud SQL、Cloud Monitoring、IAM、SCC、Artifact Analysis）、
  フィンガープリントで重複を防ぐ GitHub ISSUE の起票、お試しモード、秘密情報の伏せ字化、Cloud Run へのデプロイと
  Cloud Scheduler の設定。

[Unreleased]: https://github.com/atsuki-sakai/cloud-ops-team/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/atsuki-sakai/cloud-ops-team/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/atsuki-sakai/cloud-ops-team/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/atsuki-sakai/cloud-ops-team/releases/tag/v0.1.0
