---
name: python-quality
description: >-
  cloud-ops-team の Python（app/・scripts/・tests/）を書く・直す・レビューするときに必ず使う。
  型・例外処理・セキュリティ・遅いコード（N+1）・ADK ツールの書き方をこのリポジトリの決まりに固定する。
  編集後フック（scripts/quality/hook.py）の指摘を直すときもこの Skill に従う。
---

# cloud-ops-team の Python の書き方

このリポジトリの Python は、書いた直後にフックが `ruff`・`ruff format`・`ty`・`ast-grep` で検査する。
違反があると編集は差し戻され、「直し方」付きの一覧が返る。最初から下の決まりで書けば差し戻されない。

## 優先順位

ぶつかったときは上が勝つ。

1. フックの指摘と `CLAUDE.md`（とくに `app/core/fingerprint.py` の式を変えない、クラウドを変更するツールを作らない）
2. この Skill
3. `google-agents-cli-adk-code` Skill（ADK の API の使い方。google-adk 2.x で確認。任意の参考資料で、依存しない）
4. Google Python Style Guide。ただし次の点はこのリポジトリが優先:
   - 行の長さは 88 桁（`ruff format` に任せる。E501 は無視）
   - import は `from app.x import y` でよい。ADK のツールはモジュールでなく実体を import する
     （`from google.adk.tools.load_web_page import load_web_page`）
   - 例外処理は下の「境界」の決まりに従う（Google の「広い except を避ける」より具体的）

## 書く前の手順

1. 触るファイルの層を確かめる: ツール（`app/tools/`）、プロバイダ（`app/providers/<cloud>/`）、
   通信（`app/integrations/`、`app/providers/gcp/client.py`）、ルーター/エージェント、内部（`app/core/` など）。
2. 似た既存コードを1つ読んで形を合わせる（境界の例: `app/tools/cloud.py` の `_call`）。
3. 書いたら、フックの指摘をすべて直してから次へ進む。直せない指摘は勝手に抑制せず、下の「抑制」の決まりに従う。
4. 手元で全体を確かめるとき: `make check-py`（変更ファイル）/ `make lint`（全体）。

## 1. 型（`ruff:ANN` と ty の厳格設定）

- 関数の引数と戻り値には必ず型注釈を付ける（`app/` は ty の全ルールがエラー）。
- `Any` は ADK や外部 JSON の境目だけ。受け取ったら `isinstance` で絞ってから使う。
- `dict | list` を返す関数の結果は、使う前に `isinstance(payload, dict)` で絞る。
- 型エラーは型を直して消す。`# ty: ignore[rule] -- 理由` は stub の誤りに限る。

## 2. 例外処理（`ruff:BLE`・`ruff:TRY`・`ruff:B` の B904）

境界（ツール・プロバイダ・通信・ルーター）だけが広く受けてよい。返す形は1つ:

```python
from app.core.errors import tool_error

try:
    out = provider.list_services()
except Exception as exc:  # noqa: BLE001 -- tool boundary: a provider bug must not crash the run
    return tool_error(exc, provider=key, capability="list_services")
```

- `tool_error` は `status` / `error_type` / 秘匿化して 300 文字に切った `message` を返す。
  例外の文字列も出したくなければ `tool_error(exc, message="固定の説明")`。
- 境界でない場所は具体的な例外だけを受け、投げ直すときは `raise NewError("固定の説明") from exc`。
- 例外の文字列（`str(exc)`、`f"{exc}"`）を返り値・メッセージに直接入れない（`ast-grep:exception-text-unredacted`）。
- except の中で `{"status": "error", ...}` を手で組み立てない（`ast-grep:boundary-error-dict`）。
- ログには型名だけ: `logger.warning("... %s", type(exc).__name__)`。

## 3. セキュリティ（`ruff:S`・`ruff:TID`）

- `assert` で検証しない（`python -O` で消える）。`if` と例外で書く。
- `subprocess` に `shell=True` や外部入力を混ぜた文字列を渡さない。固定の引数リストだけ。
- HTTP は `httpx`（`requests` は禁止）。タイムアウトは明示する。
- 秘密情報・トークンを返り値やログに入れない。外へ出す文字列は `app.core.redact.redact` を通す。
- `app/tools/` はクラウド SDK や `app.providers.<cloud>` を import しない。`get_provider()` 経由だけ
  （`ast-grep:tools-cloud-neutral`）。
- クラウドのリソースを変更するツールは作らない。GitHub への書き込みは `CONFIG.dry_run` に従う。

## 4. 遅いコード・N+1（`ruff:PERF`・`ruff:ASYNC`）

- ループや内包表記の中で外部 API（`gh_rest`、`gcp_get`、`httpx.*` など）を1件ずつ呼ばない
  （`ast-grep:api-call-in-loop`、警告）。優先順:
  1. 一覧 API を1回呼んで `dict` で引く
  2. GitHub は GraphQL でまとめて取る
  3. 並列にするなら上限付き（`ThreadPoolExecutor(max_workers=N)` / `Semaphore`）。上限なしの `gather` は禁止
- 件数に上限があり、まとめる API もない場合だけ、理由と上限を書いて抑制する。
- 外部 API を呼ぶ処理を足したら、`tests/unit/conftest.py` の `api_calls` fixture で呼び出し回数を数えるテストを書く
  （例: `tests/unit/test_api_call_counts.py`。件数を増やしても呼び出し回数が増えないことを確かめる）。
- `async def` の中で同期 I/O（HTTP、ファイル、`subprocess`）を呼ばない。`asyncio.to_thread` を使う。
- ループで `append` するより内包表記か `extend` を使う。

## 5. ADK ツールの形

`tests/unit/test_providers.py::test_agent_tools_follow_adk_conventions` と
`tests/unit/test_code_quality.py::test_registered_adk_tools_return_dicts` が登録済みツールを全部検査する。

- docstring 必須（モデルが読む）。`Args:` に各引数を書く。`tool_context` は書かない。
- 引数は全部型注釈付き、デフォルト値なし（`tool_context` を除く）。
- 戻り値は `-> dict[str, Any]`（または `dict`）。成功は `{"status": "ok", ...}`、失敗は `tool_error(...)`。
- ツールの中で `try` を重ねない。プロバイダ呼び出しは `app/tools/cloud.py` の `_call` に任せる。

## 6. その他（`ruff:SIM`・`ruff:RET`・`ruff:PTH`・`ruff:DTZ`・`ruff:T20`・`ruff:FURB`・`ruff:ERA`）

- パスは `pathlib.Path`。日時はタイムゾーン付き（`dt.datetime.now(dt.UTC)`）。
- プロセス全体のキャッシュ（トークン・取得済みカタログ）は `global` で書き換えず、`app.core.cache.TtlValue` を使う。
- `print` ではなく `logging`。コメントアウトしたコードを残さない。

## 抑制の決まり（`ast-grep:suppression-needs-reason`、`ruff:PGH`）

抑制は最後の手段で、必ず「コード + ` -- ` + 理由」で書く。理由のない抑制とファイル全体の `# ruff: noqa` はエラー。

```python
except Exception as exc:  # noqa: BLE001 -- tool boundary: ...
# ast-grep-ignore: api-call-in-loop -- bounded: at most 6 Dockerfiles, no batch endpoint
value = sdk.call()  # ty: ignore[invalid-argument-type] -- google-genai stub omits Optional
```

同じ指摘が2回直しても消えないときは、繰り返さずに理由付きで抑制するか、人に確認する。

## ルール一覧（この表と `lint/ast-grep/rules/` は `tests/unit/test_code_quality.py` で一致を検査）

| ID | 重さ | 内容 |
|---|---|---|
| `ast-grep:exception-text-unredacted` | エラー | except の中で例外の文字列を秘匿化せずに使う |
| `ast-grep:boundary-error-dict` | エラー | except の中でエラー辞書を手で組み立てる（`tool_error` を使う） |
| `ast-grep:suppression-needs-reason` | エラー | 理由のない `noqa` / `ast-grep-ignore` / `type: ignore` |
| `ast-grep:tools-cloud-neutral` | エラー | `app/tools/` からのクラウド SDK・個別プロバイダの import |
| `ast-grep:api-call-in-loop` | 警告 | ループ・内包表記の中での外部 API 呼び出し（N+1） |

ruff のルール群と理由は `pyproject.toml` の `[tool.ruff.lint]`、全体の説明は `docs/code-quality.md`。
個々の ruff ルールの意味は `.venv/bin/ruff rule <CODE>` で確かめる。
