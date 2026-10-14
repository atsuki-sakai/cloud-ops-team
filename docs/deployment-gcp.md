# Google Cloud へのデプロイ手順

English: [docs/en/deployment-gcp.md](en/deployment-gcp.md)

チームは 1 つの Cloud Run サービスとして動きます。Cloud Scheduler が、OIDC で認証した HTTP リクエストでこのサービスを呼び出します。
既定の予定では、毎日のエラー監視、週次レビュー、月次診断で 1 か月に数十回呼び出します。
費用と実行時間はログ量、モデル、再試行、手動実行などに左右されます。既定の最小インスタンス数は 0 です。
配置前に [既知の制限](known-limitations.md) を確認してください。以下の手順だけで監査の完全性や重複起票の防止を保証するものではありません。

```mermaid
flowchart LR
    SCH["Cloud Scheduler<br/>3 つの HTTP ジョブ"] -- "OIDC 認証（呼び出し用 SA）" --> RUN["Cloud Run<br/>cloud-ops-team"]
    RUN -- "実行用 SA" --> VTX["Vertex AI（Gemini）"]
    RUN -- "閲覧専用のロール" --> TGT["監視対象のプロジェクト<br/>Logging · Monitoring · Run · SQL · IAM · SCC"]
    RUN -- "Secret Manager のトークン" --> GH["GitHub の ISSUE / PR"]
```

## 用意するもの

- サービスアカウントの作成、IAM ロールの付与、Cloud Run へのデプロイができるユーザーで `gcloud` にログインしていること
- 対象リポジトリの GitHub トークン。fine-grained token で **Issues: read and write**、**Pull requests: read**、
  **Actions: read**、**Contents: read**、**Metadata: read** の権限を付けます（**Administration: read** を追加すると、診断でブランチ保護も確認できます）
- 監視対象のプロジェクトは、デプロイ先と同じプロジェクトでも別のプロジェクトでも構いません

以下の手順にはすべて、`.env` を読み込む `make` コマンドを用意しています（`.env` は `make setup` で作り、`make doctor` で点検します）。
直接呼び出したい場合は、`scripts/gcp/` にある元のスクリプトに同じ値を環境変数として渡してください。

## 1. 初期設定（初回だけ）

```bash
make gcp-bootstrap          # または: PROJECT_ID=my-project TARGET_PROJECT=my-project GITHUB_TOKEN=github_pat_xxx ./scripts/gcp/bootstrap.sh
```

このコマンドは次のことを行います。

- 必要な API を有効にします
- 実行用サービスアカウント `cloud-ops-team@<project>` を作成し、`roles/aiplatform.user`、`roles/secretmanager.secretAccessor`、
  `roles/logging.logWriter`、`roles/cloudtrace.agent`、`roles/monitoring.metricWriter` を付与します
- 監視対象に対する閲覧専用のロールを付与します（`scripts/gcp/grant_target_roles.sh`）
- GitHub トークンをシークレット `cloud-ops-team-github-token` に保存します（`GITHUB_TOKEN` が未設定なら入力を求められます）

現在の bootstrap は `roles/secretmanager.secretAccessor` を**デプロイ先プロジェクト全体**に付与します。
GitHub トークン以外のシークレットにもアクセスできる範囲です。権限縮小はスクリプトに未実装なので、運用者が
トークンのシークレット単位のアクセス付与を確認したうえで、不要なプロジェクト単位の付与を見直してください。
bootstrap の再実行ではプロジェクト単位の付与が戻るため、再確認が必要です（[Secret Manager のアクセス制御](https://docs.cloud.google.com/secret-manager/docs/access-control)）。

## 2. デプロイ

まずはお試しモードで動かし、チームが登録しようとする ISSUE を確認してから、書き込みを有効にします。

```bash
make gcp-deploy                         # お試しモード（OPS_DRY_RUN は .env の値）
OPS_DRY_RUN=false make gcp-deploy       # ISSUE の登録を開始
```

スクリプトは最後に `SERVICE_URL=...` を表示し、その URL 宛ての OIDC トークンだけを受け付けるようにサービスを設定します
（`OPS_TRIGGER_AUDIENCE`、`OPS_TRIGGER_SERVICE_ACCOUNTS`）。動作確認は次のとおりです。
直接の `gcloud` コマンドは `.env` を読みません。次の値を `.env` の配置先 `PROJECT_ID` / `GOOGLE_CLOUD_PROJECT`、
`SERVICE`、`REGION` に合わせてください。監視対象の `OPS_TARGET` と配置先は異なる場合があります。

```bash
DEPLOY_PROJECT=my-project
SERVICE_NAME=cloud-ops-team
DEPLOY_REGION=asia-northeast1
URL=$(gcloud run services describe "$SERVICE_NAME" --project "$DEPLOY_PROJECT" --region "$DEPLOY_REGION" --format='value(status.url)')
curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/list-apps"     # ["app"] と表示されれば OK
```

この確認はサービスへの接続とアプリ一覧の取得までです。監視対象の API、GitHub へのアクセス、ジョブの完了は別途確認してください。

### 配置後の補完設定

標準スクリプトは許可した設定を `--env-vars-file` で渡し、カンマ・縦棒・空値を含むラベル設定も保持します。
ただし、`APP_URL` と `OPS_CONTEXT_FILE` は現在許可リストに含まれません。

A2A を使う場合は、上で取得したサービス URL を設定し、カードがその URL の `/a2a/app` を広告することを確認してください。
未設定のカードは `http://0.0.0.0:8000/a2a/app` を広告するため、外部クライアントの接続先として使えません。

```bash
gcloud run services update "$SERVICE_NAME" --project "$DEPLOY_PROJECT" --region "$DEPLOY_REGION" --update-env-vars "APP_URL=$URL"
```

プロジェクトの事情は既定の `config/ops-context.md` に置くのが簡単です。独自パスを使う場合は、そのファイルが
コンテナ内に含まれていることを確認し、配置後に設定します。次の例は `config/custom-context.md` をイメージへ含めた場合です。

```bash
gcloud run services update "$SERVICE_NAME" --project "$DEPLOY_PROJECT" --region "$DEPLOY_REGION" --update-env-vars OPS_CONTEXT_FILE=config/custom-context.md
```

`make gcp-deploy` は環境変数を置き換えるので、これらの補完設定は再デプロイのたびにやり直し、実行結果でも読み込みを確認してください。

## 3. 定期実行の登録

```bash
make jobs                    # 登録される予定を確認
make gcp-schedule            # ジョブを作成または更新（サービスの URL は自動で見つけます）
gcloud scheduler jobs run cloud-ops-error-watch --location asia-northeast1   # 今すぐ 1 回実行
```

既定の予定は、エラー監視が毎日 8:00、週次レビューが毎週月曜 9:00、月次診断が毎月 1 日 9:00 です（いずれも Asia/Tokyo）。
どれも環境変数で変えられます。詳しくは [設定一覧](configuration.md#定期実行の設定make-gcp-schedule) を見てください。

## 運用手順

| やりたいこと | コマンド |
|---|---|
| 設定とアクセス権を点検する | `make doctor` |
| 最近の実行を確認する | `make gcp-logs`、または `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="cloud-ops-team" AND httpRequest.requestUrl:"trigger/pubsub"' --limit 10 --format="value(timestamp,httpRequest.status,httpRequest.latency)"` |
| チームが作った ISSUE を見る | `gh issue list -R owner/repo --label ops-agent --state all` |
| GitHub への書き込みを止める・再開する | `gcloud run services update cloud-ops-team --region asia-northeast1 --update-env-vars OPS_DRY_RUN=true`（再開は `false`） |
| ジョブを一時停止する | `gcloud scheduler jobs pause cloud-ops-error-watch --location asia-northeast1`（再開は `resume`） |
| 予定を変える | `ERROR_WATCH_CRON=... ONLY_JOBS=error-watch make gcp-schedule` |
| コードや設定の変更を反映する | `make test && make gcp-deploy` |
| GitHub トークンを更新する | `printf '%s' "$TOKEN" \| gcloud secrets versions add cloud-ops-team-github-token --data-file=-`（新しく起動したインスタンスから反映されます） |
| ロールバックする | `gcloud run services update-traffic cloud-ops-team --region asia-northeast1 --to-revisions=<以前のリビジョン>=100` |
| サービス自体のエラーを見る | `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="cloud-ops-team" AND severity>=ERROR' --limit 20` |

Scheduler や HTTP の成功だけでは、監査が完了したとは判断できません。既定のセッション保存はメモリ内で、
ISSUE が作られなかった調査不能の説明はインスタンス終了後に残らない場合があります。
ログ・ツールのエラー・GitHub の結果を合わせて確認し、未確認項目と最終結果を運用側でも記録してください。
監査 findings が空でも、[プロバイダーの補完確認](providers.md) が必要です。

同じ対象・リポジトリへの手動実行と定期実行が重ならないようにしてください。必要に応じてインスタンス数と同時リクエスト数を
絞ることは競合を減らす方法ですが、複数サービスや再試行を含む重複防止の保証にはなりません。

## 複数のプロジェクトを監視する

監視対象ごとにサービスを 1 つずつデプロイし（`SERVICE=cloud-ops-staging TARGET_PROJECT=...`）、定期実行ジョブにもそれぞれ別の接頭辞を付けます
（`JOB_PREFIX=cloud-ops-staging`）。同じリポジトリを共有しても構いません。重複の検索・更新は、各 ISSUE に記録されたプロバイダーと監視対象で絞り込みます。
同じ対象を複数のサービスで同時に処理すると、検索と作成の間に競合し、重複起票される可能性は残ります。

## Terraform や CI/CD を使う場合

このスクリプトはあえて小さく作っています。インフラをコードで管理したい場合は、`agents-cli scaffold enhance . --deployment-target cloud_run`
でサービスとサービスアカウントの Terraform を生成でき、`--cicd-runner github_actions` を付けるとデプロイ用のパイプラインも追加されます。
定期実行ジョブと IAM の設定は、スクリプトの内容を移植してください。
