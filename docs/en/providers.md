# Cloud providers

日本語版（メイン）: [docs/providers.md](../providers.md)

A provider adapts one cloud to the fixed set of read-only **capabilities** the agents use. The agents, prompts and tool
names are identical on every cloud; only the provider changes (`OPS_CLOUD_PROVIDER`).

| Provider | Key | Status | Code |
|---|---|---|---|
| Google Cloud | `gcp` | Implemented (20 capabilities; service scope below) | [`app/providers/gcp/`](../../app/providers/gcp/) |
| Amazon Web Services | `aws` | Skeleton — every capability returns `not_supported` | [`app/providers/aws/__init__.py`](../../app/providers/aws/__init__.py) |
| Microsoft Azure | `azure` | Skeleton — every capability returns `not_supported` | [`app/providers/azure/__init__.py`](../../app/providers/azure/__init__.py) |

A skeleton provider can still inspect repository posture, CISA KEV, EOL and web threats, and retrieve PRs and CI.
Confirming deployment and error recurrence in a weekly review requires the corresponding cloud capabilities.
Unsupported capabilities return `not_supported`; the agents are instructed to report them as "not checked".
Capabilities can be implemented one at a time — contributions are very welcome.

"Implemented" means that a method exists, not that an audit is complete. See [known limitations](known-limitations.md)
for current audit limitations and the checks an operator must supplement.

## The contract

`app/providers/base.py` defines `CloudProvider`. Subclass it, set `profile`, and override the capabilities you support.

```python
from app.providers.base import CloudProvider, ProviderProfile

PROFILE = ProviderProfile(
    key="aws",
    display_name="Amazon Web Services",
    target_label="account",          # project / account / subscription
    target_label_ja="アカウント",     # used by the (Japanese) prompts
    cli="aws",                       # used in remediation examples
    stack_keywords=("Amazon", "AWS"),
    glossary={"list_services": "ECS services / Lambda functions", ...},  # one entry per capability
)

class AwsProvider(CloudProvider):
    profile = PROFILE

    def list_services(self) -> dict:
        ...  # self.target is the account id, self.region the region
```

Rules:

1. **Read-only.** Never call an API that changes a resource.
2. **Return a dict with `status`.** `ok`, `partial` (some sources/results unavailable), `not_supported`, or one of `api_disabled`, `permission_denied`, `not_found`, `rate_limited`, `error`
   with a short `message` and, where useful, a `hint` telling a human how to fix it (which API to enable, which role is missing).
   Do not raise for API failures — the wrapper turns exceptions into `status: error`, but a clear message is better.
3. **Keep responses small.** Cap lists (the GCP provider returns at most 30 error groups, 200 log entries, 20 deployments),
   truncate long strings, and paginate server-side. Preserve caps and partial failures in results; do not confuse fetched counts with totals.
4. **Redact.** Pass log excerpts and free text through `app.core.redact.redact`. Return environment variable names, never values.
5. **Use the shared shapes.** Audits return `app.core.findings.result(check, findings, summary)` with findings built by
   `app.core.findings.finding(...)`; fingerprints for error groups come from `app.core.fingerprint`.
6. **Configuration** comes from `app.config.CONFIG` (`target`, `region`) plus provider-specific environment variables you
   document here (for example `OPS_AWS_LOG_GROUPS`).
7. **Preserve conditions.** Do not discard IAM conditions, ACL expiry or engine-specific setting names during normalization.
   Keep unevaluated conditions and retrieval failures as unknown. This is guidance for new implementations, not a guarantee
   already applied to every existing GCP audit.

## Capabilities

| Capability | Signature | Used by |
|---|---|---|
| `summarize_error_groups` | `(hours: int)` | error watch |
| `list_error_logs` | `(hours: int, service_name: str, limit: int)` | error watch |
| `get_log_context` | `(service_name: str, around_time: str, window_minutes: int, trace: str)` | error watch |
| `count_matching_errors` | `(service_name: str, search_text: str, since: str)` | error watch, PR review |
| `list_services` | `()` | error watch, infrastructure review |
| `list_deployments` | `(service_name: str, limit: int)` | error watch |
| `list_scheduled_jobs` | `()` | error watch |
| `list_databases` | `()` | error watch, security audit, infrastructure review |
| `get_request_error_rates` | `(hours: int)` | error watch |
| `get_alerting_setup` | `()` | error watch, security audit, infrastructure review |
| `audit_iam` | `()` | security audit, access investigation, infrastructure review |
| `audit_credentials` | `()` | security audit, infrastructure review |
| `audit_compute` | `()` | security audit, infrastructure review |
| `audit_databases` | `()` | security audit, infrastructure review |
| `audit_network` | `()` | security audit, infrastructure review |
| `audit_security_services` | `()` | security audit |
| `list_security_findings` | `(max_findings: int)` | security audit, access investigation |
| `scan_deployed_images` | `()` | security audit |
| `investigate_access` | `(hours: int)` (1–168) | access investigation |
| `list_optimization_recommendations` | `()` | infrastructure review |

### Normalized results

Fields marked **required** are what the prompts and the PR review rely on; add any extra fields that help the model.

**`summarize_error_groups`**

```json
{"status": "ok", "target": "...", "window": {"start": "...Z", "end": "...Z", "hours": 24},
 "total_entries": 175, "truncated": false, "group_count": 4, "by_service": {"worker": 88},
 "groups": [{
   "fingerprint": "15685eff3c9da72b",        // required — fingerprint(resource_type, service, message_template(msg))
   "resource_type": "cloud_run_revision",
   "service": "worker",                      // required
   "template": "http <n> post ...",
   "count": 86, "first_seen": "...", "last_seen": "...",   // required
   "severities": ["ERROR"], "revisions": ["worker-00088"],
   "sample_message": "HTTP 500 POST https://.../api/v1/worker/reconcile",  // required, redacted
   "sample_traces": ["..."],
   "search_text": "/api/v1/worker/reconcile"   // required — search_text_from(url, message)
 }]}
```

Group at most a few thousand entries and return the 30 largest groups. Use `app.core.fingerprint.search_text_from(url, message)`
so that `count_matching_errors` can find the same error again.

**`list_error_logs`, `get_log_context`** — `{"status", "count", "entries": [{"timestamp", "severity", "service", "revision",
"message", "http_status", "trace", "insert_id"}]}` (messages redacted).

**`count_matching_errors`** — `{"status", "since", "count", "capped", "complete", "first_seen", "last_seen", "latest_samples"}`.
Match `search_text` against both the message and the request URL. The GCP implementation rejects empty search text and returns
`complete: false` for page limits or partial failures. Even if the fetched count is zero, only `status: ok` with `complete: true`
can support a "no recurrence" claim. A complete retrieval failure may omit `count`.

**`list_services`** — `{"status", "target", "region", "count", "services": [{"name" (required), "uri", "ready" (required),
"update_time" (required), "images", "service_account", "scaling", "env_plain_names", "env_secret_names", "labels"}]}`.

**`list_deployments`** — `{"status", "service", "count", "revisions": [{"name", "create_time" (required), "images", "ready"}]}`
newest first.

**`list_scheduled_jobs`** — `{"status", "count", "failing_jobs" (required), "jobs": [{"name", "schedule", "time_zone", "state",
"last_attempt_time", "last_status_code", "last_status_message", "failing", "target"}]}`.

**`list_databases`** — `{"status", "count", "instances": [{"name", "state", "engine" (required: an endoflife.date slug such as
`postgresql`, `mysql`, `mssqlserver`), "engine_version" (required: major version), "availability_type", "public_ip_enabled",
"backup_enabled", "pitr_enabled", "deletion_protection", "available_maintenance_versions"}]}`.

**`get_request_error_rates`** — `{"status", "window_hours", "services": [{"service", "requests", "5xx", "4xx", "5xx_ratio",
"4xx_ratio"}]}`.

**`get_alerting_setup`** — `{"status", "alert_policy_count", "alert_policies": [{"display_name", "enabled", "conditions",
"notification_channels"}], "uptime_check_count", "uptime_checks": [...]}`.

**Audits (`audit_*`)** — `app.core.findings.result(...)`:

```json
{"status": "ok", "check": "network", "finding_counts": {"high": 1},
 "findings": [{"check_id": "fw.sensitive_port", "severity": "high", "title": "...", "resource": "default-allow-ssh",
               "detail": "...", "recommendation": "..."}],
 "summary": {...}}
```

`check_id` should be stable: the security audit files issues with fingerprint `"<check_id>:<resource>"`.
Severity guide: `critical` (public data or admin exposure), `high` (exploitable misconfiguration), `medium` (hardening gap),
`low` (best practice), `info` (intentional or informational).

**`list_security_findings`** — `{"status", "count", "counts", "findings": [{"category", "severity", "resource", "event_time",
"explanation", "recommendation"}], "truncated"}`.

**`scan_deployed_images`** — `{"status", "image_count", "images": [{"image", "used_by", "total", "by_severity",
"fix_available", "top_findings": [{"cve", "severity", "package", "version", "fixed_version"}]}]}`.

**`investigate_access`** extends the shared audit shape with `window`, `coverage`, `limitations` and `complete`.
The window is inclusive at `start` and exclusive at `end_exclusive`. Findings include `evidence` with source, resource/subject
references, count, first/last timestamps and signal categories. Stable anonymous references group subjects and resources;
raw IPs, emails, URL queries and arbitrary log payloads are not returned.
Audit, sensitive-path probes, general HTTP 401/403 responses and Cloud Armor each have an independent query budget. Probes allow up to 1,000 entries and general denials up to 30,000; entries shared across searches are counted once. Coverage includes each budget and the cross-search duplicate count.
Per-source `coverage` includes status, pages, fetched/usable/invalid counts and completion. `logging_configuration="unverified"`
separates completed queries from comprehensive log collection. Partial counts are lower bounds; all sources failing produces
`error`, while partial retrieval produces `partial`. Empty results must not be used as a safety verdict.

**`list_optimization_recommendations`** returns `status`, `check`, `complete`, `scope`, `sources`, `recommendations`, `count`
and `limitations`. Source status and completion remain visible; unfinished source results are not mixed into successful recommendations.
Only `ACTIVE` recommendations are included, retaining `name`, `recommender`, `location`, `priority`, `description`,
`last_refresh_time`, `resources`, `recommended_changes`, `impacts` and `exclusive_group`. Proposed operations are never executed.
`impacts[].costProjection` preserves the API currency, `units`, `nanos` and `duration`; missing money or duration is not invented.
Negative cost changes indicate estimated savings. Do not blindly sum differing currencies/durations or mutually exclusive
proposals sharing an `exclusive_group`. `complete=true` applies only to the listed sources and region, not the whole cloud.

## Service mapping

### Google Cloud (implemented)

| Capability | Source |
|---|---|
| logs | Cloud Logging `entries:list` (`severity>=ERROR`), grouped by `resource.labels.*` |
| services / deployments | Cloud Run Admin API v2 services and revisions |
| scheduled jobs | Cloud Scheduler jobs |
| databases | Cloud SQL Admin instances |
| error rates / alerting | Cloud Monitoring `run.googleapis.com/request_count`, alert policies, uptime checks |
| IAM / credentials | Resource Manager `getIamPolicy`, IAM service-account keys |
| compute | Cloud Run services + `getIamPolicy` |
| databases audit | Cloud SQL settings, maintenance versions |
| network | Compute firewalls (+ VM count to calibrate severity) |
| security services | Service Usage enabled APIs |
| security findings | Security Command Center `sources/-/findings` |
| images | Container Analysis occurrences for the digests serving traffic |
| access investigation | Cloud Logging `entries:list`: audit authentication/permission denials, sensitive IAM/key/log operations, HTTP 401/403 and probe paths, Cloud Armor denials |
| optimization | Recommender: Cloud Run Cost (v1) and Cloud SQL Idle / Overprovisioned (v1beta1) in the target region; IAM Policy (v1) in `global` |

See [deployment prerequisites](deployment-gcp.md#access-and-infrastructure-data-prerequisites) for log collection and additional permissions.
Both new capabilities return `not_supported` on AWS/Azure.

An operator must supplement the current GCP audits with these checks:

- **Service-account keys:** Only the first 60 accounts in the list are inspected. Check keys on later accounts separately.
- **Cloud Run exposure:** IAM retrieval failure can leave `public: false`. Independently confirm that policy retrieval succeeded.
- **IAM and Cloud SQL:** Binding conditions and authorized-network expiry are dropped from assessment, so expired settings can be reported as currently active.
- **MySQL IAM authentication:** An enabled `cloudsql_iam_authentication` flag can be reported as disabled. Check the MySQL flag directly.

These limitations remain unfixed. MySQL PITR assessment, firewall defaults and numeric protocols, and scanning images on older
revisions serving traffic have been fixed. GitHub retrieval failures also return unknown rather than proving a setting is disabled.

### AWS (to implement — boto3)

| Capability | Suggested source |
|---|---|
| logs | `logs.start_query` / `get_query_results` (CloudWatch Logs Insights) over `OPS_AWS_LOG_GROUPS`; ALB access logs for HTTP status |
| services | `ecs.list_services` + `describe_services`, `lambda.list_functions`, `apprunner.list_services` |
| deployments | `ecs.describe_services` (`deployments`), `lambda.list_versions_by_function` |
| scheduled jobs | `scheduler.list_schedules`, `events.list_rules` (+ failed invocation metrics) |
| databases | `rds.describe_db_instances` / `describe_db_clusters` (`Engine`, `EngineVersion`) |
| error rates | `cloudwatch.get_metric_data` (`HTTPCode_Target_5XX_Count`, API Gateway `5XXError`) |
| alerting | `cloudwatch.describe_alarms`, `route53.list_health_checks` |
| IAM | `iam.get_account_summary` (root MFA), credential report, `AdministratorAccess` attachments, `accessanalyzer.list_findings` |
| credentials | `iam.list_users` → `list_access_keys` → `get_access_key_last_used` |
| compute | ECS task definitions (plain-text secret-looking env vars), Lambda configuration, public load balancers |
| databases audit | `PubliclyAccessible`, `StorageEncrypted`, `BackupRetentionPeriod`, `DeletionProtection`, `describe_pending_maintenance_actions` |
| network | `ec2.describe_security_groups` (0.0.0.0/0 or ::/0 on 22, 3389, DB ports) |
| security services | `guardduty.list_detectors`, `securityhub.describe_hub`, `inspector2.batch_get_account_status`, Config recorders, CloudTrail trails |
| security findings | `securityhub.get_findings` (`RecordState=ACTIVE`, severity CRITICAL/HIGH/MEDIUM) |
| images | `inspector2.list_findings` for ECR images used by running services |

Target: `OPS_TARGET` or `AWS_ACCOUNT_ID`; region: `OPS_TARGET_REGION`, `AWS_REGION` or `AWS_DEFAULT_REGION`.
Credentials: the boto3 default chain (IAM role on ECS / Lambda, `AWS_PROFILE` locally). A read-only identity can use the
AWS managed policies `ReadOnlyAccess` and `SecurityAudit`.

### Azure (to implement — azure-identity, azure-monitor-query, azure-mgmt-*)

| Capability | Suggested source |
|---|---|
| logs | `LogsQueryClient` (KQL) on `OPS_AZURE_WORKSPACE_ID`: `AppExceptions`, `AppRequests`, `ContainerAppConsoleLogs_CL` |
| services | `azure-mgmt-web` (App Service, Functions), `azure-mgmt-appcontainers` (Container Apps) |
| deployments | Container Apps revisions, App Service deployments |
| scheduled jobs | Container Apps Jobs executions, Logic Apps runs, Functions timer triggers |
| databases | PostgreSQL / MySQL flexible servers, Azure SQL (`version`) |
| error rates | `MetricsQueryClient` (`Http5xx`, `requests/failed`) |
| alerting | `azure-mgmt-monitor` metric and log alert rules, Application Insights availability tests |
| IAM | `azure-mgmt-authorization` role assignments (Owner / Contributor at subscription scope, guest principals) |
| credentials | Microsoft Graph application / service-principal password and key credentials (age, expiry) |
| compute | Public ingress, managed identity, plain-text app settings |
| databases audit | Public network access, firewall rules allowing 0.0.0.0–255.255.255.255, TLS, backup retention |
| network | `azure-mgmt-network` NSG rules (Any / Internet on sensitive ports) |
| security services | `azure-mgmt-security` Defender for Cloud pricing tiers |
| security findings | Defender for Cloud assessments and alerts |
| images | Defender for Containers vulnerability assessments (ACR) |

Target: `OPS_TARGET` or `AZURE_SUBSCRIPTION_ID`; region: `OPS_TARGET_REGION` or `AZURE_LOCATION`.
Credentials: `DefaultAzureCredential` (managed identity in Azure, `az login` locally). Read-only identity: the `Reader` and
`Security Reader` roles plus `Log Analytics Reader` on the workspace.

## Implementing a capability

1. Pick a capability from the tables above and read the GCP implementation of the same method for the expected shape.
2. Add the SDK as an optional dependency (`[project.optional-dependencies] aws = ["boto3>=1.35"]`) and import it inside
   the provider module so other clouds do not need it.
3. Override the method on the provider class. Keep it read-only and return the normalized fields.
4. Remove the "(未対応 / not supported)" marking automatically: `capabilities()` detects overridden methods, and the prompt
   glossary and `get_run_context` update themselves.
5. Add unit tests with stubbed SDK responses (no network in unit tests). The contract tests in `tests/unit/test_providers.py`
   check signatures, profiles and the not-supported behaviour.
6. Try it end to end with `OPS_CLOUD_PROVIDER=aws OPS_DRY_RUN=true uv run python scripts/run_job.py error_watch --hours 24`.
7. Document provider-specific environment variables in this file and in `.env.example`.

## Third-party providers

A package can ship a provider without forking this repository:

```toml
# pyproject.toml of your package
[project.entry-points."cloud_ops_team.providers"]
oci = "my_package.provider:OciProvider"
```

Install it next to cloud-ops-team and set `OPS_CLOUD_PROVIDER=oci`. For tests or embedding, call
`app.providers.register_provider("fake", FakeProvider)`.
