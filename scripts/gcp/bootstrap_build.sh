#!/usr/bin/env bash
# Build IAM only. Default: read-only plan. No runtime, secrets, API or scheduler setup.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/gcp/build_profile.sh
source "$SCRIPT_DIR/build_profile.sh"
APPLY=false
case "${1:---plan}" in
  --plan) ;;
  --apply) APPLY=true ;;
  *) build_profile_error 'usage: bootstrap_build.sh [--plan|--apply]'; exit 2 ;;
esac
[[ $# -le 1 ]] || { build_profile_error 'unexpected arguments'; exit 2; }
load_build_profile
[[ "$BUILD_PROFILE_ACTIVE" == true ]] || { build_profile_error 'explicit OPS_BUILD_PROFILE is required'; exit 1; }
require_existing_build_target
require_only_build_project_role
existing="$(gcloud iam service-accounts list --project "$PROJECT_ID" \
  --filter="email=${BUILD_SA_EMAIL}" --format='value(email)')"
[[ -z "$existing" || "$existing" == "$BUILD_SA_EMAIL" ]] \
  || { build_profile_error 'unexpected account listing'; exit 1; }
if [[ -n "$existing" ]]; then require_enabled_build_identity; fi
printf '%s\n' \
  "Builder: ${BUILD_SERVICE_ACCOUNT} (create only if absent)" \
  "Project ${PROJECT_ID}: roles/logging.logWriter" \
  "Repository ${BUILD_ARTIFACT_REPOSITORY}: roles/artifactregistry.writer" \
  "Bucket ${BUILD_SOURCE_BUCKET}: roles/storage.objectViewer"
if [[ "$APPLY" != true ]]; then
  echo 'Plan only. No IAM changes. Review the exact target and use --apply separately.'
  exit 0
fi
if [[ -z "$existing" ]]; then
  gcloud iam service-accounts create gcp-ops-build --project "$PROJECT_ID" \
    --display-name='gcp-ops-team source image builder' --quiet
fi
require_enabled_build_identity
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${BUILD_SA_EMAIL}" \
  --role=roles/logging.logWriter --condition=None --quiet >/dev/null
gcloud artifacts repositories add-iam-policy-binding "$BUILD_ARTIFACT_REPOSITORY" --project "$PROJECT_ID" \
  --location "$REGION" --member="serviceAccount:${BUILD_SA_EMAIL}" --role=roles/artifactregistry.writer \
  --condition=None --quiet >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${BUILD_SOURCE_BUCKET}" \
  --member="serviceAccount:${BUILD_SA_EMAIL}" --role=roles/storage.objectViewer --condition=None --quiet >/dev/null
echo 'Build IAM applied. Live least-privilege build/push validation remains required.'
