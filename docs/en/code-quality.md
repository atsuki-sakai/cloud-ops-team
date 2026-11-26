# Code quality gate (Python)

日本語版（メイン）: [docs/code-quality.md](../code-quality.md)

Python in this repository is checked against the same rules no matter who writes it, a person or an AI. Rules live in exactly two places:

| Location | Contents |
|---|---|
| `pyproject.toml` `[tool.ruff.lint]` / `[tool.ty]` | ruff rule families, per-path exceptions, ty strictness |
| `sgconfig.yml` and `lint/ast-grep/rules/` | Project-specific ast-grep rules (exception handling, N+1, cloud neutrality, suppression reasons) |

CI, `make lint` and the Claude Code hooks all read these two places, so none of them is stricter than the others.

## When checks run

| When | Scope | What happens |
|---|---|---|
| Right after Claude Code runs Edit / Write / MultiEdit (PostToolUse) | The one edited `.py` file | ruff, ruff format, ty and ast-grep run in parallel. Errors exit 2 and send the reason and the fix back to the agent, which must fix them. Warnings are passed back without blocking |
| Before Claude Code finishes (Stop) | Every `.py` in `git diff` plus untracked files | The same checks again. Remaining errors stop the agent from finishing, at most twice; after that it may finish and the remaining errors are shown on screen |
| `make check-py [FILES=...]` | Given files, or changed files | The hook's checks, run by hand |
| `make lint` and CI | Whole repository | ruff, ruff format, codespell, ty, `ast-grep scan`, `ast-grep test` |

The hooks are configured in `.claude/settings.json`; the implementation is `scripts/quality/hook.py`. It uses only the standard library and calls the tools in `.venv/bin` directly (`uv run` is too slow to start). Checking one file takes about 0.2 s locally.

The rules the agent reads before writing code are in `.claude/skills/python-quality/SKILL.md`. A test checks that the Skill's rule index and `lint/ast-grep/rules/` agree.

## Main rules

- **Types**: in `app/` every ty rule is an error, and arguments and return values must be annotated (`ANN`). `tests/` and `scripts/` handle loose JSON and fakes, so they use a looser profile.
- **Exceptions**: only boundaries (tools, providers, transport, router) may catch `Exception`.
  - Mark the catch with `# noqa: BLE001 -- reason` and return `tool_error(exc, ...)` from `app/core/errors.py`.
  - `tool_error` returns `status` / `error_type` / `message`. The message is redacted with `redact` and capped at 300 characters.
  - Everywhere else, catch specific exceptions and re-raise with `raise ... from exc`.
- **Security**: the bandit-equivalent `S` family is on. These are rejected:
  - `assert` used for validation
  - `shell=True`
  - `requests`
  - cloud SDK imports in `app/tools/`
- **Slow code**: `PERF` and `ASYNC` are on. An external API call inside a loop raises `api-call-in-loop`, which is a warning. Suppress it only when the loop is bounded and no batch API exists, and state the bound in the reason.
- **N+1 runtime tests**: a syntax-only rule misses calls made through helpers. `tests/unit/test_api_call_counts.py` therefore checks that the number of API calls does not grow with the input, counting calls with the `api_calls` fixture in `tests/unit/conftest.py`.
- **Suppressions**: only `# noqa: CODE -- reason`, `# ast-grep-ignore: rule-id -- reason` and `# ty: ignore[rule] -- reason` are accepted. A suppression without a reason, or a file-wide `# ruff: noqa`, is an error.

See `per-file-ignores` in `pyproject.toml` for per-path exceptions. `app/core/fingerprint.py` is exempt from S324 and FURB167 (regex flag rewrites) only, because its formula must not change.

## Disabling or softening

Set the `CLOUD_OPS_PYCHECK` environment variable:

- `enforce` (default): errors send the edit back
- `warn`: report findings, never block
- `off`: skip all checks

Claude Code reads hook settings when a session starts, so a change applies from the next session (or after reviewing it in `/hooks`).

## When the hook misbehaves

- A missing tool, a timeout, or a bug in the hook never blocks the edit. It shows `[py-quality] SETUP: ...` on screen instead; `uv sync --extra lint` usually fixes it.
- Each run appends a line to `.claude/logs/py-quality.jsonl` (gitignored). The line records the file, the rules, the counts and the time per tool.
- To try the checks by hand: `make check-py FILES=app/tools/cloud.py`

## Adding a rule or upgrading a tool

1. Write ast-grep rules in `lint/ast-grep/rules/<id>.yml`. Always fill in `message` (what is wrong) and `note` (how to fix it; the hook returns it verbatim).
2. Add valid and invalid cases to `lint/ast-grep/rule-tests/<id>-test.yml`, then run `uv run ast-grep test --skip-snapshot-tests`.
3. Add `` `ast-grep:<id>` `` to the Skill's rule index. If you skip this, `tests/unit/test_code_quality.py` fails.
4. ruff, ty and ast-grep are pinned in `pyproject.toml`, and a new version changes which code is rejected. Upgrade them only in a PR that also fixes the existing code.
