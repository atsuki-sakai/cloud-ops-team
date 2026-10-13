## What and why

<!-- Link the issue: closes #123 -->

## How it was tested

- [ ] `uv run pytest tests/unit`
- [ ] `agents-cli lint`
- [ ] Ran a job in dry-run mode (`scripts/run_job.py ...`) — paste the relevant tool calls or report excerpt
- [ ] Prompt changes: `agents-cli eval run` before / after (scores below)

## Checklist

- [ ] Read-only towards the cloud; GitHub writes honour `OPS_DRY_RUN`
- [ ] No secrets or personal data in tool results (redacted, env var names only)
- [ ] Docs and `CHANGELOG.md` (Unreleased) updated
