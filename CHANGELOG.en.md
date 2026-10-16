# Changelog

日本語版（メイン）: [CHANGELOG.md](CHANGELOG.md)

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- Bound model HTTP idle waits to 60 seconds and use HTTPX so SDK timeout retries apply. Whole agent runs and streams that have begun returning a response are not retried.
- Redact secret values in JSON/structured data and issue/report titles and metadata. Sensitive or masked search literals cannot establish zero recurring errors.
- Weekly fix review now includes unverified issues closed within 30 days, including issues automatically closed when their fixing PR was merged.
- Invalid dry-run values enabling GitHub writes, and secret backups being included in Git or deployment uploads.
- Unredacted log grouping fields and unfinished searches being reported as zero recurring errors.
- Issue target isolation, pagination beyond 100 issues, and closure after verification comment or label failures.
- Firewall defaults and numeric protocols, scanning every serving revision, and MySQL PITR detection.
- Inaccessible GitHub settings being reported as insecure, missing raw-file version information, and malformed EOL responses.
- Deployment label values conflicting with argument delimiters; added offline regression tests for these cases.

### Changed
- Scheduled jobs now use strict JSON validation and fixed collection; natural-language requests enter the same path through job-specific adapters. Analysis models receive only their stage's tools, descriptions and invocation history.
- Execution validates periods, result limits, targets and repositories, retaining missing or truncated collection results as per-run evidence. Existing collector, model and fingerprint contracts are preserved.
- Aligned the READMEs, architecture, configuration, provider and deployment guides with per-invocation issue budgets,
  duplicate controls, audit coverage and result storage. Documented deployment gaps for custom context paths and A2A URLs.
  Documentation of remaining limitations does not implement their proposed fixes.
- Documentation is now **Japanese-first**: `README.md`, `docs/*.md`, `CONTRIBUTING.md` and `CHANGELOG.md` are Japanese;
  the English versions are `README.en.md`, `docs/en/*.md`, `CONTRIBUTING.en.md` and `CHANGELOG.en.md`.
  `SECURITY.md` and the issue and PR templates are bilingual, and `CODE_OF_CONDUCT.md` has a Japanese summary.

### Added
- A comparison CLI and cases for routing, tools, arguments, preconditions and task completion against the previous architecture. Missing evidence and unauthorized tool presentations or calls fail the release gate.
- Daily `access_watch` at 08:30 with a 24-hour window: audit logs, HTTP authentication denials/probe paths and Cloud Armor denials, reported with evidence, confidence and coverage gaps.
- Weekly `infrastructure_review` on Mondays at 10:00: GCP Recommender cost/IAM recommendations and existing configuration audits, with impact, risks, implementation, validation and rollback proposals. No automatic cloud changes.
- Cloud-neutral `investigate_access(hours)` / `list_optimization_recommendations()` capabilities, schedule settings and deployment guidance for logging coverage and additional read roles. AWS/Azure return `not_supported` for both capabilities.
- [Known limitations and improvement plan](docs/en/known-limitations.md) in both languages, recording ten unresolved defects,
  the resolved session-counter defect (A03), two conditional risks, interim measures and required validation.
- Japanese versions of the configuration reference, the Google Cloud deployment guide and the provider guide;
  the Japanese architecture document now covers everything the English one does.

## [0.3.0] - 2026-10-14

### Added
- **Use it for your own project**: `make setup` (interactive `.env`), `make doctor` (checks configuration, Gemini, GitHub and
  every cloud capability with the identity in use, and prints a fix for each gap), `make run`, `make jobs`, `make chat` and
  `make gcp-bootstrap / gcp-grant / gcp-deploy / gcp-schedule / gcp-logs` that read `.env`.
- **Specialist registry** (`app/specialists/`): one module per specialist with its instruction, tools and jobs, plus
  `_template.py`. The leader's team list and routing table and the Cloud Scheduler jobs are generated from it.
- **Project context**: `config/ops-context.md` (template included) is appended to every instruction.
- Configurable issue labels (`OPS_ERROR_LABELS`, `OPS_SECURITY_LABELS`, `OPS_SEVERITY_LABELS`).
- Scheduler trigger authentication: `OPS_TRIGGER_AUDIENCE` / `OPS_TRIGGER_SERVICE_ACCOUNTS`, configured automatically by
  `make gcp-deploy` (ADK 2.12 requires trigger authentication to be configured).
- Customization guide (English and Japanese) and folder-structure documentation.

### Changed
- Default severity labels are now `severity: critical|high|medium|low` and security issues get `security` only.
  To keep the 0.2 labels set `OPS_SEVERITY_LABELS=critical=priority: critical,high=priority: high,medium=major,low=minor`
  and `OPS_SECURITY_LABELS=security,infrastructure`.
- Scheduler jobs come from the specialists' job definitions (`scripts/jobs.py`); `setup_schedules.sh` finds the service URL
  itself and accepts `<SCHEDULE_KEY>_CRON` for new jobs.
- GCP scripts fall back to `.env` names (`GOOGLE_CLOUD_PROJECT`, `OPS_TARGET`, `OPS_TARGET_REGION`); the deploy script checks
  that the GitHub token secret exists.
- Dependencies: google-adk 2.12, google-genai 2.30, opentelemetry-resourcedetector-gcp 1.14, and the `eval` extra now
  allows google-cloud-aiplatform 2.x (locked at 2.4). CI uses actions/checkout v7 and astral-sh/setup-uv v7.

## [0.2.0] - 2026-10-13

### Changed
- Renamed the project from `gcp-ops-team` to **cloud-ops-team** (root agent `cloud_ops_team`).
- Introduced a **provider architecture**: the agents use 18 cloud-neutral capability tools (`list_services`, `audit_network`,
  `list_security_findings`, ...) that delegate to the provider selected with `OPS_CLOUD_PROVIDER`.
  Google Cloud code moved to `app/providers/gcp/`.
- Prompts are cloud-neutral and rendered from the provider profile (display name, target type, CLI, glossary).
- The error watch runs **daily at 08:00 with a 24-hour window** by default; every schedule is configurable
  (`ERROR_WATCH_CRON`, `ERROR_WATCH_HOURS`, `WEEKLY_CRON`, `MONTHLY_CRON`, `ONLY_JOBS`, `JOB_PREFIX`, ...).
- Deployment scripts moved to `scripts/gcp/` with English documentation; `deploy_cloud_run.sh` uses generic `OPS_TARGET` variables.
- De-duplication is scoped to the target recorded in each issue, so several environments can share one repository
  (issues written by 0.1.0 remain recognised).

### Added
- AWS and Azure provider skeletons with service mappings; unimplemented capabilities return `not_supported` and are reported as
  "not checked".
- Third-party providers through the `cloud_ops_team.providers` entry point; `register_provider` for tests.
- `get_run_context` reports the provider's implemented and unsupported capabilities.
- `OPS_REPORT_LANGUAGE` (`ja` / `en`) for reports, issues and comments; `OPS_MODEL` and `OPS_RESEARCH_MODEL`.
- `scripts/gcp/bootstrap.sh` for one-time setup.
- Provider contract tests, golden fingerprint tests and tool-convention tests; GitHub Actions CI.
- Docs: architecture (English and Japanese), providers, configuration, deployment on Google Cloud; contributing guide,
  security policy, code of conduct, issue and PR templates.

### Fixed
- `setup_schedules.sh` failed to update existing jobs (`--update-headers` is required on update).

## [0.1.0] - 2026-10-13

### Added
- First public release as `gcp-ops-team`: error watch, monthly security audit and weekly PR review on Google Cloud
  (Cloud Logging, Cloud Run, Cloud Scheduler, Cloud SQL, Cloud Monitoring, IAM, SCC, Artifact Analysis), GitHub issue filing with
  fingerprint de-duplication, dry-run mode, redaction, Cloud Run deployment and Cloud Scheduler wiring.

[Unreleased]: https://github.com/atsuki-sakai/cloud-ops-team/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/atsuki-sakai/cloud-ops-team/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/atsuki-sakai/cloud-ops-team/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/atsuki-sakai/cloud-ops-team/releases/tag/v0.1.0
