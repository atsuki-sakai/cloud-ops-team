# Configuration

Everything is configured with environment variables: `.env` locally (see [`.env.example`](../.env.example)),
service environment variables when deployed (`scripts/gcp/deploy_cloud_run.sh` sets them).

## Agent settings (`app/config.py`)

### What to operate

| Variable | Default | Description |
|---|---|---|
| `OPS_CLOUD_PROVIDER` | `gcp` | `gcp`, `aws`, `azure` or a third-party provider key ([providers.md](providers.md)) |
| `OPS_TARGET` | provider-specific, see below | Scope to monitor: GCP project, AWS account id or Azure subscription id |
| `OPS_TARGET_REGION` | provider-specific, see below | Region of the monitored workload |
| `GITHUB_REPO` | — | `owner/repo` where issues and pull requests live |
| `GITHUB_TOKEN` | `gh auth token` locally | Token for GitHub (`GH_TOKEN` is also accepted) |

Provider-specific fallbacks when `OPS_TARGET` / `OPS_TARGET_REGION` are not set:

| Provider | Target from | Region from | Default region |
|---|---|---|---|
| `gcp` | `GCP_TARGET_PROJECT`, `GOOGLE_CLOUD_PROJECT` | `GCP_TARGET_REGION` | `asia-northeast1` |
| `aws` | `AWS_ACCOUNT_ID` | `AWS_REGION`, `AWS_DEFAULT_REGION` | `ap-northeast-1` |
| `azure` | `AZURE_SUBSCRIPTION_ID` | `AZURE_LOCATION` | `japaneast` |

### Safety

| Variable | Default | Description |
|---|---|---|
| `OPS_DRY_RUN` | `true` | Never write to GitHub; tools return previews of what they would create |
| `OPS_AUTO_CLOSE_ISSUES` | `false` | Let the weekly review close issues it verified (otherwise it labels `ops:verified` and comments) |
| `OPS_MAX_ISSUES_PER_RUN` | `10` | Maximum new issues per run |
| `OPS_RECURRENCE_COOLDOWN_HOURS` | `24` | At most one recurrence comment per issue within this window |

### Behaviour

| Variable | Default | Description |
|---|---|---|
| `OPS_REPORT_LANGUAGE` | `ja` | Language of reports, issues and comments: `ja` or `en` |
| `OPS_ERROR_LOOKBACK_HOURS` | `24` | Default error-watch window when a job does not pass `hours` |
| `OPS_TIMEZONE` | `Asia/Tokyo` | Time zone of timestamps in reports |
| `OPS_STACK_KEYWORDS` | provider defaults + common OS / runtimes | Comma-separated keywords that filter CISA KEV entries and focus web threat research |
| `OPS_MODEL` | `gemini-3.8-flash` | Model of every agent |
| `OPS_RESEARCH_MODEL` | `OPS_MODEL` | Model for web threat research (must support Google Search grounding) |

### Model access

| Variable | Description |
|---|---|
| `GOOGLE_GENAI_USE_ENTERPRISE=true`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=global` | Gemini on Vertex AI with ADC (default) |
| `GEMINI_API_KEY` | Gemini API instead of Vertex AI (works from any cloud) |

### Google Cloud provider

| Variable | Default | Description |
|---|---|---|
| `OPS_GCP_AUTH` | `adc` | `adc` (runtime service account / `gcloud auth application-default login`) or `gcloud` (borrow `gcloud auth print-access-token`, handy when local ADC belongs to another account) |

## Schedules (`scripts/gcp/setup_schedules.sh`)

The script creates or updates three Cloud Scheduler jobs. Every value can be changed; re-run the script to apply.

| Variable | Default | Description |
|---|---|---|
| `ERROR_WATCH_CRON` | `0 8 * * *` | Error watch schedule (daily at 08:00) |
| `ERROR_WATCH_HOURS` | `24` | Look-back window of each error watch — keep it equal to the interval |
| `WEEKLY_CRON` / `WEEKLY_DAYS` | `0 9 * * 1` / `7` | Weekly issue and PR review |
| `MONTHLY_CRON` / `MONTHLY_KEV_DAYS` | `0 9 1 * *` / `30` | Monthly security audit |
| `TZ_NAME` | `Asia/Tokyo` | Time zone of the cron expressions |
| `ONLY_JOBS` | all | Comma list of `error-watch`, `weekly-pr-review`, `monthly-security-audit` to update |
| `JOB_PREFIX` | `cloud-ops` | Job names become `<prefix>-error-watch`, ... |
| `INVOKER_SA` | `<prefix>-invoker` | Service account whose OIDC token calls the service |
| `ATTEMPT_DEADLINE` | `1800s` | Maximum run time per job (also applied to the Cloud Run request timeout) |

Examples:

```bash
COMMON="PROJECT_ID=my-project AGENT_URL=https://cloud-ops-team-xxxx.a.run.app"

# Error watch every hour (look back 1 hour)
env $COMMON ERROR_WATCH_CRON="0 * * * *" ERROR_WATCH_HOURS=1 ONLY_JOBS=error-watch ./scripts/gcp/setup_schedules.sh

# Every 6 hours
env $COMMON ERROR_WATCH_CRON="0 */6 * * *" ERROR_WATCH_HOURS=6 ONLY_JOBS=error-watch ./scripts/gcp/setup_schedules.sh

# Weekdays at 09:30, covering the weekend on Mondays
env $COMMON ERROR_WATCH_CRON="30 9 * * 1-5" ERROR_WATCH_HOURS=72 ONLY_JOBS=error-watch ./scripts/gcp/setup_schedules.sh

# Monthly audit on the 15th instead of the 1st
env $COMMON MONTHLY_CRON="0 9 15 * *" ONLY_JOBS=monthly-security-audit ./scripts/gcp/setup_schedules.sh
```

Pause or resume a job without changing it:

```bash
gcloud scheduler jobs pause  cloud-ops-error-watch --location asia-northeast1
gcloud scheduler jobs resume cloud-ops-error-watch --location asia-northeast1
```

A longer look-back than the interval is harmless — duplicates are de-duplicated by fingerprint — but costs more tokens.

## Deployment (`scripts/gcp/deploy_cloud_run.sh`)

The deploy script accepts every agent setting above plus `PROJECT_ID`, `REGION`, `SERVICE` (`cloud-ops-team`), `RUNTIME_SA`,
`TARGET_PROJECT`, `TARGET_REGION`, `GITHUB_TOKEN_SECRET` (`<SERVICE>-github-token`) and the sizing variables `MEMORY` (`2Gi`),
`CPU` (`1`), `MIN_INSTANCES` (`0`), `MAX_INSTANCES` (`2`), `CONCURRENCY` (`4`), `TIMEOUT` (`1800`).
It uses `--set-env-vars`, so every deploy fully defines the service configuration: pass `OPS_DRY_RUN=false` on each deploy
of a service that should write to GitHub.
