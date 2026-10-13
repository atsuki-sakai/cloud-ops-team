#!/usr/bin/env bash
# Cloud Scheduler（HTTP ターゲット）→ エージェントの /apps/app/trigger/pubsub を直接呼び出す定期実行を配線する。
#
# Pub/Sub push ではなく Scheduler の HTTP ターゲットを使う理由:
#   - Pub/Sub push の処理上限は 10 分だが、月次診断は数分〜十数分かかることがある。HTTP ターゲットは
#     --attempt-deadline を最大 30 分まで伸ばせる。
#   - 再試行を 0 にできるので、長時間ジョブが二重起動しない（Pub/Sub は未 ack で再配送される）。
#   ボディは Pub/Sub push と同じ形式なので、ADK 側のトリガー実装はそのまま使える。
#
# 使い方:
#   PROJECT_ID=<エージェントをデプロイしたプロジェクト> \
#   AGENT_URL=https://<cloud-run-url> \
#   CLOUD_RUN_SERVICE=gcp-ops-team \
#   ./scripts/setup_schedules.sh
#
# 既定スケジュール（Asia/Tokyo）:
#   エラー監視            : 毎時 0 分        → {"job":"error_watch","hours":2}
#   週次 ISSUE/PR レビュー : 月曜 09:00       → {"job":"weekly_pr_review","days":7}
#   月次セキュリティ診断   : 毎月 1 日 09:00  → {"job":"monthly_security_audit","kev_days":30}
# ERROR_WATCH_CRON / WEEKLY_CRON / MONTHLY_CRON / ERROR_WATCH_HOURS で上書きできる。
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID を指定してください}"
: "${AGENT_URL:?AGENT_URL（例: https://gcp-ops-team-xxxx-an.a.run.app）を指定してください}"
REGION="${REGION:-asia-northeast1}"
INVOKER_SA="${INVOKER_SA:-gcp-ops-invoker}"
APP_NAME="${APP_NAME:-app}"
TZ_NAME="${TZ_NAME:-Asia/Tokyo}"
ERROR_WATCH_CRON="${ERROR_WATCH_CRON:-0 * * * *}"
ERROR_WATCH_HOURS="${ERROR_WATCH_HOURS:-2}"
WEEKLY_CRON="${WEEKLY_CRON:-0 9 * * 1}"
MONTHLY_CRON="${MONTHLY_CRON:-0 9 1 * *}"
ATTEMPT_DEADLINE="${ATTEMPT_DEADLINE:-1800s}"   # Scheduler HTTP ターゲットの最大値

SA_EMAIL="${INVOKER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
ENDPOINT="${AGENT_URL%/}/apps/${APP_NAME}/trigger/pubsub"

echo "▶ APIs"
gcloud services enable cloudscheduler.googleapis.com --project "$PROJECT_ID"

echo "▶ Invoker service account: $SA_EMAIL"
gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$INVOKER_SA" --display-name "gcp-ops-team scheduler invoker" --project "$PROJECT_ID"

if [[ -n "${CLOUD_RUN_SERVICE:-}" ]]; then
  echo "▶ Allow $SA_EMAIL to invoke Cloud Run service $CLOUD_RUN_SERVICE and raise its request timeout"
  gcloud run services add-iam-policy-binding "$CLOUD_RUN_SERVICE" --region "$REGION" \
    --member "serviceAccount:$SA_EMAIL" --role roles/run.invoker --project "$PROJECT_ID" >/dev/null
  gcloud run services update "$CLOUD_RUN_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --timeout "${ATTEMPT_DEADLINE%s}" >/dev/null
fi

# Pub/Sub push と同じエンベロープ。data は base64、subscription はセッションの user_id になる。
body_for() {
  local name=$1 payload=$2
  local data
  data=$(printf '%s' "$payload" | base64 | tr -d '\n')
  printf '{"message":{"data":"%s","attributes":{"source":"cloud-scheduler","job":"%s"}},"subscription":"projects/%s/subscriptions/%s"}' \
    "$data" "$name" "$PROJECT_ID" "$name"
}

create_job() {
  local name=$1 cron=$2 payload=$3
  local body
  body=$(body_for "$name" "$payload")
  local verb=create
  gcloud scheduler jobs describe "$name" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 && verb=update
  gcloud scheduler jobs "$verb" http "$name" --location "$REGION" --project "$PROJECT_ID" \
    --schedule "$cron" --time-zone "$TZ_NAME" \
    --uri "$ENDPOINT" --http-method POST --headers "Content-Type=application/json" \
    --message-body "$body" \
    --oidc-service-account-email "$SA_EMAIL" --oidc-token-audience "$AGENT_URL" \
    --attempt-deadline "$ATTEMPT_DEADLINE" --max-retry-attempts 0 >/dev/null
  echo "   ✓ $name  [$cron $TZ_NAME]  $payload"
}

echo "▶ Scheduler jobs → $ENDPOINT"
create_job gcp-ops-error-watch            "$ERROR_WATCH_CRON" "{\"job\":\"error_watch\",\"hours\":${ERROR_WATCH_HOURS}}"
create_job gcp-ops-weekly-pr-review       "$WEEKLY_CRON"      '{"job":"weekly_pr_review","days":7}'
create_job gcp-ops-monthly-security-audit "$MONTHLY_CRON"     '{"job":"monthly_security_audit","kev_days":30}'

echo "✅ 完了。手動実行: gcloud scheduler jobs run gcp-ops-error-watch --location $REGION --project $PROJECT_ID"
