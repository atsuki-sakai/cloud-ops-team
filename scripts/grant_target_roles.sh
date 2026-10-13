#!/usr/bin/env bash
# 監視・診断対象プロジェクトに対して、エージェントのランタイム SA へ読み取り専用ロールを付与する。
#
# 使い方:
#   TARGET_PROJECT=<target-project> \
#   AGENT_SA=gcp-ops-team@<deploy-project>.iam.gserviceaccount.com \
#   ./scripts/grant_target_roles.sh
#
# 付与されるのはすべて viewer / reviewer 系。エージェントは GCP を変更しない設計なので書き込み系は不要。
set -euo pipefail
: "${TARGET_PROJECT:?TARGET_PROJECT を指定してください}"
: "${AGENT_SA:?AGENT_SA（ランタイムのサービスアカウント）を指定してください}"

ROLES=(
  roles/serviceusage.serviceUsageConsumer   # x-goog-user-project で API を呼ぶため
  roles/browser
  roles/logging.viewer                      # エラーログ
  roles/monitoring.viewer                   # 5xx 率・アラート設定
  roles/run.viewer                          # Cloud Run サービス / リビジョン / IAM
  roles/cloudscheduler.viewer               # ジョブの失敗状況
  roles/cloudsql.viewer                     # Cloud SQL 設定・メンテナンス
  roles/compute.networkViewer               # ファイアウォール
  roles/iam.securityReviewer                # IAM ポリシー・SA 鍵の棚卸し
  roles/securitycenter.findingsViewer       # SCC の検出結果（SCC 有効時）
  roles/containeranalysis.occurrences.viewer # イメージ脆弱性（スキャン有効時）
  roles/artifactregistry.reader
)
for role in "${ROLES[@]}"; do
  echo "▶ $role"
  gcloud projects add-iam-policy-binding "$TARGET_PROJECT" --member "serviceAccount:${AGENT_SA}" --role "$role" --condition=None >/dev/null
done
echo "✅ 完了"
