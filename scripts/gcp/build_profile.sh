#!/usr/bin/env bash
# Shared target contract for the opt-in, build-identity-only migration.
BUILD_PROFILE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

build_profile_error() {
  echo "Build profile: $*" >&2
  return 1
}

check_build_profile_value() {
  local name="$1" expected="$2" actual=""
  actual="${!name:-}"
  if [[ -n "$actual" && "$actual" != "$expected" ]]; then
    build_profile_error "${name} conflicts with the selected target"
    return 1
  fi
}

# shellcheck disable=SC2034 # Profile values are read by the scripts sourcing this function.
load_build_profile() {
  BUILD_PROFILE_ACTIVE=false
  case "${OPS_BUILD_PROFILE:-}" in
    '') return 0 ;;
    yadora-prod-gcp-ops) ;;
    *) build_profile_error 'unknown OPS_BUILD_PROFILE'; return 1 ;;
  esac
  check_build_profile_value PROJECT_ID yadora-prod || return 1
  check_build_profile_value GOOGLE_CLOUD_PROJECT yadora-prod || return 1
  check_build_profile_value REGION asia-northeast1 || return 1
  check_build_profile_value SERVICE gcp-ops-team || return 1
  check_build_profile_value RUNTIME_SA gcp-ops-team@yadora-prod.iam.gserviceaccount.com || return 1
  check_build_profile_value BUILD_SA_EMAIL gcp-ops-build@yadora-prod.iam.gserviceaccount.com || return 1
  check_build_profile_value BUILD_SERVICE_ACCOUNT projects/yadora-prod/serviceAccounts/gcp-ops-build@yadora-prod.iam.gserviceaccount.com || return 1
  check_build_profile_value BUILD_SOURCE_BUCKET run-sources-yadora-prod-asia-northeast1 || return 1
  check_build_profile_value BUILD_ARTIFACT_REPOSITORY cloud-run-source-deploy || return 1
  PROJECT_ID=yadora-prod
  REGION=asia-northeast1
  SERVICE=gcp-ops-team
  RUNTIME_SA=gcp-ops-team@yadora-prod.iam.gserviceaccount.com
  BUILD_SA_EMAIL=gcp-ops-build@yadora-prod.iam.gserviceaccount.com
  BUILD_SERVICE_ACCOUNT="projects/${PROJECT_ID}/serviceAccounts/${BUILD_SA_EMAIL}"
  BUILD_SOURCE_BUCKET=run-sources-yadora-prod-asia-northeast1
  BUILD_ARTIFACT_REPOSITORY=cloud-run-source-deploy
  BUILD_PROFILE_ACTIVE=true
}

require_existing_build_target() {
  local runtime repository bucket
  runtime="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
    --format='value(spec.template.spec.serviceAccountName)')" || return 1
  [[ "$runtime" == "$RUNTIME_SA" ]] || { build_profile_error 'existing runtime identity does not match'; return 1; }
  repository="$(gcloud artifacts repositories describe "$BUILD_ARTIFACT_REPOSITORY" \
    --project "$PROJECT_ID" --location "$REGION" --format='value(name,format)')" || return 1
  [[ "$repository" == "projects/${PROJECT_ID}/locations/${REGION}/repositories/${BUILD_ARTIFACT_REPOSITORY}"$'\tDOCKER' ]] \
    || { build_profile_error 'existing Docker repository does not match'; return 1; }
  bucket="$(gcloud storage buckets describe "gs://${BUILD_SOURCE_BUCKET}" --format='value(name)')" || return 1
  [[ "$bucket" == "$BUILD_SOURCE_BUCKET" ]] || { build_profile_error 'existing source bucket does not match'; return 1; }
}

require_enabled_build_identity() {
  local metadata
  metadata="$(gcloud iam service-accounts describe "$BUILD_SA_EMAIL" --project "$PROJECT_ID" \
    --format='json(email,disabled)')" || return 1
  printf '%s' "$metadata" | uv run --quiet python "$BUILD_PROFILE_SCRIPT_DIR/check_build_identity.py" "$BUILD_SA_EMAIL"
}

require_only_build_project_role() {
  local roles role
  roles="$(gcloud projects get-iam-policy "$PROJECT_ID" --flatten='bindings[].members' \
    --filter="bindings.members=serviceAccount:${BUILD_SA_EMAIL}" --format='value(bindings.role)')" || return 1
  while IFS= read -r role; do
    case "$role" in
      ''|roles/logging.logWriter) ;;
      *) build_profile_error 'builder has unexpected project roles; review its IAM before proceeding'; return 1 ;;
    esac
  done <<< "$roles"
}
