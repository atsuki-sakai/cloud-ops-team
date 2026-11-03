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

これは実 source build と新 image の配置を行い、通常の Cloud Run deploy として traffic を新 revision へ進める操作です。plan や build-only 検査ではありません。必要なレビュー・検査後に実行し、完了後は snapshot と比較して runtime 設定・IAM が保持されたことを確認します。builder の名前が正しいことだけを成功の証拠にはしません。

Cloud Build の実 `serviceAccount`、source bucket、Docker repository、成功 status、push した digest、ログ出力と新 SA の権限を照合します。Cloud Run の Ready / traffic / digest、認証付きの `/list-apps` 等の読取経路も確認します。HTTP 成功だけを監査ジョブの完了・GitHub 書込みの成功とは扱いません。無断で監査ジョブを起動して外部へ投稿しないでください。

## 復旧と完了条件

build 失敗時は理由を調べ、既定 Compute SA への fallback を追加しません。配備後の問題は保存した旧 revision への traffic 復旧を別途レビューして行い、既存 image / tag / policy 全体を削除・上書きしません。

この repository の移行成功だけで Yadora #1652 をクローズしません。Yadora 本体の build、過去の Rust 検証、Trivy DB cache 出力など、保持する経路の移行・廃止判断と実検証を揃えた後に既定 Compute SA の Editor を削除し、必要処理を再確認します。Editor を持つ旧 SA の成功は代替権限の証拠にしません。

ローカル CLI fixture は scope / 拒否 / runtime flag 非送信を検査します。本番権限の十分性や API 伝播の証拠には使いません。scan findings の脆弱性修正も、この build ID の変更とは別に追跡します。
