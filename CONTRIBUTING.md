# コントリビューションガイド

English: [CONTRIBUTING.en.md](CONTRIBUTING.en.md)

協力していただきありがとうございます。バグ報告、ドキュメントの修正、新しいチェックの追加、そして何より **AWS と Azure の機能の実装** を歓迎します。

## 開発環境の準備

```bash
uv tool install google-agents-cli
git clone https://github.com/atsuki-sakai/cloud-ops-team && cd cloud-ops-team
make setup                    # 依存関係を入れて .env を作る（OPS_DRY_RUN=true のまま）
make doctor                   # Gemini・GitHub・クラウドへのアクセスを点検
```

ユニットテストと lint にはクラウドの認証情報は不要です。

```bash
make test                     # uv run pytest tests/unit。数秒で終わり、ネットワークも使わない
make lint                     # ruff、ruff format、codespell、ty
```

Gemini とクラウドにアクセスできる場合は、次も実行できます。

```bash
make run JOB=error_watch                 # ジョブを 1 回実行し、すべてのツール呼び出しを表示
uv run pytest tests/integration          # モデルを呼び出す統合テスト
make eval                                # 振る舞いの評価（LLM が採点）
```

## どこを変更するか

| 変更内容 | 場所 | 先に読むもの |
|---|---|---|
| クラウドの機能に対応する | `app/providers/<cloud>/` | [docs/providers.md](docs/providers.md) |
| 担当（専門家）やジョブを追加する | `app/specialists/`、`app/routing.py`、`app/workflows.py` | [docs/customization.md](docs/customization.md) |
| 調査ツールを追加する | `app/tools/`、担当の母集団、固定収集または工程別の許可リスト | [docs/customization.md の 5 章](docs/customization.md#5-調査ツールを追加する) |
| エージェントの振る舞いを変える | `app/specialists/<name>.py` の `INSTRUCTION`、共通部分は `app/prompts.py` | `tests/eval/` の評価ケース |
| デプロイ | `scripts/<cloud>/` と `docs/deployment-<cloud>.md`、`docs/en/deployment-<cloud>.md` | [docs/deployment-gcp.md](docs/deployment-gcp.md) |

## 守ってほしいルール

- **クラウドに対しては読み取り専用です。** ツールやプロバイダーがクラウドのリソースを変更してはいけません。変更を伴う呼び出しを追加する PR はお断りします。
- **お試しモードを尊重してください。** GitHub に書き込む処理は、必ず `CONFIG.dry_run` に従ってください。
- **登録と実行許可を分けます。** ジョブには厳密な引数の定義と固定収集の手順が必要です。モデルへの公開範囲は`app/tool_scope.py`で制限し、拡張時は`tests/eval/routing/`の比較ケースを更新します。
- **ツールの決まりごと**は `tests/unit/test_providers.py` で検査されます。すべての引数に型ヒントを付け、既定値は付けません。
  モデル向けの docstring を書き、`status` キーを持つ JSON 化できる `dict` を返し、API エラーで例外を投げないでください。
- **ツールの結果に秘密情報や個人情報を含めないでください。** 自由記述のテキストは `app.core.redact.redact` で伏せ字にし、
  環境変数は名前だけを返します。
- **フィンガープリントの計算式（`app/core/fingerprint.py`）は変えないでください。** 変えると、開いているすべての ISSUE が
  重複して起票されます。変更するとゴールデンテストが失敗します。
- **ユニットテストはオフラインで動かします。** SDK の応答はスタブにしてください。統合テストと評価はモデルを呼び出して構いません。
- **プロンプトの変更には根拠を添えてください。** 変更の前後で `agents-cli eval run` を実行し、スコアを PR に書いてください
  （`agents-cli eval compare old.json new.json`）。
  担当・ツール・引数・実行工程を変える場合は、[実行記録の比較評価](tests/eval/routing/README.md)も行い、未実施・欠測・不合格を明記してください。

## ドキュメントの言語

ドキュメントは **日本語がメイン、英語がサブ** です。

| 日本語（メイン） | 英語 |
|---|---|
| `README.md` | `README.en.md` |
| `docs/*.md` | `docs/en/*.md` |
| `CONTRIBUTING.md` | `CONTRIBUTING.en.md` |
| `CHANGELOG.md` | `CHANGELOG.en.md` |

ドキュメントを変えるときは両方の言語を更新してください。片方しか書けない場合は PR にその旨を書いてもらえれば、メンテナーが翻訳します。

## プルリクエスト

1. 大きな変更（新しいプロバイダーや新しい担当など）は、先に ISSUE を立てて方向性をすり合わせてください。
2. PR は目的を 1 つに絞り、ドキュメントと変更履歴（Unreleased の欄）も同じ PR で更新してください。
3. CI（lint、型チェック、ユニットテスト）が通っている必要があります。
4. どのように確認したかを書いてください。プロバイダーの場合は、実際のアカウントに対してお試しモードでどの機能を実行したかを書いてください。

コントリビューションは Apache License 2.0 のもとで提供されることに同意し、
[行動規範](CODE_OF_CONDUCT.md) に従うものとします。
