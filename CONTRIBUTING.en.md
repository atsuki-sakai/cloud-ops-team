# Contributing

日本語版（メイン）: [CONTRIBUTING.md](CONTRIBUTING.md)

Thanks for helping! Bug reports, docs fixes, new checks and — most of all — **AWS and Azure capabilities** are welcome.

## Development setup

```bash
uv tool install google-agents-cli
git clone https://github.com/atsuki-sakai/cloud-ops-team && cd cloud-ops-team
make setup                    # uv sync + create .env (keeps OPS_DRY_RUN=true)
make doctor                   # check model, GitHub and cloud access
```

Unit tests and lint need no cloud credentials:

```bash
make test                     # uv run pytest tests/unit — fast, no network
make lint                     # ruff, ruff format, codespell, ty
```

With Gemini and cloud access you can also run:

```bash
make run JOB=error_watch                 # one job, every tool call printed
uv run pytest tests/integration          # calls the model
make eval                                # behaviour eval (LLM-as-judge)
```

## What goes where

| Change | Where | Read first |
|---|---|---|
| Support a cloud capability | `app/providers/<cloud>/` | [docs/en/providers.md](docs/en/providers.md) |
| New specialist or job | `app/specialists/` (copy `_template.py`) | [docs/en/customization.md](docs/en/customization.md) |
| New agent-facing tool | `app/tools/` + the specialist's `tools` | [docs/en/customization.md §5](docs/en/customization.md#5-add-a-tool) |
| Agent behaviour | `INSTRUCTION` in `app/specialists/<name>.py`, shared parts in `app/prompts.py` | the eval cases in `tests/eval/` |
| Deployment | `scripts/<cloud>/` and `docs/deployment-<cloud>.md` + `docs/en/deployment-<cloud>.md` | [docs/en/deployment-gcp.md](docs/en/deployment-gcp.md) |

The [known limitations and improvement plan](docs/en/known-limitations.md) records confirmed unresolved items and proposed
responsibility splits and shared controls. When fixing an item, add regression coverage for its trigger and update both
languages of the limitation and affected guides to match the implementation.

## Rules of the road

- **Read-only towards clouds.** A tool or provider must never change cloud resources. PRs that add mutating calls will be declined.
- **Respect dry-run.** Anything that writes to GitHub must honour `CONFIG.dry_run`.
- **Tool conventions** (enforced by `tests/unit/test_providers.py`): type hints on every parameter, no default values,
  a docstring written for the model, a JSON-serializable `dict` with a `status` key, and no exceptions for API errors.
- **No secrets or personal data** in tool results: redact free text with `app.core.redact.redact`, return environment variable
  names only.
- **Do not change the fingerprint formula** (`app/core/fingerprint.py`) — it would re-file every open issue. The golden tests
  will fail if you do.
- **Unit tests stay offline.** Stub SDK responses; integration and eval tests may call the model.
- **Prompt changes need evidence.** Run `agents-cli eval run` before and after and include the scores in the PR
  (`agents-cli eval compare old.json new.json`).

## Pull requests

1. Open an issue first for larger changes (a new provider, a new specialist) so we can agree on the shape.
2. Keep PRs focused; update docs and the changelog (Unreleased) in the same PR. Documentation is Japanese-first:
   `docs/*.md`, `README.md`, `CHANGELOG.md` are Japanese and `docs/en/*.md`, `README.en.md`, `CHANGELOG.en.md` are English.
   Update both languages (if you can only write one, say so in the PR and a maintainer will translate).
3. CI must be green (lint, types, unit tests).
4. Describe how you tested it — for providers, which capabilities you ran against a real account in dry-run mode.

Distinguish intended behavior, current implementation and unresolved limitations, and keep README summaries consistent with
the guides. For documentation-only changes, run `uv run --extra lint codespell`, check relative links and heading anchors,
and run `git diff --check`. Record the checks performed; do not present unit, model or deployment validation as completed
if it was not run.

By contributing you agree that your contributions are licensed under the Apache License 2.0 and that you follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
