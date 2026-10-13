# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately through
[GitHub private vulnerability reporting](https://github.com/atsuki-sakai/cloud-ops-team/security/advisories/new).
You will get an acknowledgement within a few days; fixes are released as patch versions and credited in the advisory unless you
prefer otherwise.

## Scope

In scope:

- Ways to make the agents change cloud resources or bypass `OPS_DRY_RUN`, the issue cap or the auto-close setting
- Leaks of secrets, tokens or personal data into issues, comments, reports, logs or traces (gaps in `app/core/redact.py`)
- Prompt injection through log messages, issue or PR content that leads to unintended writes
- Authentication weaknesses in the deployment scripts (for example a service that anyone can invoke)

Out of scope: findings in third-party services (report them to the vendor) and the security posture of the environments the
agents monitor — that is what the agents themselves report.

## Hardening checklist for operators

- Run the agents with **read-only** roles on the monitored cloud (`scripts/gcp/grant_target_roles.sh`)
- Use a **fine-grained GitHub token** limited to the target repository (Issues: write; Pull requests, Actions, Contents,
  Metadata: read) and store it in a secret manager
- Keep the service **private** (no unauthenticated invocation) and call it with a dedicated scheduler identity
- Start with `OPS_DRY_RUN=true` and keep `OPS_AUTO_CLOSE_ISSUES=false` unless you need it
- Treat issues created by the agents as untrusted input when your own AI coding tools act on them
