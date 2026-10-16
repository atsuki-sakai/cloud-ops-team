<!--
Project context for the ops team.

Copy this file to config/ops-context.md and describe YOUR system. The text is appended to every agent's
instructions, so the team triages with your knowledge instead of guessing. Keep it short (it is capped at
8,000 characters) and factual. Do not put secrets here: the file is deployed with the service.

このファイルを config/ops-context.md にコピーして、あなたのシステムについて書いてください。
内容はすべてのエージェントの指示に追加されます。短く、事実だけを書き、秘密情報は書かないでください。
-->

# サービス構成
- `api`: 公開 REST API（Cloud Run）。認証は Firebase Auth。ユーザー影響が最も大きい。
- `worker`: 定期ジョブとキュー処理（Cloud Scheduler / Cloud Tasks から呼ばれる）。
- `web`: Next.js のフロントエンド。
- DB: Cloud SQL for PostgreSQL（`app` データベース）。

# 重要度の考え方
- 決済・予約・ログインに関わるエラーは、1 回でも high 以上として扱う。
- 管理画面だけで起きるエラーは medium まで。

# 既知のノイズ（起票しなくてよいもの）
- `/healthz` への単発の 404。ボットの探索パスは攻撃成功の証拠ではないが、急増や認証失敗などとの組み合わせは不審アクセス調査で確認する。
- 毎日 03:00 のバックアップ中に出る `connection to client lost` の単発ログ。

# 担当と連絡
- 障害対応の一次担当: @your-github-handle
- セキュリティの ISSUE には `team: security` ラベルも付けたい（OPS_SECURITY_LABELS で設定）。

# 修正の進め方
- ISSUE は日本語で、推奨対応には該当しそうなファイルパス（`backend/src/...`）を書いてほしい。

# 不審アクセス調査の前提（実際の環境に合わせて記入）
- 許可された負荷試験・脆弱性検査: 実施日、対象サービス、担当、変更管理番号を記載する。
- 重要操作の正規の実施予定: IAM 変更、サービスアカウント鍵の作成、ログ設定変更などの作業時間と理由を記載する。
- ログ収集状況: Data Access 監査ログ、ロードバランサーのアクセスログ、Cloud Armor ログの有効化・除外・サンプリング・保持期間を記載する。
- 証拠が不足する場合は「未確認」。正規の作業かどうか分からない重要操作を、不正アクセス確定と扱わない。

# インフラ見直しの制約（実際の環境に合わせて記入）
- 可用性・応答時間の要件、ピーク時刻、最低インスタンス数が必要な理由を記載する。
- 待機系・災害対策・季節処理用など、利用率が低くても維持が必要なリソースを記載する。
- 権限削減時に維持すべきバッチ・運用作業と、変更可能な時間帯を記載する。
- 推奨案には影響・リスク・確認方法・ロールバック案を含め、費用見積もりが取得できない場合は金額を推測しない。
