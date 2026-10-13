# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/atsuki-sakai/cloud-ops-team/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/atsuki-sakai/cloud-ops-team/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/atsuki-sakai/cloud-ops-team/releases/tag/v0.1.0
