#!/usr/bin/env bash
# One-time setup on Google Cloud: APIs, runtime service account and its roles,
# read-only roles on the monitored project, and the GitHub token secret.
#
#   PROJECT_ID=<deploy-project> GITHUB_TOKEN=<fine-grained PAT> ./scripts/gcp/bootstrap.sh
#   # optional: TARGET_PROJECT=<monitored-project> SERVICE=cloud-ops-team
#
# Then: ./scripts/gcp/deploy_cloud_run.sh and ./scripts/gcp/setup_schedules.sh
set -euo pipefail
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
: "${PROJECT_ID:?PROJECT_ID is required}"
SERVICE="${SERVICE:-cloud-ops-team}"
TARGET_PROJECT="${TARGET_PROJECT:-$PROJECT_ID}"
SA_ID="${SA_ID:-$SERVICE}"
SA="${SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
SECRET="${GITHUB_TOKEN_SECRET:-${SERVICE}-github-token}"

echo "▶ APIs on $PROJECT_ID"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com aiplatform.googleapis.com --project "$PROJECT_ID"

echo "▶ Runtime service account $SA"
gcloud iam service-accounts describe "$SA" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_ID" --display-name "cloud-ops-team runtime" --project "$PROJECT_ID"

echo "▶ Runtime roles (Vertex AI, secrets, telemetry)"
for role in roles/aiplatform.user roles/secretmanager.secretAccessor roles/logging.logWriter \
            roles/cloudtrace.agent roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$SA" --role "$role" \
    --condition=None >/dev/null
  echo "   ✓ $role"
done

echo "▶ Read-only roles on $TARGET_PROJECT"
TARGET_PROJECT="$TARGET_PROJECT" AGENT_SA="$SA" "$(dirname "$0")/grant_target_roles.sh"

echo "▶ GitHub token secret $SECRET"
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  if gcloud secrets describe "$SECRET" --project "$PROJECT_ID" >/dev/null 2>&1; then
    printf '%s' "$GITHUB_TOKEN" | gcloud secrets versions add "$SECRET" --data-file=- --project "$PROJECT_ID" >/dev/null
  else
    printf '%s' "$GITHUB_TOKEN" | gcloud secrets create "$SECRET" --data-file=- --replication-policy=automatic \
      --project "$PROJECT_ID" >/dev/null
  fi
  echo "   ✓ stored"
else
  echo "   (skipped: set GITHUB_TOKEN to store it)"
fi
echo "✅ Done. Next: PROJECT_ID=$PROJECT_ID GITHUB_REPO=owner/repo ./scripts/gcp/deploy_cloud_run.sh"
