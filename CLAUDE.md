# Coding Agent Guide

## This project: cloud-ops-team

- Runtime: `app/agent.py` exports `cloud_ops_team`, a programmatic `ScheduledRouter` shared by HTTP/SSE, Pub/Sub-shaped and A2A requests. Registered JSON jobs bypass LLM routing; only natural-language requests enter `conversation_leader` and its five job adapters. Each adapter validates strict job JSON and uses the same fixed workflow.
- Collection and analysis: `app/workflows.py` collects registered evidence once, then removes those collectors from the analysis tool list and adds `get_collected_evidence`. `app/tool_scope.py` enforces stage-specific names/arguments in callbacks and callable wrappers; `app/prompts.py` limits glossary/context and replaces collection instructions with analysis steps. Never expose the entire registry to a model.
- Specialists are defined in `app/specialists/` and registered in `SPECIALISTS`. Adding a specialist or job also requires its `JOB_PARAMETER_BOUNDS` contract, fixed `collection_steps`, analysis scope/instructions and comparison cases. Undefined contracts fail closed. Recipes: `docs/customization.md`.
- Agent-facing tools live in `app/tools/` and must stay cloud-neutral; cloud specifics belong in `app/providers/<cloud>/`
  behind the `CloudProvider` contract (`app/providers/base.py`). Read `docs/architecture.md` and `docs/providers.md` first.
- Never add a tool that changes cloud resources. GitHub writes must honour `CONFIG.dry_run`.
- Do not change `app/core/fingerprint.py` formulas (golden tests protect open issues from being re-filed).
- Documentation is Japanese-first: `README.md`, `docs/*.md`, `CONTRIBUTING.md`, `CHANGELOG.md` are Japanese and
  `README.en.md`, `docs/en/*.md`, `CONTRIBUTING.en.md`, `CHANGELOG.en.md` are English. Update both languages together
  (`SECURITY.md` and `.github/` templates are bilingual in one file).
- Fast checks: `make test` and `make lint`. Run a job locally: `make run JOB=<job>` (`agents-cli run` has a fixed 120 s
  read timeout). Check access: `make doctor`.

## Prerequisites

Install the CLI (one-time):
```bash
uv tool install google-agents-cli
```

---

## Development Phases

### Phase 1: Understand Requirements
Before writing any code, understand the project's requirements, constraints, and success criteria.

### Phase 2: Build and Implement
Implement agent logic in `app/`. Use `agents-cli playground` for interactive testing. Iterate based on user feedback.

### Phase 3: The Evaluation Loop (Main Iteration Phase)
Start with 1-2 eval cases, run `agents-cli eval run`, iterate by making changes and rerunning it until satisfied. Expect 5-10+ iterations. Once you have a baseline, reach for `agents-cli eval compare` (regression diffs), `agents-cli eval analyze` (cluster failure modes), and `agents-cli eval optimize` (auto-tune prompts). See the **Evaluation Guide** for metrics, dataset schema, LLM-as-judge config, and common gotchas.

For routing/tool/prompt changes, additionally record baseline and candidate executions on the same suite and conditions, then run `scripts/evaluate_routing.py` following `tests/eval/routing/README.md`. At least three independent samples per case, complete traces and zero unauthorized presentation/attempt/execution are required. `runtime_stubbed` validates contracts but is never release-eligible; only comparable live-model evidence can satisfy this gate. Do not fabricate traces from expected answers or call a smoke test statistical accuracy evidence.

### Phase 4: Pre-Deployment Tests
Run `uv run pytest tests/unit tests/integration`. Fix issues until all tests pass.

### Phase 5: Deploy to Dev
**Requires explicit human approval.** Run `agents-cli deploy` only after user confirms. See the **Deployment Guide** for details.

### Phase 6: Production Deployment
Ask the user: Option A (simple single-project) or Option B (full CI/CD pipeline with `agents-cli infra cicd`).

## Development Commands

| Command | Purpose |
|---------|---------|
| `agents-cli playground` | Interactive local testing |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests |
| `agents-cli eval dataset synthesize` | Synthesize multi-turn eval scenarios for your agent |
| `agents-cli eval run` | Run the agent over the eval dataset and grade the traces |
| `agents-cli eval generate` / `agents-cli eval grade` | Decoupled form: produce traces, then grade them |
| `agents-cli eval compare` | Compare two grade-results files (regression check) |
| `agents-cli eval analyze` | Cluster failure modes from grade results |
| `agents-cli eval metric list` | List built-in metrics available in the SDK |
| `agents-cli eval optimize` | Auto-tune agent prompts using eval data |
| `agents-cli lint` | Check code quality |
| `agents-cli infra single-project` | Set up project infrastructure (Terraform) |
| `agents-cli deploy` | Deploy to dev |
| `agents-cli scaffold enhance` | Add deployment target or CI/CD to project |
| `agents-cli scaffold upgrade` | Upgrade project to latest version |

---

## Operational Guidelines for Coding Agents

- **Code preservation**: Only modify code directly targeted by the user's request. Preserve all surrounding code, config values (e.g., `model`), comments, and formatting.
- **NEVER change the model** unless explicitly asked.
- **Preserve stage boundaries**: change routine collection in `workflows.py`, decision/report requirements in the specialist and analysis prompt, and argument limits in routing/tool scope. Do not route unknown structured input through a model fallback or add shell/SQL escape hatches.
- **Feature status**: browser recording/replay, backup creation/restore and the other Issue #7 extensions are planned work. The scoped architecture and offline tests do not establish those features or a production deployment.
- **Model 404 errors**: Fix `GOOGLE_CLOUD_LOCATION` (e.g., `global` instead of `asia-northeast1`), not the model name.
- **ADK tool imports**: Import the tool instance, not the module: `from google.adk.tools.load_web_page import load_web_page`
- **Run Python with `uv`**: `uv run python script.py`. Run `agents-cli install` first.
- **Stop on repeated errors**: If the same error appears 3+ times, fix the root cause instead of retrying.
- **Terraform conflicts** (Error 409): Use `terraform import` instead of retrying creation.
