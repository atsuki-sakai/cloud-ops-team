# Customization guide

cloud-ops-team is meant to be cloned and **grown into your own operations team**. This page lists the common changes as
"what you want → where to change it → example".

日本語（メイン）: [docs/customization.md](../customization.md) · How it works: [architecture.md](architecture.md) ·
Folder structure: [architecture.md#folder-structure](architecture.md#folder-structure)

Issue control, audits and deployment have [known limitations](known-limitations.md). Editing instructions does not add
code-enforced guarantees for cross-instance de-duplication or fix verification.

| You want to | Change | Code? |
|---|---|---|
| Tell the team what your services do and which errors are noise | `config/ops-context.md` | No |
| Change when / how often jobs run | `.env` or variables of `make gcp-schedule` | No |
| Use your repository's labels | `.env` (`OPS_*_LABELS`) | No |
| Get reports in English | `.env` (`OPS_REPORT_LANGUAGE=en`) | No |
| Change decisions/reporting | Specialist instruction + analysis steps in `app/prompts.py` | Mostly text |
| Add an investigation function | Tools/provider + fixed collection or analysis scope + runtime checks | Yes |
| Add a job | Job definition + request schema + fixed collection + analysis scope + comparison cases | Yes |
| Add a specialist | Definition, schema, fixed workflow, scoped analysis and comparison cases | Yes |
| Implement AWS / Azure capabilities | `app/providers/aws/`, `azure/` | Yes ([providers.md](providers.md)) |

Use fixed collection for routine fetching and expose only necessary follow-up tools to analysis. Do not append a new tool to every specialist. After changes, run `make test`, `make lint` and baseline/candidate comparisons under identical conditions. Use `OPS_DRY_RUN=true` for live data trials. Offline contracts, live-model accuracy and production validation are distinct evidence; see the [comparison guide](../../tests/eval/routing/README.md).

## 1. Teach the team about your project (no code)

```bash
cp config/ops-context.example.md config/ops-context.md
```

Describe your services and which ones matter most, your severity policy ("anything touching payments is high"), known
noise that should not become issues, and what you want in recommended fixes. The file is appended to every agent's
instructions (up to 8,000 characters) and never overrides the safety rules. It ships in the container image, so **no secrets**.
Use `OPS_CONTEXT_FILE` to point elsewhere.
The standard deploy script does not forward that variable. On Cloud Run, use the default `config/ops-context.md` or include
the custom file in the image and apply the [post-deploy settings](deployment-gcp.md).
An arbitrary local file is not automatically included in the image, and those settings must be reapplied after redeployment.

For access investigation, describe approved security tests, planned IAM changes and log collection/exclusion/retention.
For infrastructure review, describe peak periods, availability targets, standby resources and work that must retain its access.

## 2. Change schedules (no code)

Schedules are generated from each specialist's jobs; environment variables override them.

```bash
make jobs                                                              # current jobs and schedules
ERROR_WATCH_CRON="0 */6 * * *" ERROR_WATCH_HOURS=6 make gcp-schedule   # every 6 hours
```

Built-in overrides: `ERROR_WATCH_CRON` / `ERROR_WATCH_HOURS`, `ACCESS_WATCH_CRON` / `ACCESS_WATCH_HOURS`,
`INFRASTRUCTURE_REVIEW_CRON`, `WEEKLY_CRON` / `WEEKLY_DAYS`, `MONTHLY_CRON` / `MONTHLY_KEV_DAYS`.
Access review defaults to daily at 08:30 with a 24-hour window; infrastructure review defaults to Mondays at 10:00 (Asia/Tokyo).
Jobs you add get `<SCHEDULE_KEY>_CRON` (e.g. `DEPLOY_WATCH_CRON`). `ONLY_JOBS=error-watch` limits an update to some jobs.

## 3. Match your repository's labels (no code)

Every issue gets `ops-agent` and `ops:error` / `ops:security` / `ops:report`. Everything else is configurable (empty = none):

```bash
OPS_ERROR_LABELS=bug,backend
OPS_SECURITY_LABELS=security,infrastructure
OPS_SEVERITY_LABELS=critical=priority: critical,high=priority: high,medium=priority: medium,low=priority: low
```

Use `high=priority: high|urgent` for several labels per severity. Missing labels are created on first use.

## 4. Change a specialist's procedure

Each specialist's instruction is plain text in `app/specialists/<name>.py` (`error_watch.py`, `security_audit.py`,
`pr_review.py`, `access_watch.py`, `infrastructure_review.py`). Analysis removes the original collection procedure and inserts `_ANALYSIS_STEPS` from `app/prompts.py`, so keep decision rules consistent in both locations. Fixed collection order, arguments and targets are changed in `app/workflows.py`, not by adding prompt instructions.
Keep evidence separate from hypotheses: denied requests and sensitive operations alone do not prove compromise.
Infrastructure review must not invent savings or apply changes. Reuse `check_id:resource` and `kind="security"` for the same
audit findings as the monthly review to avoid duplicate issues; include change risks, validation and rollback proposals.
Placeholders: `__PROVIDER__`, `__TARGET__`, `__TARGET_LABEL__`, `__REGION__`, `__REPO__`, `__LOOKBACK__`, `__STACK__`, `__CLI__`,
`__LANG__`. Only the exposed stage capabilities appear in the glossary/context. Compare execution records to verify that collection is not repeated and missing permissions/partial evidence/rejections remain visible in the final report.
If you change the verification criteria, evaluate the PR repository and fix relationship, CI SHA, deployed revision and log
coverage. Successful data retrieval and confirmation that an incident is resolved are separate conditions.

## 5. Add a tool

A tool is a Python function; its docstring is what the model reads.

```python
# app/tools/certificates.py
import datetime as dt
import socket
import ssl


def check_tls_certificate(hostname: str) -> dict:
    """Check the TLS certificate served by ``hostname`` and how many days remain.

    Args:
        hostname: Public host name, e.g. ``api.example.com``.
    """
    try:
        context = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as tls:
                cert = tls.getpeercert()
    except OSError as exc:
        return {"status": "error", "hostname": hostname, "message": str(exc)}
    expires = dt.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(
        tzinfo=dt.UTC
    )
    return {
        "status": "ok",
        "hostname": hostname,
        "days_left": (expires - dt.datetime.now(dt.UTC)).days,
    }
```

This is an extension example, not an implemented certificate-monitoring feature. Validate the hostname against an explicit allowlist; never accept arbitrary destinations from model output or evidence. Add the function to the specialist's tool superset and import it in `app/tools/__init__.py`, then register a fixed `CollectionStep("check_tls_certificate", {"hostname": "api.example.com"})` in `app/workflows.py`. The collector is removed from analysis tools; the model reads its evidence and applies the reporting threshold. Enforce argument, destination and query budgets in `ToolScope` or the function and test direct-call rejection.

Rules, enforced by `make test`: type hints on every parameter and **no default values**; a docstring for the
model; return a dict with `status` and never raise for API errors; read-only; GitHub writes honour `CONFIG.dry_run`; no
secrets in results. Cloud-neutral tools live in `app/tools/`; cloud-specific fetching belongs in `app/providers/<cloud>/`.

New fetching and audit tools should preserve page limits, partial failures, expiry and conditions so unchecked items remain
visible. Do not treat `status: ok` or empty findings alone as evidence that every target was checked.
The current GCP audits still have limitations here; see the [provider guide](providers.md).

## 6. Add a job to an existing specialist

A `Job` declaration alone does not authorize execution. For a weekly error review, add all three contracts together:

```python
# app/specialists/error_watch.py: illustrative addition to jobs
Job(
    name="weekly_error_review",
    params={"hours": "data.hours if given, else 168"},
    scheduled_params={"hours": 168},
    schedule="0 10 * * 1",
    schedule_key="weekly-error-review",
)
```

```python
# app/routing.py: illustrative entry in JOB_PARAMETER_BOUNDS
"weekly_error_review": {"hours": (1, 168)},
```

```python
# app/workflows.py: illustrative addition to collection_steps
if job_name == "weekly_error_review":
    return collection_steps("error_watch", params)
```

This deliberately reuses the existing specialist, collection and analysis rules. Keep `Job.params`, defaults and schema consistent; unknown input must not silently fall back to another job. Add success, alias, similar-intent, invalid-type/range and unknown-field cases in `tests/eval/routing/cases.json`, then compare baseline and candidate on that exact updated suite. Validate schedules/offline tests and complete comparable live-model and dry-run trials before deployment.

## 7. Add a specialist

Copy `app/specialists/_template.py` and register it in `SPECIALISTS`, then complete:

1. **Request contract:** canonical job/aliases, integer parameters, defaults and `JOB_PARAMETER_BOUNDS`.
2. **Fixed collection:** ordered `collection_steps`, explicit functions/arguments, imports in `app/tools/__init__.py` and the specialist tool superset.
3. **Analysis scope:** `get_collected_evidence` plus the minimum follow-up/reporting subset after collectors are removed.
4. **Decision/report requirements:** evidence-reading instructions, gaps, duplicate handling and dry-run semantics.
5. **Execution boundary and evaluation:** offline rejection tests for unauthorized tools, foreign scope, excessive queries and partial evidence, plus baseline/candidate confusion cases.

Schedules and conversation candidates derive from definitions, but undefined request schemas/workflows fail closed. Do not grow every tool list. Browser recording/replay and post-deploy verification specialists remain in [Issue #7](https://github.com/atsuki-sakai/cloud-ops-team/issues/7).

## 8. Implement AWS / Azure capabilities

See [providers.md](providers.md): normalized result fields, suggested APIs per capability and testing. Unimplemented
capabilities report `not_supported`, so you can add them one at a time; `make doctor` shows what is implemented.

## 9. Change the model

`OPS_MODEL` (default `gemini-3.8-flash`) for all agents, `OPS_RESEARCH_MODEL` for web research (needs Google Search grounding).
For non-Gemini models, wrap them with ADK's `LiteLlm` in `_model()` in `app/agent.py`.

## Development loop

| Task | Command |
|---|---|
| Check configuration and permissions | `make doctor` (`ARGS=--quick` skips audits) |
| Run one job and see every tool call | `make run JOB=<job> ARGS="--hours 24"` |
| Chat with the team | `make chat` |
| List jobs and schedules | `make jobs` |
| Offline tests / static checks | `make test` / `make lint` |
| Report/content evaluation | `make eval` (`tests/eval/datasets/`) |
| Routing/tool baseline comparison | `scripts/evaluate_routing.py` ([format and usage](../../tests/eval/routing/README.md)) |

`permission_denied` / `api_disabled` → `make doctor` explains the fix. Unexpected behaviour → read the tool calls printed by
`make run` and adjust the instruction. `agents-cli run` stops after 120 s of silence → use `make run`. Gemini 429s are retried
five times with backoff. Ship changes with `make gcp-deploy`, starting with `OPS_DRY_RUN=true`.

Comparisons require at least three independent samples per case, matching conditions, complete traces and zero unauthorized presentation/attempt/execution. Passing `runtime_stubbed` does not satisfy release eligibility; live-model comparison and report-content verification remain necessary.
