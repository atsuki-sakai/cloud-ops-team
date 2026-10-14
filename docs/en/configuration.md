# Configuration

日本語版（メイン）: [docs/configuration.md](../configuration.md)

Everything is configured with environment variables: `.env` locally (create it with `make setup`, template
[`.env.example`](../../.env.example)), service environment variables when deployed (`make gcp-deploy` sets them from `.env`).
Run `make doctor` after any change to check that the configuration works.

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

Boolean settings accept `true/false`, `yes/no`, `on/off` or `1/0`, case-insensitively.
Unset or empty values use the default; other values stop startup with a configuration error.
`make setup` gives the owner exclusive read/write access to `.env` and its backup.
`.env.backup-*` files are excluded from Git and deployment uploads.

### Behaviour

| Variable | Default | Description |
|---|---|---|
| `OPS_REPORT_LANGUAGE` | `ja` | Language of reports, issues and comments: `ja` or `en` |
| `OPS_ERROR_LOOKBACK_HOURS` | `24` | Default error-watch window when a job does not pass `hours` |
| `OPS_TIMEZONE` | `Asia/Tokyo` | Time zone of timestamps in reports |
| `OPS_STACK_KEYWORDS` | provider defaults + common OS / runtimes | Comma-separated keywords that filter CISA KEV entries and focus web threat research |
| `OPS_MODEL` | `gemini-3.8-flash` | Model of every agent |
| `OPS_RESEARCH_MODEL` | `OPS_MODEL` | Model for web threat research (must support Google Search grounding) |

### Fitting your repository and project

| Variable | Default | Description |
|---|---|---|
| `OPS_ERROR_LABELS` | `bug` | Extra labels on error issues (comma separated, empty = none) |
| `OPS_SECURITY_LABELS` | `security` | Extra labels on security issues |
| `OPS_SEVERITY_LABELS` | `critical=severity: critical,high=severity: high,medium=severity: medium,low=severity: low` | Severity → labels (`high=a\|b` for several) |
| `OPS_CONTEXT_FILE` | `config/ops-context.md` | Project knowledge appended to every instruction (copy `config/ops-context.example.md`) |

Every issue also gets `ops-agent` and `ops:error` / `ops:security` / `ops:report`, which the team relies on.

### Trigger authentication (server)

| Variable | Default | Description |
|---|---|---|
| `OPS_TRIGGER_AUDIENCE` | set by `make gcp-deploy` to the service URL | ADK verifies the scheduler's Google-signed OIDC token against this audience. Empty: authentication is left to Cloud Run IAM (or local development) |
| `OPS_TRIGGER_SERVICE_ACCOUNTS` | `<JOB_PREFIX>-invoker@<project>` | Callers allowed to trigger jobs when the audience is set |

### Model access

| Variable | Description |
|---|---|
| `GOOGLE_GENAI_USE_ENTERPRISE=true`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=global` | Gemini on Vertex AI with ADC (default) |
| `GEMINI_API_KEY` | Gemini API instead of Vertex AI (works from any cloud) |

### Google Cloud provider

| Variable | Default | Description |
|---|---|---|
| `OPS_GCP_AUTH` | `adc` | `adc` (runtime service account / `gcloud auth application-default login`) or `gcloud` (borrow `gcloud auth print-access-token`, handy when local ADC belongs to another account) |

## Schedules (`make gcp-schedule`)

Scheduler jobs are generated from the jobs declared by the specialists in `app/specialists/` — `make jobs` lists them.
Every value can be overridden; re-run `make gcp-schedule` to apply (existing jobs are updated in place).

| Variable | Default | Description |
|---|---|---|
| `ERROR_WATCH_CRON` / `ERROR_WATCH_HOURS` | `0 8 * * *` / `24` | Error watch (daily at 08:00) and its look-back window — keep the window equal to the interval |
| `WEEKLY_CRON` / `WEEKLY_DAYS` | `0 9 * * 1` / `7` | Weekly issue and PR review |
| `MONTHLY_CRON` / `MONTHLY_KEV_DAYS` | `0 9 1 * *` / `30` | Monthly security audit |
| `<SCHEDULE_KEY>_CRON` | job default | Jobs you add, e.g. `DEPLOY_WATCH_CRON` for schedule key `deploy-watch` |
| `TZ_NAME` | `Asia/Tokyo` | Time zone of the cron expressions |
| `ONLY_JOBS` | all | Comma list of schedule keys to update, e.g. `error-watch` |
| `JOB_PREFIX` / `INVOKER_SA` | `cloud-ops` / `<prefix>-invoker` | Job names (`<prefix>-error-watch`) and the identity that calls the service |
| `PROJECT_ID`, `REGION`, `CLOUD_RUN_SERVICE` / `SERVICE`, `AGENT_URL` | from `.env`, auto-detected URL | Where the jobs point |
| `ATTEMPT_DEADLINE` | `1800s` | Maximum run time per job (also applied to the Cloud Run request timeout) |
| `SKIP_SERVICE_UPDATE` | — | `1` skips the invoker binding / timeout update on the service |

Examples:

```bash
ERROR_WATCH_CRON="0 * * * *" ERROR_WATCH_HOURS=1 ONLY_JOBS=error-watch make gcp-schedule        # hourly
ERROR_WATCH_CRON="0 */6 * * *" ERROR_WATCH_HOURS=6 ONLY_JOBS=error-watch make gcp-schedule      # every 6 hours
ERROR_WATCH_CRON="30 9 * * 1-5" ERROR_WATCH_HOURS=72 ONLY_JOBS=error-watch make gcp-schedule    # weekdays, Monday covers the weekend
MONTHLY_CRON="0 9 15 * *" ONLY_JOBS=monthly-security-audit make gcp-schedule                    # audit on the 15th
```

Pause or resume a job without changing it:

```bash
gcloud scheduler jobs pause  cloud-ops-error-watch --location asia-northeast1
gcloud scheduler jobs resume cloud-ops-error-watch --location asia-northeast1
```

A longer look-back than the interval is harmless — duplicates are de-duplicated by fingerprint — but costs more tokens.

## Deployment (`make gcp-deploy`)

`make gcp-deploy` runs `scripts/gcp/deploy_cloud_run.sh` with `.env` loaded. It accepts every agent setting above plus
`PROJECT_ID` (default `GOOGLE_CLOUD_PROJECT`), `REGION` (default `OPS_TARGET_REGION`), `SERVICE` (`cloud-ops-team`), `RUNTIME_SA`,
`TARGET_PROJECT`, `TARGET_REGION`, `GITHUB_TOKEN_SECRET` (`<SERVICE>-github-token`) and the sizing variables `MEMORY` (`2Gi`),
`CPU` (`1`), `MIN_INSTANCES` (`0`), `MAX_INSTANCES` (`2`), `CONCURRENCY` (`4`), `TIMEOUT` (`1800`).
It writes only allowlisted settings to a temporary `--env-vars-file`, preserving commas, pipes and empty label settings.
The file is removed on exit. Every deploy fully defines the service configuration: pass `OPS_DRY_RUN=false` on each deploy
of a service that should write to GitHub.
