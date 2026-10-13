#!/usr/bin/env bash
# Deploy the ops team to Cloud Run (source build via Cloud Build).
#
# `agents-cli deploy` copies the whole local .env into the service, which leaks local-only
# values such as credential file paths. This script passes only what the service needs.
#
#   PROJECT_ID=<project> GITHUB_REPO=owner/repo ./scripts/gcp/deploy_cloud_run.sh
#   OPS_DRY_RUN=false PROJECT_ID=<project> GITHUB_REPO=owner/repo ./scripts/gcp/deploy_cloud_run.sh
#
# Environment (defaults in brackets):
#   PROJECT_ID            [gcloud config project]   project that runs the service
#   REGION                [asia-northeast1]
#   SERVICE               [cloud-ops-team]
#   RUNTIME_SA            [<SERVICE>@<PROJECT_ID>.iam.gserviceaccount.com]
#   TARGET_PROJECT        [PROJECT_ID]               project to monitor and audit
#   TARGET_REGION         [REGION]
#   GITHUB_REPO           (required)                 owner/repo
#   GITHUB_TOKEN_SECRET   [<SERVICE>-github-token]   Secret Manager secret with the GitHub token
#   OPS_DRY_RUN           [true]                     false = the team writes to GitHub
#   OPS_AUTO_CLOSE_ISSUES [false]
#   OPS_REPORT_LANGUAGE   [ja]                       ja | en
#   OPS_MODEL             [gemini-3.8-flash]
#   OPS_STACK_KEYWORDS    [provider defaults]        comma separated
#   OPS_MAX_ISSUES_PER_RUN [10]  OPS_ERROR_LOOKBACK_HOURS [24]  OPS_RECURRENCE_COOLDOWN_HOURS [24]
#   OPS_TIMEZONE          [Asia/Tokyo]
#   MEMORY [2Gi]  CPU [1]  MIN_INSTANCES [0]  MAX_INSTANCES [2]  CONCURRENCY [4]  TIMEOUT [1800]
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
: "${PROJECT_ID:?PROJECT_ID is required}"
: "${GITHUB_REPO:?GITHUB_REPO (owner/repo) is required}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-cloud-ops-team}"
RUNTIME_SA="${RUNTIME_SA:-${SERVICE}@${PROJECT_ID}.iam.gserviceaccount.com}"
TARGET_PROJECT="${TARGET_PROJECT:-$PROJECT_ID}"
TARGET_REGION="${TARGET_REGION:-$REGION}"
GITHUB_TOKEN_SECRET="${GITHUB_TOKEN_SECRET:-${SERVICE}-github-token}"

# "^|^" switches the gcloud list delimiter to "|" so values may contain commas.
ENV_VARS="^|^OPS_CLOUD_PROVIDER=gcp"
ENV_VARS+="|GOOGLE_GENAI_USE_ENTERPRISE=true|GOOGLE_CLOUD_PROJECT=${PROJECT_ID}|GOOGLE_CLOUD_LOCATION=global"
ENV_VARS+="|OPS_TARGET=${TARGET_PROJECT}|OPS_TARGET_REGION=${TARGET_REGION}|OPS_GCP_AUTH=adc|GITHUB_REPO=${GITHUB_REPO}"
ENV_VARS+="|OPS_DRY_RUN=${OPS_DRY_RUN:-true}|OPS_AUTO_CLOSE_ISSUES=${OPS_AUTO_CLOSE_ISSUES:-false}"
ENV_VARS+="|OPS_REPORT_LANGUAGE=${OPS_REPORT_LANGUAGE:-ja}|OPS_MODEL=${OPS_MODEL:-gemini-3.8-flash}"
ENV_VARS+="|OPS_MAX_ISSUES_PER_RUN=${OPS_MAX_ISSUES_PER_RUN:-10}|OPS_ERROR_LOOKBACK_HOURS=${OPS_ERROR_LOOKBACK_HOURS:-24}"
ENV_VARS+="|OPS_RECURRENCE_COOLDOWN_HOURS=${OPS_RECURRENCE_COOLDOWN_HOURS:-24}|OPS_TIMEZONE=${OPS_TIMEZONE:-Asia/Tokyo}"
ENV_VARS+="|ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false"
[[ -n "${OPS_STACK_KEYWORDS:-}" ]] && ENV_VARS+="|OPS_STACK_KEYWORDS=${OPS_STACK_KEYWORDS}"

cd "$(dirname "$0")/../.."
gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --source . \
  --memory "${MEMORY:-2Gi}" --cpu "${CPU:-1}" \
  --min-instances "${MIN_INSTANCES:-0}" --max-instances "${MAX_INSTANCES:-2}" --concurrency "${CONCURRENCY:-4}" \
  --timeout "${TIMEOUT:-1800}" --no-allow-unauthenticated \
  --service-account "$RUNTIME_SA" \
  --set-env-vars "$ENV_VARS" \
  --update-secrets "GITHUB_TOKEN=${GITHUB_TOKEN_SECRET}:latest" \
  --labels "app=cloud-ops-team" \
  --quiet
echo "SERVICE_URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
