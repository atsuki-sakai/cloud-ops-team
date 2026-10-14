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
#   <JOB>_CRON         [job default]   any job added in app/specialists: e.g. COST_REPORT_CRON
#   ONLY_JOBS          [all]           comma list of schedule keys, e.g. error-watch,weekly-pr-review
#   JOB_PREFIX         [cloud-ops]     Scheduler job names become <prefix>-error-watch, ...
#   INVOKER_SA         [<prefix>-invoker] service account whose OIDC token calls the service
#   PROJECT_ID         [GOOGLE_CLOUD_PROJECT, gcloud config]
#   REGION             [OPS_TARGET_REGION or asia-northeast1]
#   CLOUD_RUN_SERVICE  [SERVICE or cloud-ops-team]
#   AGENT_URL          [URL of CLOUD_RUN_SERVICE]
#   APP_NAME           [app]
#   ATTEMPT_DEADLINE   [1800s]
#
# Re-running the script is safe: existing jobs are updated in place.
# Pause / resume a job:  gcloud scheduler jobs pause <prefix>-error-watch --location <region>
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}"
: "${PROJECT_ID:?PROJECT_ID is required}"
REGION="${REGION:-${OPS_TARGET_REGION:-asia-northeast1}}"
CLOUD_RUN_SERVICE="${CLOUD_RUN_SERVICE:-${SERVICE:-cloud-ops-team}}"
if [[ -z "${AGENT_URL:-}" ]]; then
  AGENT_URL="$(gcloud run services describe "$CLOUD_RUN_SERVICE" --project "$PROJECT_ID" --region "$REGION" \
    --format='value(status.url)' 2>/dev/null || true)"
fi
: "${AGENT_URL:?AGENT_URL not found: deploy first (make gcp-deploy) or pass AGENT_URL}"
APP_NAME="${APP_NAME:-app}"
TZ_NAME="${TZ_NAME:-Asia/Tokyo}"
JOB_PREFIX="${JOB_PREFIX:-cloud-ops}"
INVOKER_SA="${INVOKER_SA:-${JOB_PREFIX}-invoker}"
ATTEMPT_DEADLINE="${ATTEMPT_DEADLINE:-1800s}"
ONLY_JOBS="${ONLY_JOBS:-}"

SA_EMAIL="${INVOKER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
ENDPOINT="${AGENT_URL%/}/apps/${APP_NAME}/trigger/pubsub"

echo "▶ APIs"
gcloud services enable cloudscheduler.googleapis.com --project "$PROJECT_ID" >/dev/null

echo "▶ Invoker service account: $SA_EMAIL"
gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$INVOKER_SA" --display-name "ops team scheduler invoker" --project "$PROJECT_ID"

if [[ "${SKIP_SERVICE_UPDATE:-}" != "1" ]]; then
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

wanted() { [[ -z "$ONLY_JOBS" || ",${ONLY_JOBS}," == *",$1,"* ]]; }

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
JOBS="$(cd "$ROOT" && uv run --quiet python scripts/jobs.py schedules)"
[[ -n "$JOBS" ]] || { echo "No scheduled jobs defined in app/specialists"; exit 1; }
while IFS=$'\t' read -r key cron payload; do
  upsert_job "$key" "$cron" "$payload"
done <<< "$JOBS"

echo "✅ Done. Run now: gcloud scheduler jobs run ${JOB_PREFIX}-error-watch --location $REGION --project $PROJECT_ID"
