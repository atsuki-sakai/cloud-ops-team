#!/usr/bin/env bash
# Cloud Run へデプロイ（ソースから Cloud Build でビルド）。
#
# `agents-cli deploy` はローカル .env をそのまま環境変数に流し込むため、ローカル専用の
# GOOGLE_APPLICATION_CREDENTIALS（ローカルパス）やカンマ入りの OPS_STACK_KEYWORDS が壊れる。
# ここでは必要な変数だけを明示して gcloud を直接呼ぶ。
#
#   PROJECT_ID=<project> GITHUB_REPO=owner/repo ./scripts/deploy_cloud_run.sh
#   OPS_DRY_RUN=false PROJECT_ID=<project> GITHUB_REPO=owner/repo ./scripts/deploy_cloud_run.sh   # 本番起票を有効化
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
: "${PROJECT_ID:?PROJECT_ID（デプロイ先プロジェクト）を指定してください}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-gcp-ops-team}"
RUNTIME_SA="${RUNTIME_SA:-gcp-ops-team@${PROJECT_ID}.iam.gserviceaccount.com}"
TARGET_PROJECT="${TARGET_PROJECT:-$PROJECT_ID}"
TARGET_REGION="${TARGET_REGION:-$REGION}"
GITHUB_REPO="${GITHUB_REPO:?GITHUB_REPO (owner/repo) を指定してください}"
GITHUB_TOKEN_SECRET="${GITHUB_TOKEN_SECRET:-gcp-ops-team-github-token}"
OPS_DRY_RUN="${OPS_DRY_RUN:-true}"
OPS_AUTO_CLOSE_ISSUES="${OPS_AUTO_CLOSE_ISSUES:-false}"
OPS_STACK_KEYWORDS="${OPS_STACK_KEYWORDS:-Cloud Run,Cloud SQL,PostgreSQL,Node.js,Next.js,Rust,Firebase,Stripe,Debian,Alpine,Linux}"

# "^|^" で区切り文字を | に変更（値にカンマを含められる）
ENV_VARS="^|^GOOGLE_GENAI_USE_ENTERPRISE=true"
ENV_VARS+="|GOOGLE_CLOUD_PROJECT=${PROJECT_ID}|GOOGLE_CLOUD_LOCATION=global"
ENV_VARS+="|GCP_TARGET_PROJECT=${TARGET_PROJECT}|GCP_TARGET_REGION=${TARGET_REGION}|GITHUB_REPO=${GITHUB_REPO}|OPS_GCP_AUTH=adc"
ENV_VARS+="|OPS_DRY_RUN=${OPS_DRY_RUN}|OPS_AUTO_CLOSE_ISSUES=${OPS_AUTO_CLOSE_ISSUES}|OPS_MAX_ISSUES_PER_RUN=${OPS_MAX_ISSUES_PER_RUN:-10}"
ENV_VARS+="|OPS_ERROR_LOOKBACK_HOURS=${OPS_ERROR_LOOKBACK_HOURS:-24}|OPS_RECURRENCE_COOLDOWN_HOURS=${OPS_RECURRENCE_COOLDOWN_HOURS:-24}|OPS_TIMEZONE=${OPS_TIMEZONE:-Asia/Tokyo}"
ENV_VARS+="|OPS_STACK_KEYWORDS=${OPS_STACK_KEYWORDS}|ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false"

cd "$(dirname "$0")/.."
gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --source . \
  --memory "${MEMORY:-2Gi}" --cpu "${CPU:-1}" \
  --min-instances "${MIN_INSTANCES:-0}" --max-instances "${MAX_INSTANCES:-2}" --concurrency "${CONCURRENCY:-4}" \
  --timeout "${TIMEOUT:-1800}" --no-allow-unauthenticated \
  --service-account "$RUNTIME_SA" \
  --set-env-vars "$ENV_VARS" \
  --update-secrets "GITHUB_TOKEN=${GITHUB_TOKEN_SECRET}:latest" \
  --labels "app=gcp-ops-team,created-by=gcp-ops-team-deploy" \
  --quiet
echo "SERVICE_URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
