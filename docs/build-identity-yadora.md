# Yadora の既存サービスのビルド ID 移行

English: [docs/en/build-identity-yadora.md](en/build-identity-yadora.md)

Refs [atsuki-sakai/Yadora#1652](https://github.com/atsuki-sakai/Yadora/issues/1652)。移行用の手順・契約であり、IAM 適用・本番ビルド・Editor 削除の実施記録ではありません。

## 固定する対象

`OPS_BUILD_PROFILE=yadora-prod-gcp-ops` を明示したときだけ、次の組合せを使います。値の正は `scripts/gcp/build_profile.sh`。設定一覧は [configuration.md](configuration.md#デプロイの設定make-gcp-deploy) を参照してください。

| 対象 | 値 |
|---|---|
| 配置 project / region | `yadora-prod` / `asia-northeast1` |
| 既存 Cloud Run service | `gcp-ops-team` |
| 維持する runtime SA | `gcp-ops-team@yadora-prod.iam.gserviceaccount.com` |
| 専用 build SA | `gcp-ops-build@yadora-prod.iam.gserviceaccount.com` |
| 既存 source bucket | `run-sources-yadora-prod-asia-northeast1` |
| 既存 Docker repository | `cloud-run-source-deploy` |

profile と競合する project / service / region / identity / bucket / repository の入力は拒否します。既存 service の runtime SA と source / destination resource が一致しない場合も停止します。他 project / service の既存の一般デプロイは変更しません。`yadora-prod` の `gcp-ops-team` を profile なしで指定した場合は停止し、既定 Compute SA へ切り替えません。

## 変更前の確認

既存 Desktop checkout の feature branch を配備に使わず、レビュー済みの clean checkout を用意します。稼働 source の PR #23 merge `2a16824210e50c02fc48a24c4544f8575d854e5f` と、移行の base main `3ca4e6d57c8f46486e6ca351f1d98faf3a4ef754` の差分は、変更履歴・比較評価の記録 script / docs / unit test の5ファイルです。app・依存・Dockerfile・配置 script に差分はありませんでした。実行直前にも最新 main と配備対象を照合し、未配備のアプリ変更を混ぜないでください。

Cloud Run の image / 100% traffic revision / runtime SA / 環境変数・secret 参照 / scaling / timeout / IAM と、IAM 変更対象の policy を非公開領域へ保存します。個人情報・token・Cookie・秘密値は PR / Issue へ掲載しません。`artifacts/` は Git と source upload の対象外です。

## Build 専用 bootstrap

通常の `make gcp-bootstrap` は runtime・secret・監視対象の権限も設定するため、この移行では使いません。次は既存 source bucket / repository / service の読取確認と、専用 SA / 限定 binding だけを扱います。API や bucket / repository / runtime を新規作成しません。

```bash
OPS_BUILD_PROFILE=yadora-prod-gcp-ops make gcp-build-bootstrap
# 読取確認と表示内容をレビューした後だけ、別の実行で適用する
OPS_BUILD_PROFILE=yadora-prod-gcp-ops make gcp-build-bootstrap ARGS=--apply
```

既定は plan のみで IAM 変更はありません。初回の変更は専用 SA の作成と次の3 binding、既存 SA なら3 binding のみです。

| Grant level | Role |
|---|---|
| project `yadora-prod` | `roles/logging.logWriter` |
| repository `cloud-run-source-deploy` | `roles/artifactregistry.writer` |
| source bucket `run-sources-yadora-prod-asia-northeast1` | `roles/storage.objectViewer` |

Owner 等の承認済み operator で適用します。非 Owner への実行委譲が必要なら、専用 SA 上の `iam.serviceAccounts.actAs` を別途限定して設計します。実行者への project-wide な権限付与をこの script は行いません。権限拒否を SA 不在と読み替えず、既存 SA が無効・不一致・想定外の project role を持つ場合は停止します。Editor・runtime・Secret・KMS・DB 権限は追加しません。

適用後は SA の全 grant level を確認し、Editor がないことと上表の限定 binding を記録します。source deploy の[公式 build ID 設定](https://docs.cloud.google.com/run/docs/configuring/services/build-service-account)は `--build-service-account` を説明しています。この3 role の十分性は、Editor のない **当該 SA だけ**で実 build / push が成功して初めて確認できます。失敗したからといって `roles/run.builder` や Editor を無条件に追加しません。

## 既存 runtime 設定を保持する source deploy

```bash
OPS_BUILD_PROFILE=yadora-prod-gcp-ops make gcp-deploy
```

この profile は既存 service に `--source .` と専用 `--build-service-account` を渡します。一般デプロイの `--env-vars-file`、runtime SA、memory / CPU / scaling / timeout、secret、labels、認証・IAM を再設定する flag は渡しません。`.env` の `OPS_DRY_RUN`、model、監視対象などを本番へ再適用しないため、現在の runtime 設定を維持できます。profile 自体は runtime の環境変数へ転送しません。

この固定 profile は `--no-traffic` で source build と revision 作成を行い、保存した唯一の100%旧 route を検査してから、今回の revision **名**に100%を割り当てます。既存の明示 pin は通常の source deploy だけでは移動しません。source deploy の exit 0 や、旧 revision の HTTP 200 を新 image の配備成功と扱わないでください。

切替前に最新 Created revision・immutable digest・Ready・service UID・runtime 設定・IAM、実 Build の成功・専用 SA・source bucket・push digest を実取得します。generation を照合した [v2 Service の etag](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.services#Service) を付け、[`updateMask=traffic` の PATCH](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.services/patch) だけを1回行います。LATEST への追従・runtime・IAM は更新しません。競合・取得失敗・不一致・operation 不明時は書込みを再試行せず停止します。既に同じ revision 名へ100%切替済みなら、検証だけで終了します。

Cloud Build の実 `serviceAccount`、source bucket、Docker repository、成功 status、push した digest、ログ出力と新 SA の権限を照合します。Cloud Run の Ready / traffic / digest、認証付きの `/list-apps` 等の読取経路も確認します。HTTP 成功だけを監査ジョブの完了・GitHub 書込みの成功とは扱いません。無断で監査ジョブを起動して外部へ投稿しないでください。

## 既存候補を再 build せず切り替える

`make gcp-promote-existing` は resume 専用です。build / source deploy / IAM 操作を呼びません。source deploy 前に保存した service / service IAM の非公開 JSON と、旧・新 revision / digest・実 Build ID・**その image を build した source SHA** を明示します。既定は読取検証と plan のみで、`--apply` のときだけ上記の限定 PATCH を行います。

```bash
PROMOTION_ARGS="--before-service artifacts/before-service.json --before-iam artifacts/before-service-iam.json --previous-revision gcp-ops-team-pr23-2a16824 --previous-digest sha256:4011722df35db78d918e62155c33fa7477d30f280730a3469dd4db90dab4bf60 --revision gcp-ops-team-00009-rlx --digest sha256:ee4cd770ea067f4e8ff671fece75dec6d11f0d5fa397edc06aba48fb023a93a3 --build-id dc66ced2-8926-47de-95d2-a7796a4fbb83 --image-source-sha e1788e0c6f5f1995e8710a7c7e25e80936732cb8"
OPS_BUILD_PROFILE=yadora-prod-gcp-ops make gcp-promote-existing ARGS="$PROMOTION_ARGS"
# plan と証拠をレビューし、実反映が許可された後だけ:
OPS_BUILD_PROFILE=yadora-prod-gcp-ops make gcp-promote-existing ARGS="$PROMOTION_ARGS --apply"
```

この例は2026-11-03に専用 SA で実 build/push 済みの候補です。修正 helper の commit SHA に置き換えて build 済みと記録しません。記録した SHA の `git archive` を private tempdir に展開し、その時点の ignore 規則から全 upload 対象を生成して、Build が参照する generation 固定の実 GCS source zip と照合します。現在の checkout の削除・ignore 改変で期待集合を縮めません。dirty checkout、別 source、内容・追加/欠落ファイルは停止条件です。SHA は operator が記録した source であり、Cloud Build の Git provenance フィールドを捏造しません。

複数 route・tag・別 service/revision・Ready 不成立・runtime/IAM 差分・外部同時変更は手動で迂回せず停止します。operation の結果が不明なら再 build / PATCH をせず実設定を確認し、同じ expected 値での resume 検証を使います。完了後は新 revision の100% / digest と、認証付きの読取 health を別途確認します。

## 復旧と完了条件

build 失敗時は理由を調べ、既定 Compute SA への fallback を追加しません。配備後の問題は保存した旧 revision への traffic 復旧を別途レビューして行い、既存 image / tag / policy 全体を削除・上書きしません。

この repository の移行成功だけで Yadora #1652 をクローズしません。Yadora 本体の build、過去の Rust 検証、Trivy DB cache 出力など、保持する経路の移行・廃止判断と実検証を揃えた後に既定 Compute SA の Editor を削除し、必要処理を再確認します。Editor を持つ旧 SA の成功は代替権限の証拠にしません。

ローカル CLI fixture は scope / 拒否 / runtime flag 非送信を検査します。本番権限の十分性や API 伝播の証拠には使いません。scan findings の脆弱性修正も、この build ID の変更とは別に追跡します。
