# Yadora 向け Build SA の本番移行記録

English: [production-yadora-build-identity-2026-11-03.md](en/production-yadora-build-identity-2026-11-03.md)

2026-11-03 08:24:44 UTC に `yadora-prod` / `asia-northeast1` の既存 `gcp-ops-team` で、Editor を持たない専用 Build SA による build / push と新 revision への100%切替、認証付き読取経路を確認しました。実行手順は [移行 runbook](build-identity-yadora.md)、関連は [Yadora #1652](https://github.com/atsuki-sakai/Yadora/issues/1652) です。既定 Compute SA の Editor は保持し、同 Issue はこの移行だけではクローズしません。

## 本番の image と source

| 項目 | 実取得した値 |
|---|---|
| image を build した main SHA | `2c97825a5feb37050a0d488d13931afe60193966` ([PR #29](https://github.com/atsuki-sakai/cloud-ops-team/pull/29)) |
| Cloud Build ID | `b2f8f004-2bee-4317-b2b3-7083f64547c9` |
| Build status | `SUCCESS`（08:21:25–08:22:17 UTC） |
| Build SA | `gcp-ops-build@yadora-prod.iam.gserviceaccount.com` |
| source bucket | `run-sources-yadora-prod-asia-northeast1` |
| image | `asia-northeast1-docker.pkg.dev/yadora-prod/cloud-run-source-deploy/gcp-ops-team@sha256:874a23f5de3f219c5ffe1d76d450869c087fb99a9a127067aca22da09bb079e9` |
| 配信 revision | `gcp-ops-team-00010-hqr`、Ready、唯一の明示 revision 名へ100% |
| 直前の100% revision | `gcp-ops-team-pr23-2a16824` |
| 直前の image digest | `sha256:4011722df35db78d918e62155c33fa7477d30f280730a3469dd4db90dab4bf60` |

この記録を追加する後続 commit は image の build source ではありません。保持した既存 `source-commit` runtime label も source 証明には使わず、上記 SHA と実 Build が参照する generation 固定 GCS zip を照合しました。zip は129ファイル・1,727,613 bytesの期待 upload 全体と一致し、追加・欠落・内容差分は0でした。archive SHA-256 は `34f1d5f297fe7ee44779eddb38f4bab7bf8244ca10a1ce9581d6275d7b9c557d`。秘密ファイル・非公開成果物・Ruff cache は含まれていませんでした。

## 権限と設定の検証

[PR #27](https://github.com/atsuki-sakai/cloud-ops-team/pull/27) の固定 profile で06:09:44 UTCに専用 SA と次の3 binding だけを bootstrap しました。API・bucket・repository・runtime・Secret の bootstrap は実行していません。

| 範囲 | 専用 Build SA の role |
|---|---|
| project `yadora-prod` | `roles/logging.logWriter` のみ |
| repository `cloud-run-source-deploy` | `roles/artifactregistry.writer` のみ |
| source bucket | `roles/storage.objectViewer` のみ |

SA は enabled、専用 SA 自身の IAM binding は空で、Editor はありませんでした。同 SA で source の読取・image push が成功し、Build の `CLOUD_LOGGING_ONLY` 設定と実ログ10件も確認しました。旧 Compute SA の Editor を持つ成功と混同していません。初回 bucket IAM の更新では metageneration / update time だけが変わり、保持設定・暗号化・公開防止等の実設定は不変でした。

最終配備は clean な上記 main から `OPS_BUILD_PROFILE=yadora-prod-gcp-ops make gcp-deploy` を1回実行しました。`--no-traffic` の候補作成後、旧・新 runtime、Build / source / SA / digest / Ready、service UID・generation・etag を検査し、traffic-only PATCH 1回で新 revision 名へ100%を固定しました。v1 / v2 の実取得で切替と reconciliation 完了を確認しています。LATEST 追従には変更していません。

配備前後の非公開スナップショットを比較し、runtime SA・model・環境変数・secret参照・scaling・timeout・labels・ingress・認証設定、project / repository / bucket / service IAM、source / repository の設定が不変でした。Yadora の他4サービスの全 snapshot も不変でした。raw policy・環境値・個人情報・token は公開記録へ含めていません。

## 実経路と検査結果

- 新 revision が唯一の100%配信先であることを確認後、認証付き `GET /list-apps` は HTTP 200 / JSON 配列 `["app"]` でした。
- `GET /a2a/app/.well-known/agent-card.json` は HTTP 200、`name=cloud_ops_team`、非空の `supportedInterfaces`（2件）を確認しました。
- job / agent の起動、GitHub 投稿は行っていません。これらの HTTP 成功は監査 job の完走・投稿の成功・脆弱性修正の証明ではありません。
- 最終変更の Python 3.13 / 3.11 全 unit は逐次実行で各622成功。Ruff / format / codespell / ty と独立レビューは成功し、具体的な P1 / P2 はありませんでした。実 integration 6成功は不変の app tree に対する既存検証です。
- 高負荷時の先行 matrix は各620成功＋routing 子process timeout1件として保持しました。最終 hash では該当単体成功の後に全 matrix を逐次完走し、timeout を成功に読み替えていません。GitHub Actions は最新 run でも job 開始前の platform account 要因で停止し、steps は0でした。自動 CI の成功とは扱いません。

## 保持した候補と残作業

先行 Build `dc66ced2-8926-47de-95d2-a7796a4fbb83` は source `e1788e0c6f5f1995e8710a7c7e25e80936732cb8` から `ee4cd770…` を専用 SA で build / push しましたが、既存の明示 traffic pin が移動せず、候補 `gcp-ops-team-00009-rlx` は未昇格でした。その source zip の余剰 Ruff cache16件を例外受容せず、候補と証拠を保持しました。[PR #28](https://github.com/atsuki-sakai/cloud-ops-team/pull/28) の限定 traffic guard に加え、PR #29 で supported source staging と子プロセスの ignore 有効固定を導入しています。

main `e1680afd3fe868b0e48fb392a2435ebb7317adad` からの途中の試行は、SDK568.0.0が `run deploy --ignore-file` を受理せず build 前の引数解析で停止しました。停止後に service 全 snapshot 不変・新 Build 0件を実取得し、不明な write を再試行した扱いにはしていません。

旧 image / revision / tag は削除していません。問題時の旧 revision への traffic 復旧は、現在の状態と etag を確認して別途レビューします。Editor 削除は明示的に保留しています。他の必要経路の移行・継続 / 廃止判断と検証、既存脆弱性の修正は別作業です。
