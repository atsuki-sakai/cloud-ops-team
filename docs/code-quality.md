# コード品質ゲート（Python）

English: [docs/en/code-quality.md](en/code-quality.md)

このリポジトリの Python は、人が書いても AI が書いても同じルールで検査します。ルールを書く場所は2つだけです。

| 置き場所 | 中身 |
|---|---|
| `pyproject.toml` の `[tool.ruff.lint]` / `[tool.ty]` | ruff のルール群とパスごとの例外、ty の厳格設定 |
| `sgconfig.yml` と `lint/ast-grep/rules/` | プロジェクト固有の ast-grep ルール（例外処理・N+1・クラウド非依存・抑制の理由） |

CI、`make lint`、Claude Code のフックはすべて、この2つを読みます。どれか1つだけ厳しい、ということはありません。

## 検査が走るタイミング

| タイミング | 対象 | 何をするか |
|---|---|---|
| Claude Code が Edit / Write / MultiEdit した直後（PostToolUse） | 編集した `.py` 1つ | ruff・ruff format・ty・ast-grep を並列に実行。エラーなら終了コード 2 で、理由と直し方を AI に返して直させる。警告は止めずに伝える |
| Claude Code が応答を終える前（Stop） | `git diff` と未追跡の `.py` 全部 | 同じ検査をもう一度。エラーが残っていれば終了を止める（最大2回。その後は終了を許し、残りを画面に表示） |
| `make check-py [FILES=...]` | 指定ファイル、省略時は変更ファイル | フックと同じ検査を手で実行 |
| `make lint` と CI | リポジトリ全体 | ruff・ruff format・codespell・ty・`ast-grep scan`・`ast-grep test` |

フックの設定は `.claude/settings.json`、本体は `scripts/quality/hook.py` です。標準ライブラリだけで書かれ、`.venv/bin` のツールを直接呼びます（`uv run` は起動が遅いため使いません）。1ファイルの検査は手元で約 0.2 秒です。

AI が書く前に読む決まりは `.claude/skills/python-quality/SKILL.md` にあります。Skill のルール一覧と `lint/ast-grep/rules/` が一致しているかは、テストで検査しています。

## 主な決まり

- **型**: `app/` は ty の全ルールがエラー。関数の引数と戻り値には型注釈が必須（`ANN`）です。`tests/` と `scripts/` は外部 JSON や偽物を扱うため、緩い設定にしています。
- **例外処理**: 広い `except Exception` を書けるのは、境界（ツール・プロバイダ・通信・ルーター）だけです。理由付きの `# noqa: BLE001 -- 理由` を付け、`app/core/errors.py` の `tool_error(exc, ...)` で返します。`tool_error` は `status` / `error_type` / `message` を返し、`message` は `redact` で秘匿化したうえで 300 文字に切ります。それ以外の場所は具体的な例外だけを受け、`raise ... from exc` で投げ直します。
- **セキュリティ**: bandit 相当の `S` を有効にしています。`assert` による検証、`shell=True`、`requests`、`app/tools/` からのクラウド SDK の import は禁止です。
- **遅いコード**: `PERF`・`ASYNC` に加えて、ループの中で外部 API を呼ぶと `api-call-in-loop`（警告）が出ます。件数に上限があり、まとめて取る API もない場合だけ、理由と上限を書いて抑制します。
- **N+1 の実行時テスト**: 構文だけのルールでは、ヘルパー経由の呼び出しを見落とします。そのため `tests/unit/test_api_call_counts.py` で、件数を増やしても API の呼び出し回数が増えないことを確かめています。呼び出し回数は `tests/unit/conftest.py` の `api_calls` fixture で数えます。
- **抑制**: `# noqa: CODE -- 理由`、`# ast-grep-ignore: rule-id -- 理由`、`# ty: ignore[rule] -- 理由` の形だけを認めます。理由のない抑制と、ファイル全体の `# ruff: noqa` はエラーになります。

パスごとの例外は `pyproject.toml` の `per-file-ignores` を参照してください。指紋の式を変えないため、`app/core/fingerprint.py` は S324 と FURB167（正規表現フラグの書き換え）だけを対象外にしています。

## 止める・弱める

環境変数 `CLOUD_OPS_PYCHECK` で切り替えます。

- `enforce`（既定）: エラーなら差し戻す
- `warn`: 止めずに指摘だけ返す
- `off`: 検査しない

フックの設定は、Claude Code のセッション開始時に読み込まれます。変更は次のセッションから効きます（または `/hooks` で確認）。

## フックが動かないとき

- ツールが見つからない、時間切れ、フック自体のエラー: 編集は止めず、画面に `[py-quality] SETUP: ...` を表示します。多くの場合 `uv sync --extra lint` で直ります。
- 実行ごとの記録が `.claude/logs/py-quality.jsonl`（git 管理外）に残ります。中身は、ファイル、ルール、件数、ツールごとの所要時間です。
- フックの試し方: `make check-py FILES=app/tools/cloud.py`

## ルールを足す・ツールを上げる

1. ast-grep ルールは `lint/ast-grep/rules/<id>.yml` に書きます。`message`（何が悪いか）と `note`（直し方。フックがそのまま返す）を必ず書いてください。
2. 正しい例（valid）とダメな例（invalid）を `lint/ast-grep/rule-tests/<id>-test.yml` に書き、`uv run ast-grep test --skip-snapshot-tests` で確かめます。
3. Skill のルール一覧に `` `ast-grep:<id>` `` を足します（足さないと `tests/unit/test_code_quality.py` が落ちます）。
4. ruff・ty・ast-grep は `pyproject.toml` で版を固定しています。版を上げると差し戻されるコードが変わります。上げるときは、既存コードの修正と同じ PR で行ってください。
