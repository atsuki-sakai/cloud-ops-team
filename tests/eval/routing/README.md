# 担当・ツール選択の比較評価 / Routing comparison gate

このローカルCLIは、実行から取得したJSON記録を比較します。モデル呼び出し、GCP操作、GitHub書き込みは行いません。既存のADK用7ケースは維持し、`cases.json`に定期ジョブ、別名、類似する自然文、未知ジョブ、引数不正、バックアップ確認と作成の区別、部分取得・権限不足を追加しています。新しいバックアップ機能などの完成を待たず、現在の未対応境界も評価できます。

## 実行 / Usage

```bash
uv run python scripts/evaluate_routing.py \
  --suite tests/eval/routing/cases.json \
  --baseline artifacts/routing/baseline.json \
  --candidate artifacts/routing/candidate.json \
  --min-samples 3 \
  --output artifacts/routing/comparison.json
uv run pytest tests/unit/test_routing_evaluation.py
```

終了コードは`0`=候補が比較条件を満たす、`1`=評価済みだが不合格、`2`=入力・証拠・比較条件の不備です。CIでは終了コードをそのまま使ってください。出力JSONの`passed`も確認できます。入力不備の場合も、古い成功結果が残らないよう`--output`を失敗結果で更新します。

Exit codes: `0` passed, `1` evaluated but failed, `2` invalid or incomparable evidence. Preserve this exit code in CI. The CLI never collects traces or invokes an LLM.

## 実行記録 / Run format

以下は**形式の説明用**であり、実行済み結果ではありません。実行記録を期待値から作成したり、同一結果のコピーを独立した標本として扱わないでください。

```json
{
  "schema_version": 1,
  "run_id": "baseline-unique-id",
  "revision": "55a7e1fd93344d4f8ddfb007dc9af6036b869d3f",
  "mode": "runtime_stubbed",
  "suite_sha256": "SHA256 of the exact suite file bytes",
  "conditions": {
    "model": "configured-model-name",
    "model_config": {"temperature": 0},
    "snapshot_id": "hashed-fixture-bundle-id",
    "clock": "2026-10-16T00:00:00Z",
    "target": "fixture-project",
    "permissions_profile": "readonly-with-github-dry-run"
  },
  "records": [
    {
      "case_id": "scheduled_access_watch",
      "sample": 0,
      "route": "access_watch",
      "trace_complete": true,
      "model_invocations": 1,
      "presentations": [
        {"phase": "analysis", "tools": ["get_collected_evidence", "publish_report"]}
      ],
      "calls": [
        {"phase": "collect", "tool": "get_run_context", "arguments": {}, "outcome": "ok", "facts": []},
        {"phase": "collect", "tool": "investigate_access", "arguments": {"hours": 24}, "outcome": "ok", "facts": ["evidence_checked"]}
      ],
      "completion": {"status": "complete", "facts": ["evidence_checked", "report_ready"]},
      "evidence": ["private-artifact://actual-run/trace.json"]
    }
  ]
}
```

- `mode=runtime_stubbed`は、固定スナップショットとモデルの代替実装を使う経路・契約の試験です。合格しても`release_eligible=false`であり、LLMの選択精度を測定した証拠にはなりません。`live_model`は実モデルを実行した記録です。両者を混ぜた比較は拒否します。
- `revision`は実際に実行した版を記録します。旧構成`55a7e1f`の独立したチェックアウトでも、同じ入力・データ・条件を使用して実行し、同じ記録形式へ変換できます。比較プログラムのインポートのために旧版アプリを新しい版へ差し替える必要はありません。
- `conditions`はモデル名、モデル設定、データ、固定時刻、対象、権限を含み、前後で完全一致が必要です。プロンプト・ツール構成の差分は比較対象の`revision`と証拠に残します。モデルの別名が実際のモデル更新を隠す場合は、解決した版の情報も条件へ追加します。
- `presentations`は**毎回のモデル呼び出しに渡した実際のツール一覧**です。呼ばれたツールだけから逆算してはいけません。`model_invocations`と件数が一致する必要があります。モデルを使わない固定収集は提示0件です。ルーターの担当候補は`conversation`、固定収集は`collect`、担当の分析は`analysis`として記録します。
- `calls`は試行順です。`outcome=ok`は呼び出しが正常に結果を返したことを表し、データの完全性や業務完了とは別です。権限不足を返す正常な関数応答も、内容を証拠に残した上で`ok`として記録できます。未許可呼び出しは、実行前に拒否したら`blocked`、実行を開始して失敗したら`error`です。
- `facts`は、トレース・結果・レポートのアサーションを確認したアダプターまたはレビュー者が付けます。単にモデルが「完了」と書いたことから`report_ready`等を付けないでください。特に部分取得・権限不足は、それを最終報告で説明しているかを確認します。CLI自体は文章の事実性を自動判定しません。
- `trace_complete=true`はすべてのモデル提示・呼び出し・最終結果を取得できた場合のみ記録します。欠損記録は不合格です。`evidence`に実トレースとアサーション結果の保存場所を記録し、認証情報や本番の生ログは含めないでください。

Record actual model tool presentations, every attempted call, final outcome and assertion evidence. A complete trace is mandatory. Facts must be verified by the recorder or reviewer; this CLI is a deterministic contract evaluator, not a truth judge for generated prose. Do not synthesize measured traces from expected answers.

## 固定データで記録する / Recording against frozen tools

`scripts/record_routing.py`は、実際のADK Runnerを対象チェックアウトで起動し、各ケースを独立した子プロセスで記録します。`frozen-tools-v1.json`は本番データを含まない固定API応答です。運用ツールを元の引数定義を保持した代替関数へ置換してからエージェントを再構築し、対象を`fixture-project`、GitHubリポジトリを`example/fixture`、`dry_run=true`に固定します。

```bash
# 先に、git archive 55a7e1f を別の読取専用ディレクトリへ展開します。
uv run python scripts/record_routing.py \
  --checkout /absolute/path/to/baseline-archive \
  --revision 55a7e1fd93344d4f8ddfb007dc9af6036b869d3f \
  --mode runtime_stubbed --samples 3 --parallel 2 \
  --output artifacts/routing/baseline-offline.json

# 候補も同じPython環境、ケース、データ、モデル設定で記録します。
uv run python scripts/record_routing.py \
  --checkout /absolute/path/to/candidate \
  --revision candidate-commit-or-source-manifest \
  --mode runtime_stubbed --samples 3 --parallel 2 \
  --output artifacts/routing/candidate-offline.json
```

実モデルを測る場合は`--mode live_model`を指定します。`runtime_stubbed`はスクリプト化したモデル代替応答で実際の経路・記録契約を検証する方式です。両者の結果は別ファイルへ保存します。`--case CASE_ID --samples 1`で限定した接続確認も可能ですが、全ケース最低3回を満たさないため比較ゲートは通りません。

同時実行は最大2件、ケースごとの時間上限は`--timeout`で設定します。タイムアウト、モデルの権限不足、欠落トレースはそのまま失敗として残します。実行中の`.progress.json`と、終了後の全イベント・ツール宣言・固定収集結果を個別トレースへ保存します。トレースと出力はローカルの非公開ファイルです。

ネットワークはモデルのGoogle APIと認証更新だけを許可し、Logging・Compute・GitHub等は拒否します。既存ADCが別プロジェクトへのサービスアカウント代理認証だった場合でも、IAMや保存済みADCを変更する必要はありません。既存の有効なCLIユーザー権限で実行するなら`--cli-credentials`を付け、取得したトークンを子プロセスのメモリ内だけで使用できます。モデル実行権限がなければ評価は失敗し、権限の自動追加はしません。

Use the same Python/SDK environment for both checkouts. The recorder replaces operational tools with signature-preserving frozen responses, fixes a fictional target/repository and dry-run, and permits only model/authentication network traffic. `--cli-credentials` uses the existing active CLI token in process memory without changing saved ADC or IAM. Never treat a scripted-model run or a partial probe as measured LLM accuracy. Missing traces and timeouts remain failures.

## 待機箇所の診断 / Diagnosing waits

個別トレースの`details.transport_events`と`.progress.json`の`transport_events`には、HTTP送信wrapperの`send_start`、`response_returned`、`exception`、`cancelled`を記録します。記録内容はリクエスト番号、transport名（`httpx.sync`、`httpx.async`、`requests`、`aiohttp`）、相対monotonic時刻、経過秒数、返却されたHTTP status、例外クラスだけです。ヘッダー、本文、query、例外本文、トークンは記録しません。従来の`network_requests`も許可ホストとqueryを除いたpathだけを記録します。

`send_start`はPythonのwrapperへ到達した証拠であり、DNS解決・TCP/TLS接続・サーバー到達の証明ではありません。`response_returned`は元の送信関数が戻ったことだけを示します。特に`aiohttp`では本文の読み取り前に戻る場合があり、モデル応答の解析完了や業務完了を意味しません。`model_events`は担当ごとの`model_start`と`model_response` callbackを同じtask内で対応付けます。この経過時間には初期化・認証・SDKの再試行等も含まれ、APIサーバーの処理時間そのものではありません。対応付けできないcallbackは`unmatched_callback=true`として残し、時間を推測しません。`runtime_stubbed`ではbefore callbackから代替応答を返すため、after callbackを通らず、`model_response`が0件の場合があります。実際に通らなかったcallbackは補いません。

時刻の原点は子プロセス内のegress計測初期化時です。固定fixture時刻や外部の壁時計とは別です。タイムアウト・エラー時には、`details.pending_tasks`へ最大16 task・各32段（絶対上限32 task・64段）のawait chainを保存します。期限と同じ時間の診断callbackを`asyncio.timeout`より先に登録し、ADKのキャンセル後処理でモデルtaskが消える前の位置を採取します。別taskや周期pollは作らず、callback handleは必ず解除します。通常例外で期限前の採取がない場合は、エラー捕捉時のsnapshotへ切り替え、`capture`で区別します。記録するのはmodule、ファイルbasename、関数、行番号だけで、局所変数・引数・task名・coroutineのreprは含めません。`error.traceback_frames`も同じ形式です。C実装のawaitableやasync generator境界では、それ以上の待機先を得られない場合があり、`opaque_awaitable_boundary`へ残します。通常成功時にはtask stackを保存しません。`asyncio.run`終了時のキャンセルは最終JSON保存後に起きる場合があるため、最後のprogressにだけ後続のcancelイベントがあることがあります。エラー時のtask snapshotと両方を確認してください。

Per-sample `details.transport_events` and progress `transport_events` record send-wrapper start, return, exception and cancellation. Only the request number, transport name, relative monotonic time, elapsed time, returned HTTP status and exception class are recorded. Headers, bodies, queries, exception text and tokens are excluded. `send_start` does not establish DNS/TCP/TLS completion or server receipt. `response_returned` means only that the wrapped function returned; aiohttp may return before the response body is read. It does not prove a parsed model response or task completion.

`model_events` pair per-agent start/response callbacks within the same task; an unmatched callback is explicit and receives no inferred duration. Model duration can include setup, authentication and SDK retries. A `runtime_stubbed` response returned by the before callback bypasses the after callback, so it can have zero `model_response` events; missing callbacks are never synthesized. The clock starts when egress instrumentation is initialized in the sample process, independently of fixture time and wall clocks. A same-deadline callback registered before `asyncio.timeout` captures task locations before ADK cancellation cleanup removes them; it adds no task or polling, and its handle is always cancelled. On failure only, `pending_tasks` retains this bounded snapshot or an explicitly labelled after-error fallback. It and `error.traceback_frames` contain module, file basename, function and line, never locals, arguments, task names or coroutine representations. Opaque C awaitables and async-generator boundaries may limit visibility and are flagged. Successful runs discard task snapshots. A final progress checkpoint may contain a later cancellation from `asyncio.run` cleanup that occurred after the result JSON was written; inspect both alongside the in-flight task snapshot. No model, timeout, retry or comparison-gate behavior is changed by this instrumentation.

## 判定 / Gates

1. 担当、必須ツール、引数、必須前提、最終業務完了を別々に集計します。順序全体の完全一致ではなく、必須呼び出しと特定操作の前提条件を検証します。独立した収集の順序や許可された追加調査は変えられます。
2. 引数は指定した必須フィールドを型も含めて照合します。同じツールを誤った引数で試した後、正しい引数で実行しても、その誤りは消しません。
3. 未許可ツールの**提示、試行、実行**は別件数です。候補で1件でもあれば不合格です。他の成功や平均値で相殺しません。
4. 全ケースで前後の`case_id`と`sample`が一致し、ケースごとに最低3回の独立実行が必要です。欠落ケース、重複記録、標本不足、条件不一致、別スイートの記録を拒否します。`--min-samples`でスイートの最低数を下げることはできません。
5. 候補の各記録は全5指標を満たし、指標の平均も旧構成から低下していないことが必要です。旧構成に見つかった欠陥は旧構成の失敗としてそのまま残ります。
6. `live_model`の比較が合格した場合に限り`release_eligible=true`です。これはこの比較ゲートの判定であり、全業務・本番の品質保証や、統計的な精度証明ではありません。最低3回は反復スモーク試験の下限です。正式運用の判定では、実業務を代表する別の未使用ケース、障害ケース、十分な標本数、内容レビューが必要です。

Route, required tools, arguments, preconditions and task completion are independent metrics. Unauthorized presentation/attempt/execution counts must all be zero. Missing samples fail closed. Only live-model comparisons can satisfy this gate for release; fixture checks and three repetitions do not establish statistical accuracy or production readiness.

## ケース追加 / Extending cases

`allowed_tools`は工程ごとの許可リストです。未登録の工程は空の許可リストとして判定します。`required_calls`はツール名と必須引数、`preconditions`は操作前の成功済みツール・取得済み事実、`completion_statuses`と`completion_facts`は最終成果の条件です。`forbidden_facts`で部分取得時の「攻撃なしを証明した」等の誤った結論を禁止できます。

機能の追加時は、その機能のケースと、似た既存業務へ誤って振り分けるケースを追加し、許可リストを全担当へ広げずに再評価してください。スイート変更後は指紋が変わるため、同じ新スイートで旧構成と候補を再実行します。既存のADK評価は、自由文の品質・根拠・日本語報告など、この契約評価だけでは測れない部分に引き続き使用します。
