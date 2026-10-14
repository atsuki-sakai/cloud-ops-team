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
- `/healthz` への 404、ボットによる `/wp-login.php` などへの 404。
- 毎日 03:00 のバックアップ中に出る `connection to client lost` の単発ログ。

# 担当と連絡
- 障害対応の一次担当: @your-github-handle
- セキュリティの ISSUE には `team: security` ラベルも付けたい（OPS_SECURITY_LABELS で設定）。

# 修正の進め方
- ISSUE は日本語で、推奨対応には該当しそうなファイルパス（`backend/src/...`）を書いてほしい。
