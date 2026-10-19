# Execution history and evidence storage

日本語（メイン）: [execution-history.md](../execution-history.md)

The [Issue #8](https://github.com/atsuki-sakai/cloud-ops-team/issues/8) implementation provides
a cloud- and model-independent SQLite store in `app/core/execution_store.py` and connects existing jobs to it.
It persists execution records and protected evidence at a caller-specified location and can reopen the same file after restart.

Setting `OPS_EXECUTION_DB_PATH` records registered jobs and specialist jobs selected through conversation in the same ledger.
When unset, the store is not opened and existing execution behaviour is retained. Conversation-only turns that run no specialist job are not registered.
Completion of all of Issue #8, durable cloud storage and operational release require separate validation.

## Enable recording for existing jobs

```bash
OPS_EXECUTION_DB_PATH=/srv/cloud-ops-private/history.sqlite3 make run JOB=access_watch
```

Choose a writable parent location. See [configuration](configuration.md) for retention and evidence size settings.
Each job is registered before execution and acquired results are protected and stored before candidate filtering or model-preview limits.
No storage paths, new references, tools or descriptions are added to the existing model-facing snapshot.
Full acquired evidence is not automatically shown to the model; model-driven retrieval of later pages remains a separate integration.

Cloud Scheduler job name plus schedule time, or standard Pub/Sub push subscription plus messageId, produces an opaque delivery ID.
This uses the [documented Scheduler retry identity](https://docs.cloud.google.com/scheduler/docs/creating), whose original schedule time remains fixed across attempts.
Headers and bodies do not authenticate callers: existing OIDC/platform verification must succeed before a job enters the ledger.
HTTP/SSE, A2A and direct runs without a known delivery identity remain scoped to the ADK invocation.
Missing or empty subscriptions do not establish a delivery from messageId alone.
Enabled trigger requests are limited to 64 KiB; the original buffered body is replayed to the existing handler.

A repeated scope/job/delivery returns a `duplicate` result event with its existing run ID and state, without repeating collection, model calls or report side effects.
The ADK trigger may still acknowledge with HTTP 200 for duplicates or failed jobs; inspect the ledger and result events rather than HTTP status alone.
Reusing a delivery with different parameters or configuration is rejected. `full` stops at its first duplicate and does not automatically resume unstarted later jobs.
Failed, interrupted and running records are not resumed by redelivery; inspect their state and start a new execution intentionally.
This does not guarantee GitHub issue deduplication across different deliveries or invocations.

Storage failures are not converted to success and stop later collection or GitHub writes.
Failures while saving outcomes or final reports also remain sticky. If an outcome can be saved, it records the first storage failure using a fixed exception classification, without exception messages or traces.
Cancellation cannot forcibly terminate an already-running collector thread, but its returned result and later collection steps are rejected.
`TimeoutError` produces `timed_out`; cancellation or stream closure attempts minimal `interrupted` finalization.
If storage itself cannot accept a final state, or the process is killed, a `running` record may remain. Automatic recovery/resume is not implemented.

Incomplete, unread or truncated evidence remains `partial`; failed report tools produce `report_failed`, alongside a protected final text and state.
`succeeded` means programmatically observed acquisition/presentation and normal stream completion. It does not certify model understanding, evidence quality or business completion.
Runtime enforcement of shared budgets belongs to Issue #19; model quality and release assessment belong to Issue #20.

## Storage and protection requirements

- Callers explicitly select a dedicated directory and database file. There is no fallback to memory or temporary storage when the path is missing.
- The directory and database must be accessible only to their owner. Shared permissions and symbolic links are rejected; SQLite side files stay inside the protected directory.
- Side files may disappear or have zero links during normal concurrent deletion. The database still requires exactly one link; side files with unsafe types, ownership, permissions or multiple links remain rejected.
- Ancestor directories must be owned by the current OS user or root. Group- or other-writable ancestors require the sticky bit, rejecting replacement of the private directory through non-sticky shared storage. Root-owned sticky temporary storage is allowed, but does not provide durable storage.
- Permission errors, storage failures and size-limit failures propagate as exceptions. They do not become successful persistence or successful investigation.
- Persistence means reopening the same file. Cloud Run's temporary filesystem does not provide persistence across replacement or multiple instances. Cloud storage, permissions and backups must be designed before integration.
- Anyone with direct database-file access can read its contents. Scope checks prevent caller mistakes; they do not replace authorization between processes using the same OS identity or database encryption.
- The current OS user and root are trusted. Deliberate path or database replacement by the same OS identity is outside this adapter's permission boundary.

## Execution identity and state

Every operation requires `ExecutionScope(provider, target)`.
The same event has separate executions when its provider, target or job differs.
`create_run` transactionally detects duplicate events and returns both the record and whether it was newly created.
Callers must decide whether an existing execution can be resumed or retried.
Job integration uses this API to suppress the same delivery. Existing GitHub issue deduplication across different deliveries is unchanged.

Records retain the run ID, scheduled time, trigger, configuration and scenario versions, correlation ID, and start/end times.
They distinguish starting, running, success, failure, timeout, partial acquisition, reporting failure and interruption.
Terminal records cannot return to running or success.
The integration records cancellation as interrupted; detecting hard process loss and recovering abandoned records still requires separate monitoring.

Minimal example with an explicit location: select a writable parent and replace the path with durable storage for real use.

```python
from app.core.execution_store import ExecutionScope, ExecutionStore

scope = ExecutionScope("gcp", "example-project")
store = ExecutionStore("/srv/cloud-ops-private/history.sqlite3")
run, created = store.create_run(scope, "error_watch", "example-event-1")
if created:
    store.transition_run(scope, run.run_id, "running")
    evidence = store.put_evidence(
        scope,
        run.run_id,
        "example-source",
        "Protected test evidence",
        media_type="text/plain",
        protection_confirmed=True,
        acquisition_complete=True,
    )
    page = store.read_evidence_page(
        scope, run.run_id, evidence["ref"], evidence["version"], page_size=256
    )
    # Record actual model presentation or citation separately when observed.
    store.transition_run(scope, run.run_id, "succeeded")
```

Here `succeeded` ends the sample operation. Real jobs must verify required acquisition, investigation and reporting before setting their final status.

## Evidence persistence and pagination

Only text and JSON whose acquisition, masking and storage permissions were confirmed by the caller are accepted.
The store also applies existing redaction, but redaction alone does not certify arbitrary input as safe.
Image, video, raw DOM and network-capture protection are outside this backend's contract.
Collectors must restrict targets, acquisition volume and permitted storage scope.

Evidence is bound to the execution, target, source and immutable version.
Metadata returns opaque references, hashes, sizes and storage state without the content or filesystem path.
Oversized content is not silently truncated and reported as completely stored.
API acquisition range, completeness and missing reasons remain separate from the stored range.
The canonical stored content is the protected, redacted content, not recoverable original secret values.

`read_evidence_page` checks scope, run, evidence reference and version and returns a bounded page.
Continuation cursors are signed with a secret retained by the store and cannot be changed or reused for another scope, run, reference or version.
Ranges use Unicode character positions; storage limits use UTF-8 bytes.
Inspect the returned range, remaining content and next cursor and present only the needed pages to the model.

`record_evidence_use` records `presented` and `referenced` ranges separately.
Existing jobs record the bounded `get_collected_evidence` return and follow-up tool responses as separate presented evidence only when the read actually completes.
This does not mark the original acquired body as fully presented or automatically assert citations.
Reading a page does not mark it as investigated. The store cannot prove a model understood or investigated the content.
Callers must observe actual presentation and evidence citations and retain unread or unverified ranges.

## Retention, deletion and budget records

Retention and per-evidence size limits are constructor settings.
Defaults are 30 days, 8 MiB per evidence body, and 4,096 characters per page (maximum 16,384).
Expired content cannot be read. `purge_expired(scope)` removes expired content for that scope;
`delete_evidence` removes one evidence body. Records and missing reasons remain instead of reporting complete acquisition.
Callers must schedule expiry cleanup.
Manage database backups, storage snapshots and physical media erasure separately.

`save_budget_snapshot` records shared limits, consumption, stopping reasons and unverified work.
It does not enforce call, cost or time budgets or reserve and aggregate concurrent consumption.
Issue #19 will provide that runtime control and connect it to persistence of evidence and unfinished work on stopping.

## Validation and follow-up

Unit tests use real SQLite files in temporary directories without model, cloud or GitHub calls.

```bash
uv run pytest tests/unit/test_execution_store.py -q
uv run pytest tests/unit/test_execution_history.py tests/unit/test_execution_identity.py -q
uv run pytest tests/unit/test_execution_trigger_history.py -q
uv run pytest tests/unit -q
```

The starting baseline is `3ca4e6d57c8f46486e6ca351f1d98faf3a4ef754`, with 537 existing unit tests passing.
Storage-layer tests do not establish model-selection accuracy or production operation.

| Integration | Remaining work |
|---|---|
| Issue #8 operational integration | Detect hard process loss and recover abandoned records; define total retention/capacity and delivery/caller operating boundaries |
| Issue #8 operations | Select durable cloud storage, reader authorization, total capacity, ledger retention, deletion and backups |
| Issue #19 | Connect presented tool sets, actual calls, stages, versions and runtime enforcement of shared budgets |
| Issue #20 | Compare long evidence, later pages and budget stops; run comparable live-model evaluation when model presentation changes |
| Issue #11 and later features | Integrate bounded evidence reads and preserve missing or unverified coverage in each investigation |
