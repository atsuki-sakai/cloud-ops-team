"""Optional job-history adapter; no history references enter the model's tools."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from app.app_utils.execution_identity import current_delivery_identity
from app.config import CONFIG
from app.core.contextutil import bind_var
from app.core.execution_store import ExecutionScope, ExecutionStore, RunRecord
from app.core.jsonutil import unique_pairs_hook
from app.core.redact import redact, redact_obj

WORKFLOW_VERSION = "fixed-collection-v1-history-v1"
_REPORT_TOOLS = frozenset(
    {
        "publish_report",
        "create_ops_issue",
        "comment_on_issue",
        "add_issue_labels",
        "close_issue",
    }
)
_REPORT_SUCCESS = frozenset(
    {
        "ok",
        "created",
        "dry_run",
        "updated_existing",
        "duplicate_recent",
        "skipped",
        "commented",
        "labeled",
        "closed",
        "verified_not_closed",
        "verified_already_closed",
    }
)
_CURRENT: ContextVar[ExecutionHistory | None] = ContextVar(
    "ops_execution_history", default=None
)


class ExecutionStopped(RuntimeError):
    """The owning async execution ended while a collector thread was working."""


def _version(value: Any) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL, json.dumps(value, sort_keys=True, allow_nan=False)
        )
    )


_unique_object = unique_pairs_hook(ValueError, "Ambiguous structured evidence")

# CONFIG fields that change what a run does; their values version each run.
_CONFIG_VERSION_FIELDS: tuple[str, ...] = (
    "provider",
    "target",
    "region",
    "github_repo",
    "model",
    "research_model",
    "report_language",
    "dry_run",
    "auto_close_issues",
    "max_issues_per_run",
    "error_lookback_hours",
    "recurrence_cooldown_hours",
    "timezone",
    "stack_keywords",
    "error_labels",
    "security_labels",
    "severity_labels",
    "issue_label",
    "execution_max_evidence_bytes",
    "execution_retention_seconds",
)


def _json(value: Any) -> str:
    # Validate before protection, so non-JSON values cannot disappear silently.
    checked = json.loads(
        json.dumps(value, ensure_ascii=False, allow_nan=False),
        object_pairs_hook=_unique_object,
    )
    return json.dumps(redact_obj(checked), ensure_ascii=False, allow_nan=False)


def _safe_storage_error_type(error: Exception) -> str:
    # Exception messages and custom class names may contain private data.
    for error_type in (
        sqlite3.IntegrityError,
        sqlite3.OperationalError,
        sqlite3.DatabaseError,
        sqlite3.Error,
        PermissionError,
        FileNotFoundError,
        OSError,
        ValueError,
        TypeError,
    ):
        if isinstance(error, error_type):
            return error_type.__name__
    return "Exception"


@dataclass
class ExecutionHistory:
    store: ExecutionStore
    scope: ExecutionScope
    run: RunRecord
    created: bool
    partial: bool = False
    report_failed: bool = False
    storage_failed: bool = False
    storage_error_type: str | None = None
    evidence_read_required: bool = False
    _stopped: bool = False
    _finished: bool = False
    _lock: Any = field(default_factory=RLock, repr=False)

    def ensure_active(self) -> None:
        with self._lock:
            if self._stopped:
                raise ExecutionStopped("Execution has already ended")

    def completed_status(self) -> str:
        """Terminal status for a job whose stream finished with a report."""
        if self.report_failed:
            return "report_failed"
        if self.partial or self.evidence_read_required:
            return "partial"
        return "succeeded"

    def _mark_storage_failure(self, error: Exception) -> None:
        self.storage_failed = True
        self.storage_error_type = self.storage_error_type or _safe_storage_error_type(
            error
        )

    def _put(
        self, source: str, content: str, *, media_type: str, **options: Any
    ) -> dict[str, Any]:
        return self.store.put_evidence(
            self.scope,
            self.run.run_id,
            source,
            content,
            media_type=media_type,
            protection_confirmed=True,
            **options,
        )

    def record_json(
        self,
        source: str,
        value: Any,
        *,
        incomplete: bool = False,
        presented: bool = False,
    ) -> None:
        with self._lock:
            self.ensure_active()
            if isinstance(value, dict) and value.get("status") in {
                "rejected",
                "scope_mismatch",
            }:
                incomplete = True
            try:
                content = _json(value)
            except (TypeError, ValueError, OverflowError, RecursionError):
                content = '{"status":"error","message":"Invalid structured evidence"}'
                incomplete = True
            try:
                metadata = self._put(
                    source,
                    content,
                    media_type="application/json",
                    acquisition_complete=not incomplete,
                    missing_reason="incomplete_source" if incomplete else None,
                )
                self.partial |= incomplete
                if presented:
                    self.store.record_evidence_use(
                        self.scope,
                        self.run.run_id,
                        metadata["ref"],
                        metadata["version"],
                        purpose="presented",
                        start=0,
                        end=metadata["saved_range"][1],
                    )
            except Exception as error:
                # ADK may convert tool exceptions into a response and continue.
                # Keep a shared stop signal outside that response so subsequent
                # collectors/writers cannot proceed without their audit trail.
                self._mark_storage_failure(error)
                self._stopped = True
                raise

    def observe_tool(self, name: str, result: Any, *, incomplete: bool) -> None:
        if name == "get_collected_evidence":
            # The reader records its exact bounded return only when called.
            return
        with self._lock:
            self.ensure_active()
            if name in _REPORT_TOOLS:
                status = result.get("status") if isinstance(result, dict) else None
                self.report_failed |= status not in _REPORT_SUCCESS
            self.record_json(
                f"tool/{name}", result, incomplete=incomplete, presented=True
            )

    def finish(self, status: str, *, report: str = "", reason: str) -> None:
        with self._lock:
            if self._finished:
                return
            if self.storage_failed and status not in {
                "failed",
                "timed_out",
                "interrupted",
            }:
                raise ExecutionStopped("Execution history could not be persisted")
            # A cancelled to_thread collector can still return later. Stop it
            # before writing the terminal record, and never let it resurrect us.
            self._stopped = True
            try:
                self._put(
                    "execution_outcome",
                    _json(
                        {
                            "stop_reason": reason,
                            "unverified": self.partial or self.evidence_read_required,
                            "evidence_unpresented": self.evidence_read_required,
                            "report_failed": self.report_failed,
                            "storage_failed": self.storage_failed,
                            "storage_error_type": self.storage_error_type,
                        }
                    ),
                    media_type="application/json",
                    acquisition_complete=True,
                    run_status=None if report else status,
                )
                if report:
                    self._put(
                        "final_report",
                        redact(report),
                        media_type="text/plain",
                        acquisition_complete=True,
                        run_status=status,
                    )
            except Exception as error:
                self._mark_storage_failure(error)
                raise
            self._finished = True


def current_execution() -> ExecutionHistory | None:
    return _CURRENT.get()


def bind_execution(
    execution: ExecutionHistory | None,
) -> AbstractContextManager[None]:
    return bind_var(_CURRENT, execution)


def begin_execution(
    invocation_id: str, job: str, params: Mapping[str, int]
) -> ExecutionHistory | None:
    if not CONFIG.execution_db_path:
        return None
    scope = ExecutionScope(CONFIG.provider, CONFIG.target)
    store = ExecutionStore(
        CONFIG.execution_db_path,
        max_evidence_bytes=CONFIG.execution_max_evidence_bytes,
        retention_seconds=CONFIG.execution_retention_seconds,
    )
    delivery = current_delivery_identity()
    config_version = _version(
        {
            "job": job,
            "params": dict(params),
            "config": {name: getattr(CONFIG, name) for name in _CONFIG_VERSION_FIELDS},
        }
    )
    event_id = (
        delivery.event_id if delivery else _version({"invocation": invocation_id})
    )
    record, created = store.create_run(
        scope,
        job,
        event_id,
        scheduled_at=delivery.scheduled_at if delivery else None,
        trigger=delivery.trigger if delivery else "invocation",
        correlation_id=(delivery.correlation_id if delivery else None) or event_id,
        config_version=config_version,
        scenario_version=WORKFLOW_VERSION,
    )
    if (record.config_version, record.scenario_version) != (
        config_version,
        WORKFLOW_VERSION,
    ):
        raise ValueError("Delivery conflicts with the recorded execution configuration")
    execution = ExecutionHistory(store, scope, record, created)
    if created:
        execution.run = store.transition_run(scope, record.run_id, "running")
    return execution
