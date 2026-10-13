#!/usr/bin/env bash
# Wire Cloud Scheduler (HTTP target + OIDC) to the agent's trigger endpoint.
#
# Each job POSTs a Pub/Sub-push-shaped body to /apps/<app>/trigger/pubsub. An HTTP target is
# used instead of a Pub/Sub push subscription because push deliveries are capped at 10 minutes
# and are redelivered when not acked; HTTP targets allow a 30-minute deadline and zero retries,
# so a long monthly audit never runs twice.
#
# Usage:
#   PROJECT_ID=<deploy-project> AGENT_URL=https://<cloud-run-url> CLOUD_RUN_SERVICE=cloud-ops-team \
#     ./scripts/setup_schedules.sh
#
# Everything is configurable with environment variables (defaults in brackets):
#   ERROR_WATCH_CRON   ["0 8 * * *"]   error watch schedule (daily 08:00). e.g. "0 * * * *" for hourly
#   ERROR_WATCH_HOURS  [24]            look-back window of each error watch run; match it to the interval
#   WEEKLY_CRON        ["0 9 * * 1"]   weekly issue / PR review (Monday 09:00)
#   WEEKLY_DAYS        [7]
#   MONTHLY_CRON       ["0 9 1 * *"]   monthly security audit (1st day 09:00)
#   MONTHLY_KEV_DAYS   [30]
#   TZ_NAME            [Asia/Tokyo]
#   ONLY_JOBS          [all]           comma list of error-watch,weekly-pr-review,monthly-security-audit
#   JOB_PREFIX         [cloud-ops]     Scheduler job names become <prefix>-error-watch, ...
#   INVOKER_SA         [<prefix>-invoker] service account whose OIDC token calls the service
#   REGION             [asia-northeast1]
#   APP_NAME           [app]
#   ATTEMPT_DEADLINE   [1800s]
#
# Re-running the script is safe: existing jobs are updated in place.
# Pause / resume a job:  gcloud scheduler jobs pause <prefix>-error-watch --location <region>
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID is required}"
: "${AGENT_URL:?AGENT_URL is required (e.g. https://cloud-ops-team-xxxx-an.a.run.app)}"
REGION="${REGION:-asia-northeast1}"
APP_NAME="${APP_NAME:-app}"
TZ_NAME="${TZ_NAME:-Asia/Tokyo}"
JOB_PREFIX="${JOB_PREFIX:-cloud-ops}"
INVOKER_SA="${INVOKER_SA:-${JOB_PREFIX}-invoker}"
ERROR_WATCH_CRON="${ERROR_WATCH_CRON:-0 8 * * *}"
ERROR_WATCH_HOURS="${ERROR_WATCH_HOURS:-24}"
WEEKLY_CRON="${WEEKLY_CRON:-0 9 * * 1}"
WEEKLY_DAYS="${WEEKLY_DAYS:-7}"
MONTHLY_CRON="${MONTHLY_CRON:-0 9 1 * *}"
MONTHLY_KEV_DAYS="${MONTHLY_KEV_DAYS:-30}"
ATTEMPT_DEADLINE="${ATTEMPT_DEADLINE:-1800s}"
ONLY_JOBS="${ONLY_JOBS:-error-watch,weekly-pr-review,monthly-security-audit}"

SA_EMAIL="${INVOKER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
ENDPOINT="${AGENT_URL%/}/apps/${APP_NAME}/trigger/pubsub"

echo "▶ APIs"
gcloud services enable cloudscheduler.googleapis.com --project "$PROJECT_ID" >/dev/null

echo "▶ Invoker service account: $SA_EMAIL"
gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$INVOKER_SA" --display-name "ops team scheduler invoker" --project "$PROJECT_ID"

if [[ -n "${CLOUD_RUN_SERVICE:-}" ]]; then
  echo "▶ Allow $SA_EMAIL to invoke $CLOUD_RUN_SERVICE and align its request timeout"
  gcloud run services add-iam-policy-binding "$CLOUD_RUN_SERVICE" --region "$REGION" \
    --member "serviceAccount:$SA_EMAIL" --role roles/run.invoker --project "$PROJECT_ID" >/dev/null
  gcloud run services update "$CLOUD_RUN_SERVICE" --region "$REGION" --project "$PROJECT_ID" \
    --timeout "${ATTEMPT_DEADLINE%s}" >/dev/null
fi

# Pub/Sub push envelope: data is base64, subscription becomes the session user_id.
body_for() {
  local name=$1 payload=$2 data
  data=$(printf '%s' "$payload" | base64 | tr -d '\n')
  printf '{"message":{"data":"%s","attributes":{"source":"cloud-scheduler","job":"%s"}},"subscription":"projects/%s/subscriptions/%s"}' \
    "$data" "$name" "$PROJECT_ID" "$name"
}

wanted() { [[ ",${ONLY_JOBS}," == *",$1,"* ]]; }

upsert_job() {
  local key=$1 cron=$2 payload=$3
  wanted "$key" || return 0
  local name="${JOB_PREFIX}-${key}" verb=create headers_flag=--headers body
  body=$(body_for "$name" "$payload")
  if gcloud scheduler jobs describe "$name" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
    verb=update headers_flag=--update-headers
  fi
  gcloud scheduler jobs "$verb" http "$name" --location "$REGION" --project "$PROJECT_ID" \
    --schedule "$cron" --time-zone "$TZ_NAME" \
    --uri "$ENDPOINT" --http-method POST "$headers_flag" "Content-Type=application/json" \
    --message-body "$body" \
    --oidc-service-account-email "$SA_EMAIL" --oidc-token-audience "$AGENT_URL" \
    --attempt-deadline "$ATTEMPT_DEADLINE" --max-retry-attempts 0 >/dev/null
  echo "   ✓ $name  [$cron $TZ_NAME]  $payload"
}

echo "▶ Scheduler jobs → $ENDPOINT"
upsert_job error-watch            "$ERROR_WATCH_CRON" "{\"job\":\"error_watch\",\"hours\":${ERROR_WATCH_HOURS}}"
upsert_job weekly-pr-review       "$WEEKLY_CRON"      "{\"job\":\"weekly_pr_review\",\"days\":${WEEKLY_DAYS}}"
upsert_job monthly-security-audit "$MONTHLY_CRON"     "{\"job\":\"monthly_security_audit\",\"kev_days\":${MONTHLY_KEV_DAYS}}"

echo "✅ Done. Run now: gcloud scheduler jobs run ${JOB_PREFIX}-error-watch --location $REGION --project $PROJECT_ID"
