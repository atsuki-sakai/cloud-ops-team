## 変更内容と理由 / What and why

<!-- 関連する ISSUE をリンクしてください / Link the issue: closes #123 -->

## 確認方法 / How it was tested

- [ ] `uv run pytest tests/unit`
- [ ] `agents-cli lint`
- [ ] お試しモードでジョブを実行した（`scripts/run_job.py ...`）。関係するツール呼び出しかレポートの抜粋を貼る / Ran a job in dry-run mode — paste the relevant tool calls or report excerpt
- [ ] プロンプトを変えた場合: 変更前後の `agents-cli eval run` のスコアを下に書く / Prompt changes: `agents-cli eval run` before / after (scores below)

## チェックリスト / Checklist

- [ ] クラウドに対して読み取り専用で、GitHub への書き込みは `OPS_DRY_RUN` に従う / Read-only towards the cloud; GitHub writes honour `OPS_DRY_RUN`
- [ ] ツールの結果に秘密情報や個人情報を含めない（伏せ字化し、環境変数は名前だけ） / No secrets or personal data in tool results (redacted, env var names only)
- [ ] ドキュメント（日本語と英語）と変更履歴の Unreleased を更新した / Docs (Japanese and English) and the changelog (Unreleased) updated
