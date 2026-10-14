# Customization guide

cloud-ops-team is meant to be cloned and **grown into your own operations team**. This page lists the common changes as
"what you want → where to change it → example".

日本語（メイン）: [docs/customization.md](../customization.md) · How it works: [architecture.md](architecture.md) ·
Folder structure: [architecture.md#folder-structure](architecture.md#folder-structure)

| You want to | Change | Code? |
|---|---|---|
| Tell the team what your services do and which errors are noise | `config/ops-context.md` | No |
| Change when / how often jobs run | `.env` or variables of `make gcp-schedule` | No |
| Use your repository's labels | `.env` (`OPS_*_LABELS`) | No |
| Get reports in English | `.env` (`OPS_REPORT_LANGUAGE=en`) | No |
| Change a specialist's procedure, thresholds or issue format | `INSTRUCTION` in `app/specialists/<name>.py` | Text only |
| Add an investigation tool | `app/tools/` + the specialist's `tools` | Yes |
| Add a job to an existing specialist | `jobs` in `app/specialists/<name>.py` | A little |
| Add a specialist | copy `app/specialists/_template.py` | Yes |
| Implement AWS / Azure capabilities | `app/providers/aws/`, `azure/` | Yes ([providers.md](providers.md)) |

After a change: `make test` (offline, seconds), `make run JOB=error_watch` (one real run in dry-run mode), optionally
`make eval` (LLM-judged behaviour).

## 1. Teach the team about your project (no code)

```bash
cp config/ops-context.example.md config/ops-context.md
```

Describe your services and which ones matter most, your severity policy ("anything touching payments is high"), known
noise that should not become issues, and what you want in recommended fixes. The file is appended to every agent's
instructions (up to 8,000 characters) and never overrides the safety rules. It ships in the container image, so **no secrets**.
Use `OPS_CONTEXT_FILE` to point elsewhere.

## 2. Change schedules (no code)

Schedules are generated from each specialist's jobs; environment variables override them.

```bash
make jobs                                                              # current jobs and schedules
ERROR_WATCH_CRON="0 */6 * * *" ERROR_WATCH_HOURS=6 make gcp-schedule   # every 6 hours
```

Built-in overrides: `ERROR_WATCH_CRON` / `ERROR_WATCH_HOURS`, `WEEKLY_CRON` / `WEEKLY_DAYS`, `MONTHLY_CRON` / `MONTHLY_KEV_DAYS`.
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
`pr_review.py`). Edit it directly — for example, file `medium` security findings too by changing the filing rule.
Placeholders: `__PROVIDER__`, `__TARGET__`, `__TARGET_LABEL__`, `__REGION__`, `__REPO__`, `__LOOKBACK__`, `__STACK__`, `__CLI__`,
`__LANG__`. The tool glossary, output language and project context are appended automatically.

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

Give it to a specialist (`tools=[*SECURITY_AUDIT_TOOLS, check_tls_certificate]`) and add one line to its instruction saying
when to use it. Rules, enforced by `make test`: type hints on every parameter and **no default values**; a docstring for the
model; return a dict with `status` and never raise for API errors; read-only; GitHub writes honour `CONFIG.dry_run`; no
secrets in results. Cloud-neutral tools live in `app/tools/`; cloud-specific fetching belongs in `app/providers/<cloud>/`.

## 6. Add a job to an existing specialist

Append a `Job` — the leader's routing table and the scheduler pick it up.

```python
# app/specialists/error_watch.py
jobs=(
    Job(name="error_watch", ...),
    Job(
        name="weekly_error_review",
        params={"hours": "data.hours if given, else 168"},
        schedule="0 10 * * 1",
        schedule_key="weekly-error-review",       # scheduler job <prefix>-weekly-error-review, override WEEKLY_ERROR_REVIEW_CRON
        scheduled_params={"hours": 168},
    ),
),
```

Then `make jobs`, `make test`, `make gcp-deploy` (so the leader knows the job) and `make gcp-schedule`.

## 7. Add a specialist

```bash
cp app/specialists/_template.py app/specialists/deploy_watch.py
```

Fill in `name`, `role`, `description`, `INSTRUCTION`, `tools` and `jobs`, then append `deploy_watch.SPECIALIST` to
`SPECIALISTS` in `app/specialists/__init__.py`. The leader's team list and routing table are generated from the registry.
A complete example (a specialist that detects error increases right after deployments) is in the
[Japanese guide](../customization.md#7-新しい担当専門家を追加する); its code is language-independent.

```bash
make test && make run JOB=deploy_watch ARGS="--hours 24"
make gcp-deploy && make gcp-schedule
```

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
| Behaviour evaluation | `make eval` (cases in `tests/eval/datasets/`) |

`permission_denied` / `api_disabled` → `make doctor` explains the fix. Unexpected behaviour → read the tool calls printed by
`make run` and adjust the instruction. `agents-cli run` stops after 120 s of silence → use `make run`. Gemini 429s are retried
five times with backoff. Ship changes with `make gcp-deploy`, starting with `OPS_DRY_RUN=true`.
