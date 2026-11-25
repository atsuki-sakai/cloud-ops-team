"""Explicit local SQLite storage for run history and already-protected evidence.

Callers must supply a private directory on persistent storage when restart
durability is required. This adapter supplies neither tenant authentication nor
raw-media sanitization, and it is not connected to job execution automatically.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.redact import redact, redact_obj

MAX_PAGE_CHARS = 16_384
TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "timed_out", "partial", "report_failed", "interrupted"}
)
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z", re.I)
_SECRET_KEY = re.compile(
    r"secret|token|password|passwd|pwd|credential|authorization|cookie|private.?key|api.?key|access.?key",
    re.IGNORECASE,
)


class InvalidTransition(ValueError):
    """The current status does not permit the requested transition."""


class EvidenceNotFound(LookupError):
    """No evidence matches all of scope, run, reference and version."""


def _label(value: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ValueError("identifier must be a nonempty bounded label")
    if not _UUID.fullmatch(value) and redact(value) != value:
        raise ValueError("identifier must not contain sensitive material")
    return value


def _positive(value: int, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON keys would discard evidence")
    return result


def _protected_json(value: Any) -> Any:
    """Mask key-context secrets as well as the shared redactor's patterns."""
    if isinstance(value, dict):
        return _json_object(
            [
                (
                    redact(key),
                    "[REDACTED]" if _SECRET_KEY.search(key) else _protected_json(v),
                )
                for key, v in value.items()
            ]
        )
    if isinstance(value, list):
        return [_protected_json(item) for item in value]
    return redact_obj(value)


@dataclass(frozen=True)
class ExecutionScope:
    provider: str
    target: str

    def __post_init__(self) -> None:
        _label(self.provider)
        _label(self.target)


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    job: str
    event_id: str
    scheduled_at: float | None
    trigger: str
    config_version: str
    scenario_version: str
    correlation_id: str
    started_at: float
    ended_at: float | None
    status: str
    budget_snapshot: dict[str, Any] | None


class ExecutionStore:
    """Short-lived connections; every public data operation requires a scope.

    Private filesystem ownership is a deployment precondition, not multi-user
    authorization. SQLite errors intentionally propagate to the caller.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_evidence_bytes: int = 8 * 1024 * 1024,
        retention_seconds: int = 30 * 24 * 3600,
    ) -> None:
        self._path = Path(db_path)
        self._max_bytes = _positive(max_evidence_bytes, "max_evidence_bytes")
        self._retention = _positive(retention_seconds, "retention_seconds")
        if not self._path.is_absolute() or self._path.name in {"", ".", ".."}:
            raise ValueError("an explicit absolute database path is required")
        self._check_ancestors()
        self._path.parent.mkdir(mode=0o700, exist_ok=True)
        self._check_private(self._path.parent, directory=True)
        try:
            fd = os.open(
                self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value BLOB);
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, provider TEXT NOT NULL, target TEXT NOT NULL,
                    job TEXT NOT NULL, event_id TEXT NOT NULL, scheduled_at REAL,
                    trigger TEXT NOT NULL, config_version TEXT NOT NULL,
                    scenario_version TEXT NOT NULL, correlation_id TEXT NOT NULL,
                    started_at REAL NOT NULL, ended_at REAL, status TEXT NOT NULL,
                    budget_json TEXT,
                    UNIQUE(provider, target, job, event_id)
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    ref TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    version INTEGER NOT NULL, source TEXT NOT NULL, media_type TEXT NOT NULL,
                    content TEXT, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    total_chars INTEGER NOT NULL, acquired_start INTEGER, acquired_end INTEGER,
                    acquisition_complete INTEGER NOT NULL, acquisition_missing_reason TEXT,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL, tombstone_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS evidence_uses (
                    id INTEGER PRIMARY KEY, ref TEXT NOT NULL REFERENCES evidence(ref),
                    purpose TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
                    recorded_at REAL NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO settings VALUES ('cursor_secret', ?)",
                (secrets.token_bytes(32),),
            )

    def _check_ancestors(self) -> None:
        for parent in (self._path, *self._path.parents):
            try:
                info = parent.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("symlink database paths are not permitted")
            if parent != self._path and (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, os.getuid()}
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)
            ):
                raise ValueError(
                    "database ancestors must have trusted ownership and permissions"
                )

    @staticmethod
    def _check_private(
        path: Path, *, directory: bool = False, allow_unlinked: bool = False
    ) -> None:
        info = path.lstat()
        correct_type = (
            stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        )
        mode = 0o700 if directory else 0o600
        if (
            not correct_type
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_uid != os.getuid()
            or (
                not directory
                and info.st_nlink not in ({0, 1} if allow_unlinked else {1})
            )
        ):
            raise ValueError(
                "database storage must have private ownership and permissions"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._check_ancestors()
        self._check_private(self._path.parent, directory=True)
        self._check_private(self._path)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self._path) + suffix)
            try:
                self._check_private(sidecar, allow_unlinked=True)
            except FileNotFoundError:
                # Another connection may remove its journal while committing.
                # Only these optional files may disappear; the DB stays required.
                pass
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA secure_delete = ON")
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _scope(scope: ExecutionScope) -> tuple[str, str]:
        if not isinstance(scope, ExecutionScope):
            raise TypeError("ExecutionScope is required")
        return scope.provider, scope.target

    def _run_row(
        self, conn: sqlite3.Connection, scope: ExecutionScope, run_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM runs WHERE provider=? AND target=? AND run_id=?",
            (*self._scope(scope), run_id),
        ).fetchone()
        if row is None:
            raise LookupError("run not found in scope")
        return row

    @staticmethod
    def _run_record(row: sqlite3.Row) -> RunRecord:
        fields = {
            name: row[name]
            for name in RunRecord.__dataclass_fields__
            if name != "budget_snapshot"
        }
        return RunRecord(
            **fields,
            budget_snapshot=json.loads(row["budget_json"])
            if row["budget_json"]
            else None,
        )

    def create_run(
        self,
        scope: ExecutionScope,
        job: str,
        event_id: str,
        *,
        scheduled_at: float | None = None,
        trigger: str = "manual",
        config_version: str = "unverified",
        scenario_version: str = "unverified",
        correlation_id: str | None = None,
    ) -> tuple[RunRecord, bool]:
        """Atomically deduplicate scope + job + event; return (record, created)."""
        values = [job, event_id, trigger, config_version, scenario_version]
        for value in values:
            _label(value)
        correlation_id = _label(correlation_id or str(uuid.uuid4()))
        if scheduled_at is not None:
            self._timestamp(scheduled_at)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_id = str(uuid.uuid4())
            result = conn.execute(
                """INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                   ON CONFLICT(provider,target,job,event_id) DO NOTHING""",
                (
                    run_id,
                    *self._scope(scope),
                    job,
                    event_id,
                    scheduled_at,
                    trigger,
                    config_version,
                    scenario_version,
                    correlation_id,
                    time.time(),
                    None,
                    "starting",
                ),
            )
            row = conn.execute(
                "SELECT * FROM runs WHERE provider=? AND target=? AND job=? AND event_id=?",
                (*self._scope(scope), job, event_id),
            ).fetchone()
            return self._run_record(row), bool(result.rowcount)

    @staticmethod
    def _timestamp(value: float) -> float:
        if type(value) not in {float, int} or not 0 <= value < float("inf"):
            raise ValueError("timestamp must be finite and nonnegative")
        return value

    def get_run(self, scope: ExecutionScope, run_id: str) -> RunRecord:
        with self._connection() as conn:
            return self._run_record(self._run_row(conn, scope, run_id))

    def _transition(
        self, conn: sqlite3.Connection, scope: ExecutionScope, run_id: str, status: str
    ) -> RunRecord:
        row = self._run_row(conn, scope, run_id)
        allowed = (
            {"running", *TERMINAL_STATUSES}
            if row["status"] == "starting"
            else TERMINAL_STATUSES
            if row["status"] == "running"
            else set()
        )
        if status not in allowed:
            raise InvalidTransition("status transition is not permitted")
        conn.execute(
            "UPDATE runs SET status=?, ended_at=? WHERE run_id=?",
            (status, time.time() if status in TERMINAL_STATUSES else None, run_id),
        )
        return self._run_record(self._run_row(conn, scope, run_id))

    def transition_run(
        self, scope: ExecutionScope, run_id: str, status: str
    ) -> RunRecord:
        """Only one worker can claim starting -> running; terminal states stay terminal."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._transition(conn, scope, run_id, status)

    def save_budget_snapshot(
        self,
        scope: ExecutionScope,
        run_id: str,
        *,
        limits: Mapping[str, int | float],
        consumed: Mapping[str, int | float],
        stop_reason: str | None = None,
        unverified: bool = True,
    ) -> None:
        """Save observations only: this adapter does not aggregate or enforce budgets."""
        snapshot: dict[str, Any] = {
            "unverified": unverified,
            "stop_reason": stop_reason,
        }
        if type(unverified) is not bool or stop_reason not in {
            None,
            "limit_reached",
            "deadline",
            "cancelled",
            "unverified",
            "error",
        }:
            raise ValueError("invalid budget snapshot state")
        for name, counters in (("limits", limits), ("consumed", consumed)):
            snapshot[name] = {}
            for key, value in counters.items():
                token_counter = key == "tokens" or key.endswith("_tokens")
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) or (
                    _SECRET_KEY.search(key) and not token_counter
                ):
                    raise ValueError("invalid budget counter name")
                snapshot[name][key] = self._timestamp(value)
        with self._connection() as conn:
            self._run_row(conn, scope, run_id)
            conn.execute(
                "UPDATE runs SET budget_json=? WHERE run_id=?",
                (json.dumps(snapshot), run_id),
            )

    def put_evidence(
        self,
        scope: ExecutionScope,
        run_id: str,
        source: str,
        content: str,
        *,
        media_type: str,
        protection_confirmed: bool,
        acquisition_complete: bool,
        version: int = 1,
        acquisition_range: tuple[int, int] | None = None,
        missing_reason: str | None = None,
        run_status: str | None = None,
    ) -> dict[str, Any]:
        """Store complete protected text, optionally changing run status atomically.

        Confirmation must come from a caller that already applied its protection
        policy. Raw HTML/DOM/network captures, images and video are unsupported.
        Acquisition ranges describe the source; saved/page ranges use codepoints.
        """
        if protection_confirmed is not True or type(acquisition_complete) is not bool:
            raise ValueError(
                "explicit protection and acquisition confirmation required"
            )
        if media_type not in {"text/plain", "application/json"} or not isinstance(
            content, str
        ):
            raise ValueError(
                "only protected text/plain or application/json is supported"
            )
        _positive(version, "version")
        _label(source)
        if acquisition_range is not None:
            self._range(*acquisition_range)
        if missing_reason is not None:
            _label(missing_reason)
            if acquisition_complete:
                raise ValueError("complete acquisition cannot have a missing reason")
        if not acquisition_complete and missing_reason is None:
            missing_reason = "unverified"
        # Reject oversized original input as well, never silently truncate it.
        if len(content.encode("utf-8")) > self._max_bytes:
            raise ValueError("evidence exceeds max_evidence_bytes")
        if media_type == "application/json":
            parsed = json.loads(content, object_pairs_hook=_json_object)
            protected = json.dumps(
                _protected_json(redact_obj(parsed)), ensure_ascii=False, allow_nan=False
            )
        else:
            protected = redact(content)
        payload = protected.encode("utf-8")
        if len(payload) > self._max_bytes:
            raise ValueError("protected evidence exceeds max_evidence_bytes")
        ref = "ev_" + secrets.token_urlsafe(24)
        now = time.time()
        acquired_start, acquired_end = acquisition_range or (None, None)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._run_row(conn, scope, run_id)
            conn.execute(
                "INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    ref,
                    run_id,
                    version,
                    source,
                    media_type,
                    protected,
                    hashlib.sha256(payload).hexdigest(),
                    len(payload),
                    len(protected),
                    acquired_start,
                    acquired_end,
                    acquisition_complete,
                    missing_reason,
                    now,
                    now + self._retention,
                ),
            )
            if run_status is not None:
                self._transition(conn, scope, run_id, run_status)
            return self._metadata(self._evidence_row(conn, scope, run_id, ref, version))

    def _evidence_row(
        self,
        conn: sqlite3.Connection,
        scope: ExecutionScope,
        run_id: str,
        ref: str,
        version: int,
    ) -> sqlite3.Row:
        _positive(version, "version")
        row = conn.execute(
            """SELECT evidence.* FROM evidence JOIN runs USING(run_id)
               WHERE provider=? AND target=? AND run_id=? AND ref=? AND version=?""",
            (*self._scope(scope), run_id, ref, version),
        ).fetchone()
        if row is None:
            raise EvidenceNotFound("evidence not found in requested scope/run/version")
        return row

    @staticmethod
    def _metadata(row: sqlite3.Row) -> dict[str, Any]:
        reason = row["tombstone_reason"] or (
            "expired" if row["expires_at"] <= time.time() else None
        )
        return {
            "ref": row["ref"],
            "run_id": row["run_id"],
            "version": row["version"],
            "source": row["source"],
            "media_type": row["media_type"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "range_unit": "unicode_codepoints",
            "saved_range": [0, row["total_chars"]],
            "saved_complete": True,
            "storage_status": "missing" if reason else "available",
            "missing_reason": reason,
            "expires_at": row["expires_at"],
            "acquisition_range": [row["acquired_start"], row["acquired_end"]],
            "acquisition_complete": bool(row["acquisition_complete"]),
            "acquisition_missing_reason": row["acquisition_missing_reason"],
            "complete": not reason and bool(row["acquisition_complete"]),
        }

    def get_evidence_metadata(
        self, scope: ExecutionScope, run_id: str, ref: str, version: int
    ) -> dict[str, Any]:
        with self._connection() as conn:
            return self._metadata(self._evidence_row(conn, scope, run_id, ref, version))

    def list_evidence_metadata(
        self, scope: ExecutionScope, run_id: str
    ) -> list[dict[str, Any]]:
        """Discover a run's evidence after restart, without returning private bodies."""
        with self._connection() as conn:
            self._run_row(conn, scope, run_id)
            return [
                self._metadata(row)
                for row in conn.execute(
                    "SELECT * FROM evidence WHERE run_id=? ORDER BY created_at,ref",
                    (run_id,),
                )
            ]

    @staticmethod
    def _cursor_secret(conn: sqlite3.Connection) -> bytes:
        return conn.execute(
            "SELECT value FROM settings WHERE key='cursor_secret'"
        ).fetchone()[0]

    def _cursor(self, conn: sqlite3.Connection, binding: list[Any], offset: int) -> str:
        payload = json.dumps([*binding, offset], separators=(",", ":")).encode()
        signature = hmac.digest(self._cursor_secret(conn), payload, "sha256")
        return base64.urlsafe_b64encode(signature + payload).decode()

    def _cursor_offset(
        self, conn: sqlite3.Connection, binding: list[Any], cursor: str
    ) -> int:
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ValueError
            decoded = base64.b64decode(cursor, altchars=b"-_", validate=True)
            if base64.urlsafe_b64encode(decoded).decode() != cursor:
                raise ValueError
            signature, payload = decoded[:32], decoded[32:]
            if not hmac.compare_digest(
                signature, hmac.digest(self._cursor_secret(conn), payload, "sha256")
            ):
                raise ValueError
            values = json.loads(payload)
            if values[:-1] != binding or type(values[-1]) is not int or values[-1] < 0:
                raise ValueError
            return values[-1]
        except (ValueError, TypeError, IndexError) as exc:
            raise ValueError("invalid evidence cursor") from exc

    def read_evidence_page(
        self,
        scope: ExecutionScope,
        run_id: str,
        ref: str,
        version: int,
        *,
        page_size: int = 4096,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Read without marking evidence as presented, referenced or verified."""
        _positive(page_size, "page_size")
        if page_size > MAX_PAGE_CHARS:
            raise ValueError("page_size exceeds fixed maximum")
        with self._connection() as conn:
            row = self._evidence_row(conn, scope, run_id, ref, version)
            binding = [*self._scope(scope), run_id, ref, version]
            start = (
                self._cursor_offset(conn, binding, cursor) if cursor is not None else 0
            )
            metadata = self._metadata(row)
            available = metadata["storage_status"] == "available"
            end = min(start + page_size, row["total_chars"]) if available else start
            if start > row["total_chars"]:
                raise ValueError("cursor is outside the evidence range")
            return {
                **metadata,
                "content": row["content"][start:end] if available else None,
                "returned_range": [start, end],
                "total": row["total_chars"],
                "complete": metadata["complete"] and end == row["total_chars"],
                "page_complete": available and end == row["total_chars"],
                "next_cursor": self._cursor(conn, binding, end)
                if available and end < row["total_chars"]
                else None,
            }

    @staticmethod
    def _range(start: int, end: int) -> None:
        if type(start) is not int or type(end) is not int or not 0 <= start <= end:
            raise ValueError("range must contain ordered nonnegative integers")

    def record_evidence_use(
        self,
        scope: ExecutionScope,
        run_id: str,
        ref: str,
        version: int,
        *,
        purpose: str,
        start: int,
        end: int,
    ) -> None:
        """Explicit audit assertion by the caller; reading alone creates no assertion."""
        self._range(start, end)
        if purpose not in {"presented", "referenced"}:
            raise ValueError("purpose must be presented or referenced")
        with self._connection() as conn:
            row = self._evidence_row(conn, scope, run_id, ref, version)
            if (
                end > row["total_chars"]
                or self._metadata(row)["storage_status"] != "available"
            ):
                raise ValueError("evidence range is unavailable")
            conn.execute(
                "INSERT INTO evidence_uses(ref,purpose,start,end,recorded_at) VALUES (?,?,?,?,?)",
                (ref, purpose, start, end, time.time()),
            )

    def list_evidence_uses(
        self, scope: ExecutionScope, run_id: str, ref: str, version: int
    ) -> list[dict[str, Any]]:
        with self._connection() as conn:
            self._evidence_row(conn, scope, run_id, ref, version)
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT purpose,start,end,recorded_at FROM evidence_uses WHERE ref=? ORDER BY id",
                    (ref,),
                )
            ]

    def delete_evidence(
        self, scope: ExecutionScope, run_id: str, ref: str, version: int
    ) -> None:
        """Keep metadata/history; no guarantee of physical erasure from storage/backups."""
        with self._connection() as conn:
            self._evidence_row(conn, scope, run_id, ref, version)
            conn.execute(
                "UPDATE evidence SET content=NULL,tombstone_reason=COALESCE(tombstone_reason,'deleted') WHERE ref=?",
                (ref,),
            )

    def purge_expired(self, scope: ExecutionScope) -> int:
        """Delete only expired bodies belonging to the supplied scope, retaining tombstones."""
        with self._connection() as conn:
            result = conn.execute(
                """UPDATE evidence SET content=NULL,tombstone_reason='expired'
                   WHERE expires_at<=? AND content IS NOT NULL AND run_id IN
                   (SELECT run_id FROM runs WHERE provider=? AND target=?)""",
                (time.time(), *self._scope(scope)),
            )
            return result.rowcount
