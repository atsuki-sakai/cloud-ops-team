# Deploying on Google Cloud

日本語版（メイン）: [docs/deployment-gcp.md](../deployment-gcp.md)

The team runs as one Cloud Run service. Cloud Scheduler calls it with OIDC-authenticated HTTP requests.
Total cost is dominated by model tokens: a daily error watch, a weekly review and a monthly audit amount to a few dozen
short-lived requests per month, and the service scales to zero in between.

```mermaid
flowchart LR
    SCH["Cloud Scheduler<br/>3 HTTP jobs"] -- "OIDC (invoker SA)" --> RUN["Cloud Run<br/>cloud-ops-team"]
    RUN -- "runtime SA" --> VTX["Vertex AI (Gemini)"]
    RUN -- "read-only roles" --> TGT["Monitored project<br/>Logging · Monitoring · Run · SQL · IAM · SCC"]
    RUN -- "token from Secret Manager" --> GH["GitHub issues / PRs"]
```

## Prerequisites

- `gcloud` authenticated as a user who can create service accounts, grant IAM roles and deploy Cloud Run
- A GitHub token for the repository: a fine-grained token with **Issues: read and write**, **Pull requests: read**,
  **Actions: read**, **Contents: read**, **Metadata: read** (add **Administration: read** to let the audit check branch protection)
- The monitored project can be the same as the deploy project or a different one

Every step below has a `make` target that reads `.env` (create it with `make setup`, check it with `make doctor`).
The underlying scripts in `scripts/gcp/` take the same values as environment variables if you prefer to call them directly.

## 1. Bootstrap (once)

```bash
make gcp-bootstrap          # or: PROJECT_ID=my-project TARGET_PROJECT=my-project GITHUB_TOKEN=github_pat_xxx ./scripts/gcp/bootstrap.sh
```

This enables the required APIs, creates the runtime service account `cloud-ops-team@<project>` with
`roles/aiplatform.user`, `roles/secretmanager.secretAccessor`, `roles/logging.logWriter`, `roles/cloudtrace.agent` and
`roles/monitoring.metricWriter`, grants it read-only roles on the target (`scripts/gcp/grant_target_roles.sh`), and stores the
token in the secret `cloud-ops-team-github-token` (it asks for the token when `GITHUB_TOKEN` is not set).

## 2. Deploy

Start in dry-run mode, look at what the team would file, then enable writes:

```bash
make gcp-deploy                         # dry run (OPS_DRY_RUN from .env)
OPS_DRY_RUN=false make gcp-deploy       # start filing issues
```

The script prints `SERVICE_URL=...` and configures the service to verify the scheduler's OIDC tokens for that URL
(`OPS_TRIGGER_AUDIENCE`, `OPS_TRIGGER_SERVICE_ACCOUNTS`). Smoke test it:

```bash
URL=$(gcloud run services describe cloud-ops-team --region asia-northeast1 --format='value(status.url)')
curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/list-apps"     # ["app"]
```

## 3. Schedule

```bash
make jobs                    # what will be scheduled
make gcp-schedule            # create or update the jobs (the service URL is detected automatically)
gcloud scheduler jobs run cloud-ops-error-watch --location asia-northeast1   # run once now
```

Defaults: error watch daily at 08:00, weekly review on Mondays at 09:00, monthly audit on the 1st at 09:00 (Asia/Tokyo).
Change any of them with environment variables — see [configuration.md](configuration.md#schedules-make-gcp-schedule).

## Runbook

| Task | Command |
|---|---|
| Check configuration and access | `make doctor` |
| Recent runs | `make gcp-logs`, or `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="cloud-ops-team" AND httpRequest.requestUrl:"trigger/pubsub"' --limit 10 --format="value(timestamp,httpRequest.status,httpRequest.latency)"` |
| Issues created by the team | `gh issue list -R owner/repo --label ops-agent --state all` |
| Stop / resume writing to GitHub | `gcloud run services update cloud-ops-team --region asia-northeast1 --update-env-vars OPS_DRY_RUN=true` (`false` to resume) |
| Pause a job | `gcloud scheduler jobs pause cloud-ops-error-watch --location asia-northeast1` (`resume` to restart) |
| Change a schedule | `ERROR_WATCH_CRON=... ONLY_JOBS=error-watch make gcp-schedule` |
| Ship code or configuration changes | `make test && make gcp-deploy` |
| Rotate the GitHub token | `printf '%s' "$TOKEN" \| gcloud secrets versions add cloud-ops-team-github-token --data-file=-` (picked up by new instances) |
| Roll back | `gcloud run services update-traffic cloud-ops-team --region asia-northeast1 --to-revisions=<previous-revision>=100` |
| Errors of the service itself | `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="cloud-ops-team" AND severity>=ERROR' --limit 20` |

## Monitoring several projects

Deploy one service per target (`SERVICE=cloud-ops-staging TARGET_PROJECT=...`) with its own scheduler prefix
(`JOB_PREFIX=cloud-ops-staging`). They can share one repository: de-duplication is scoped to the target recorded in each issue.

## Using Terraform or CI/CD instead

The scripts are intentionally small. If you prefer infrastructure as code, `agents-cli scaffold enhance . --deployment-target cloud_run`
generates Terraform for the service and its service account, and `--cicd-runner github_actions` adds a deployment pipeline;
port the scheduler jobs and IAM bindings from the scripts.
