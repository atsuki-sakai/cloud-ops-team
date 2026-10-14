#!/usr/bin/env bash
# Deploy the ops team to Cloud Run (source build via Cloud Build).
#
# `agents-cli deploy` copies the whole local .env into the service, which leaks local-only
# values such as credential file paths. This script passes only what the service needs.
#
#   make gcp-deploy                                 # reads .env
#   PROJECT_ID=<project> GITHUB_REPO=owner/repo ./scripts/gcp/deploy_cloud_run.sh
#   OPS_DRY_RUN=false make gcp-deploy                # let the team write to GitHub
#
# Environment (defaults in brackets):
#   PROJECT_ID            [GOOGLE_CLOUD_PROJECT, gcloud config]   project that runs the service
#   REGION                [OPS_TARGET_REGION or asia-northeast1]
#   SERVICE               [cloud-ops-team]
#   RUNTIME_SA            [<SERVICE>@<PROJECT_ID>.iam.gserviceaccount.com]
#   TARGET_PROJECT        [OPS_TARGET or PROJECT_ID] project to monitor and audit
#   TARGET_REGION         [OPS_TARGET_REGION or REGION]
#   GITHUB_REPO           (required)                 owner/repo
#   GITHUB_TOKEN_SECRET   [<SERVICE>-github-token]   Secret Manager secret with the GitHub token
#   OPS_DRY_RUN           [true]                     false = the team writes to GitHub
#   OPS_AUTO_CLOSE_ISSUES [false]
#   OPS_REPORT_LANGUAGE   [ja]                       ja | en
#   OPS_MODEL             [gemini-3.8-flash]
#   OPS_STACK_KEYWORDS    [provider defaults]        comma separated
#   OPS_MAX_ISSUES_PER_RUN [10]  OPS_ERROR_LOOKBACK_HOURS [24]  OPS_RECURRENCE_COOLDOWN_HOURS [24]
#   OPS_TIMEZONE          [Asia/Tokyo]
#   OPS_ERROR_LABELS / OPS_SECURITY_LABELS / OPS_SEVERITY_LABELS   [app defaults] issue labels
#   OPS_TRIGGER_AUDIENCE  [service URL]             OIDC audience the scheduler uses (ADK verifies tokens)
#   OPS_TRIGGER_SERVICE_ACCOUNTS [<JOB_PREFIX>-invoker SA]  callers allowed to trigger jobs
#   JOB_PREFIX [cloud-ops]  INVOKER_SA [<JOB_PREFIX>-invoker]   same as scripts/gcp/setup_schedules.sh
#   MEMORY [2Gi]  CPU [1]  MIN_INSTANCES [0]  MAX_INSTANCES [2]  CONCURRENCY [4]  TIMEOUT [1800]
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}"
: "${PROJECT_ID:?PROJECT_ID is required}"
: "${GITHUB_REPO:?GITHUB_REPO (owner/repo) is required}"
REGION="${REGION:-${OPS_TARGET_REGION:-asia-northeast1}}"
SERVICE="${SERVICE:-cloud-ops-team}"
RUNTIME_SA="${RUNTIME_SA:-${SERVICE}@${PROJECT_ID}.iam.gserviceaccount.com}"
TARGET_PROJECT="${TARGET_PROJECT:-${OPS_TARGET:-$PROJECT_ID}}"
TARGET_REGION="${TARGET_REGION:-${OPS_TARGET_REGION:-$REGION}}"
GITHUB_TOKEN_SECRET="${GITHUB_TOKEN_SECRET:-${SERVICE}-github-token}"

INVOKER_SA="${INVOKER_SA:-${JOB_PREFIX:-cloud-ops}-invoker}"
OPS_TRIGGER_SERVICE_ACCOUNTS="${OPS_TRIGGER_SERVICE_ACCOUNTS:-${INVOKER_SA}@${PROJECT_ID}.iam.gserviceaccount.com}"
# The scheduler's OIDC tokens carry the service URL as audience; known once the service exists.
OPS_TRIGGER_AUDIENCE="${OPS_TRIGGER_AUDIENCE:-$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" \
  --region "$REGION" --format='value(status.url)' 2>/dev/null || true)}"

if ! gcloud secrets describe "$GITHUB_TOKEN_SECRET" --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "Secret $GITHUB_TOKEN_SECRET not found in $PROJECT_ID. Run make gcp-bootstrap (or scripts/gcp/bootstrap.sh) first." >&2
  exit 1
fi

cd "$(dirname "$0")/../.."
# An env file preserves commas, pipes, whitespace and empty label settings.
# JSON is also a valid YAML mapping for gcloud's --env-vars-file.
ENV_FILE="$(mktemp "${TMPDIR:-/tmp}/cloud-ops-env.XXXXXX")"
trap 'rm -f "$ENV_FILE"' EXIT
export PROJECT_ID TARGET_PROJECT TARGET_REGION OPS_TRIGGER_AUDIENCE OPS_TRIGGER_SERVICE_ACCOUNTS
uv run --quiet python scripts/gcp/deploy_env.py "$ENV_FILE"

gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --source . \
  --memory "${MEMORY:-2Gi}" --cpu "${CPU:-1}" \
  --min-instances "${MIN_INSTANCES:-0}" --max-instances "${MAX_INSTANCES:-2}" --concurrency "${CONCURRENCY:-4}" \
  --timeout "${TIMEOUT:-1800}" --no-allow-unauthenticated \
  --service-account "$RUNTIME_SA" \
  --env-vars-file "$ENV_FILE" \
  --update-secrets "GITHUB_TOKEN=${GITHUB_TOKEN_SECRET}:latest" \
  --labels "app=cloud-ops-team" \
  --quiet
SERVICE_URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
if [[ -z "$OPS_TRIGGER_AUDIENCE" ]]; then
  # First deploy: the URL is known only now. Enable token verification for the scheduler.
  gcloud run services update "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --quiet \
    --update-env-vars "^|^OPS_TRIGGER_AUDIENCE=${SERVICE_URL}|OPS_TRIGGER_SERVICE_ACCOUNTS=${OPS_TRIGGER_SERVICE_ACCOUNTS}" >/dev/null
fi
echo "SERVICE_URL=${SERVICE_URL}"
