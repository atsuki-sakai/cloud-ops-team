#!/usr/bin/env bash
# Grant the agent's runtime service account read-only roles on the project it monitors.
#
#   TARGET_PROJECT=<target-project> AGENT_SA=<sa>@<deploy-project>.iam.gserviceaccount.com \
#     ./scripts/gcp/grant_target_roles.sh
#
# Every role is a viewer / reviewer role: the team never changes cloud resources.
set -euo pipefail
: "${TARGET_PROJECT:?TARGET_PROJECT is required}"
: "${AGENT_SA:?AGENT_SA (runtime service account email) is required}"

ROLES=(
  roles/serviceusage.serviceUsageConsumer    # call APIs billed to the target (x-goog-user-project)
  roles/browser
  roles/logging.viewer                       # error logs
  roles/monitoring.viewer                    # 5xx rates, alert policies, uptime checks
  roles/run.viewer                           # Cloud Run services, revisions, IAM
  roles/cloudscheduler.viewer                # failing jobs
  roles/cloudsql.viewer                      # Cloud SQL settings and maintenance
  roles/compute.networkViewer                # firewall rules
  roles/iam.securityReviewer                 # IAM policy, service-account keys
  roles/securitycenter.findingsViewer        # Security Command Center (when enabled)
  roles/containeranalysis.occurrences.viewer # image vulnerabilities (when enabled)
  roles/artifactregistry.reader
)
for role in "${ROLES[@]}"; do
  echo "▶ $role"
  gcloud projects add-iam-policy-binding "$TARGET_PROJECT" --member "serviceAccount:${AGENT_SA}" \
    --role "$role" --condition=None >/dev/null
done
echo "✅ Done"
