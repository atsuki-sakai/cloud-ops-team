"""Offline contracts against actual SQLite files, including failure boundaries."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Barrier

import pytest

from app.core import execution_store
from app.core.execution_store import (
    MAX_PAGE_CHARS,
    EvidenceNotFound,
    ExecutionScope,
    ExecutionStore,
    InvalidTransition,
)

SCOPE = ExecutionScope("gcp", "project-a")
OTHER = ExecutionScope("gcp", "project-b")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "private" / "runs.sqlite3"


@pytest.fixture
def store(db_path: Path) -> ExecutionStore:
    return ExecutionStore(db_path)


def new_run(
    store: ExecutionStore, scope: ExecutionScope = SCOPE, event: str = "event-1"
) -> str:
    run, created = store.create_run(scope, "error_watch", event)
    assert created
    return run.run_id


def evidence(
    store: ExecutionStore, run_id: str, *, scope: ExecutionScope = SCOPE, **kwargs
):
    options = {
        "source": "cloud.logs",
        "content": "protected evidence",
        "media_type": "text/plain",
        "protection_confirmed": True,
        "acquisition_complete": True,
    }
    options.update(kwargs)
    return store.put_evidence(scope, run_id, **options)


def test_scope_is_frozen_and_validated():
    with pytest.raises(FrozenInstanceError):
        SCOPE.target = "different"
    with pytest.raises(ValueError):
        ExecutionScope("gcp", "")
    with pytest.raises(ValueError):
        ExecutionScope("gcp", "Bearer secret")


def test_reopen_preserves_run_and_all_metadata(store, db_path):
    run, created = store.create_run(
        SCOPE,
        "error_watch",
        "scheduled-event",
        scheduled_at=1234.5,
        trigger="scheduler",
        config_version="config-2",
        scenario_version="scenario-3",
        correlation_id="corr-1",
    )
    assert created
    store.transition_run(SCOPE, run.run_id, "running")
    reopened = ExecutionStore(db_path)
    restored = reopened.get_run(SCOPE, run.run_id)
    assert restored.scheduled_at == 1234.5
    assert restored.config_version == "config-2"
    assert restored.scenario_version == "scenario-3"
    assert restored.correlation_id == "corr-1"
    assert restored.trigger == "scheduler"
    assert restored.status == "running"
    assert restored.started_at > 0 and restored.ended_at is None


def test_uuid_identifiers_are_not_rejected_as_personal_numbers(store, monkeypatch):
    identifier = "8a5ca3dc-7772-4602-8102-506589112792"
    monkeypatch.setattr(execution_store.uuid, "uuid4", lambda: identifier)
    run, created = store.create_run(SCOPE, "error_watch", "event-1")
    assert created and run.correlation_id == identifier


def test_deduplication_is_atomic_across_connections(store, db_path):
    barrier = Barrier(4)

    def create(_):
        independent = ExecutionStore(db_path)
        barrier.wait()
        return independent.create_run(SCOPE, "error_watch", "same-event")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert sum(created for _, created in results) == 1
    assert len({run.run_id for run, _ in results}) == 1
    assert store.create_run(OTHER, "error_watch", "same-event")[1]
    assert store.create_run(SCOPE, "security_audit", "same-event")[1]


def test_only_one_worker_can_claim_run(store, db_path):
    run_id = new_run(store)
    barrier = Barrier(2)

    def claim(_):
        independent = ExecutionStore(db_path)
        barrier.wait()
        try:
            independent.transition_run(SCOPE, run_id, "running")
        except InvalidTransition:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, range(2))) == [False, True]


@pytest.mark.parametrize("initial", ["starting", "running"])
@pytest.mark.parametrize(
    "terminal",
    [
        "succeeded",
        "failed",
        "timed_out",
        "partial",
        "report_failed",
        "interrupted",
    ],
)
def test_terminal_states_cannot_be_revived(store, initial, terminal):
    run_id = new_run(store)
    if initial == "running":
        store.transition_run(SCOPE, run_id, "running")
    ended = store.transition_run(SCOPE, run_id, terminal)
    assert ended.ended_at >= ended.started_at
    for status in ("running", "succeeded", terminal):
        with pytest.raises(InvalidTransition):
            store.transition_run(SCOPE, run_id, status)
    assert store.get_run(SCOPE, run_id).status == terminal


def test_provider_is_part_of_scope_and_idempotency_identity(store):
    run_id = new_run(store)
    other_provider = ExecutionScope("aws", SCOPE.target)
    other_run = new_run(store, other_provider)
    meta = evidence(store, run_id)
    with pytest.raises(LookupError):
        store.get_run(other_provider, run_id)
    with pytest.raises(EvidenceNotFound):
        store.read_evidence_page(other_provider, run_id, meta["ref"], 1)
    with pytest.raises(EvidenceNotFound):
        store.delete_evidence(other_provider, other_run, meta["ref"], 1)


def test_all_run_operations_enforce_scope(store):
    run_id = new_run(store)
    for action in (
        lambda: store.get_run(OTHER, run_id),
        lambda: store.transition_run(OTHER, run_id, "running"),
        lambda: store.save_budget_snapshot(OTHER, run_id, limits={}, consumed={}),
        lambda: evidence(store, run_id, scope=OTHER),
    ):
        with pytest.raises(LookupError):
            action()
    with pytest.raises(TypeError):
        store.get_run(None, run_id)


def test_evidence_is_protected_persisted_and_metadata_has_no_body_or_path(
    store, db_path
):
    run_id = new_run(store)
    metadata = evidence(
        store,
        run_id,
        content=json.dumps(
            {
                "nested": [
                    {"clientSecret": {"value": "hidden-one"}},
                    {"refresh_token": "hidden-two"},
                ],
                "Password": ["hidden-three"],
                "safe": "tail",
                "message": "Bearer hidden-four",
            }
        ),
        media_type="application/json",
        acquisition_range=(0, 15),
    )
    assert metadata["storage_status"] == "available"
    assert metadata["acquisition_range"] == [0, 15]
    assert "content" not in metadata and "path" not in metadata
    assert str(db_path) not in json.dumps(metadata)
    reopened = ExecutionStore(db_path)
    page = reopened.read_evidence_page(SCOPE, run_id, metadata["ref"], 1)
    assert "hidden-" not in page["content"]
    assert json.loads(page["content"])["safe"] == "tail"
    assert page["complete"] and page["page_complete"]
    assert not reopened.list_evidence_uses(SCOPE, run_id, metadata["ref"], 1)
    assert b"hidden-one" not in db_path.read_bytes()


def test_plain_text_is_redacted(store):
    run_id = new_run(store)
    meta = evidence(
        store,
        run_id,
        content="Authorization: Bearer do-not-store\npassword=also-hidden",
    )
    page = store.read_evidence_page(SCOPE, run_id, meta["ref"], 1)
    assert "do-not-store" not in page["content"]
    assert "also-hidden" not in page["content"]


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "api-key",
        "apikey",
        "access_key",
        "access-key",
        "accesskey",
        "client_secret",
        "client-secret",
        "clientSecret",
        "refresh_token",
        "authorization",
        "cookie",
        "privateKey",
    ],
)
def test_shared_and_extended_secret_keys_never_reach_body_or_database(
    store, db_path, key
):
    run_id = new_run(store)
    content = json.dumps({"nested": [{key: "MUST_REMOVE_VALUE"}], "safe": "remains"})
    meta = evidence(store, run_id, content=content, media_type="application/json")
    page = store.read_evidence_page(SCOPE, run_id, meta["ref"], 1)
    assert "MUST_REMOVE_VALUE" not in page["content"]
    assert json.loads(page["content"])["safe"] == "remains"
    assert b"MUST_REMOVE_VALUE" not in db_path.read_bytes()


@pytest.mark.parametrize(
    "options",
    [
        {"protection_confirmed": False},
        {"protection_confirmed": 1},
        {"media_type": "image/png"},
        {"media_type": "text/html"},
        {"media_type": "application/octet-stream"},
        {"version": 0},
        {"version": True},
        {"acquisition_complete": "true"},
        {"acquisition_range": (-1, 5)},
        {"missing_reason": "truncated"},
    ],
)
def test_unsafe_or_invalid_evidence_is_rejected(store, options):
    with pytest.raises(ValueError):
        evidence(store, new_run(store), **options)


def test_protection_confirmation_is_required(store):
    with pytest.raises(TypeError):
        store.put_evidence(
            SCOPE,
            new_run(store),
            "logs",
            "safe",
            media_type="text/plain",
            acquisition_complete=True,
        )


@pytest.mark.parametrize(
    "content",
    [
        '{"key": 1, "key": 2}',
        '{"a@example.com": 1, "b@example.com": 2}',
        '{"value": NaN}',
    ],
)
def test_json_does_not_silently_discard_data_or_accept_nonfinite_values(store, content):
    with pytest.raises(ValueError):
        evidence(store, new_run(store), content=content, media_type="application/json")


def test_size_limit_never_silently_truncates(db_path):
    store = ExecutionStore(db_path, max_evidence_bytes=8)
    run_id = new_run(store)
    with pytest.raises(ValueError, match="max_evidence_bytes"):
        evidence(store, run_id, content="あいう")
    meta = evidence(store, run_id, content="12345678")
    assert meta["size_bytes"] == 8
    assert (
        store.read_evidence_page(SCOPE, run_id, meta["ref"], 1)["content"] == "12345678"
    )


def test_long_unicode_body_reaches_tail_and_cursor_survives_reopen(store, db_path):
    run_id = new_run(store)
    text = "証拠" * 5000 + "最後の証拠"
    meta = evidence(store, run_id, content=text)
    pieces, cursor, previous_end = [], None, 0
    while True:
        page = ExecutionStore(db_path).read_evidence_page(
            SCOPE, run_id, meta["ref"], 1, page_size=4093, cursor=cursor
        )
        assert page["returned_range"][0] == previous_end
        previous_end = page["returned_range"][1]
        pieces.append(page["content"])
        cursor = page["next_cursor"]
        if cursor is None:
            assert page["complete"] and previous_end == len(text)
            break
        assert not page["complete"]
    assert "".join(pieces) == text
    assert meta["size_bytes"] == len(text.encode())
    with pytest.raises(ValueError):
        store.read_evidence_page(
            SCOPE, run_id, meta["ref"], 1, page_size=MAX_PAGE_CHARS + 1
        )


def test_cursor_is_bound_to_scope_run_ref_and_version(store):
    first = new_run(store)
    second = new_run(store, event="second")
    other = new_run(store, OTHER)
    a = evidence(store, first, version=2)
    b = evidence(store, second, version=2)
    c = evidence(store, other, scope=OTHER, version=2)
    d = evidence(store, first, version=3)
    cursor = store.read_evidence_page(SCOPE, first, a["ref"], 2, page_size=3)[
        "next_cursor"
    ]
    for scope, run_id, ref, version in [
        (SCOPE, second, b["ref"], 2),
        (OTHER, other, c["ref"], 2),
        (SCOPE, first, d["ref"], 3),
    ]:
        with pytest.raises(ValueError, match="cursor"):
            store.read_evidence_page(scope, run_id, ref, version, cursor=cursor)
    with pytest.raises(ValueError, match="cursor"):
        store.read_evidence_page(SCOPE, first, a["ref"], 2, cursor="!" + cursor[1:])
    with pytest.raises(ValueError, match="cursor"):
        store.read_evidence_page(SCOPE, first, a["ref"], 2, cursor=cursor + "=")
    decoded = base64.urlsafe_b64decode(cursor)
    values = json.loads(decoded[32:])
    values[-1] += 1
    modified = decoded[:32] + json.dumps(values, separators=(",", ":")).encode()
    forged_cursor = base64.urlsafe_b64encode(modified).decode()
    with pytest.raises(ValueError, match="cursor"):
        store.read_evidence_page(SCOPE, first, a["ref"], 2, cursor=forged_cursor)
    with pytest.raises(EvidenceNotFound):
        store.read_evidence_page(OTHER, first, a["ref"], 2, cursor=cursor)
    with pytest.raises(EvidenceNotFound):
        store.read_evidence_page(SCOPE, first, a["ref"], 3, cursor=cursor)


def test_evidence_is_immutable_and_explicit_audit_is_separate(store):
    run_id = new_run(store)
    first = evidence(store, run_id)
    second = evidence(store, run_id, content="replacement")
    assert first["ref"] != second["ref"]
    page = store.read_evidence_page(SCOPE, run_id, first["ref"], 1)
    assert page["content"] == "protected evidence"
    assert store.list_evidence_uses(SCOPE, run_id, first["ref"], 1) == []
    store.record_evidence_use(
        SCOPE, run_id, first["ref"], 1, purpose="presented", start=0, end=9
    )
    store.record_evidence_use(
        SCOPE, run_id, first["ref"], 1, purpose="referenced", start=10, end=18
    )
    uses = store.list_evidence_uses(SCOPE, run_id, first["ref"], 1)
    assert [(u["purpose"], u["start"], u["end"]) for u in uses] == [
        ("presented", 0, 9),
        ("referenced", 10, 18),
    ]
    with pytest.raises(ValueError):
        store.record_evidence_use(
            SCOPE, run_id, first["ref"], 1, purpose="verified", start=0, end=1
        )
    with pytest.raises(ValueError):
        store.record_evidence_use(
            SCOPE, run_id, first["ref"], 1, purpose="presented", start=0, end=19
        )


def test_partial_acquisition_never_becomes_complete_from_reading(store):
    run_id = new_run(store)
    meta = evidence(store, run_id, acquisition_complete=False, acquisition_range=(0, 5))
    assert meta["acquisition_missing_reason"] == "unverified"
    page = store.read_evidence_page(SCOPE, run_id, meta["ref"], 1)
    assert page["page_complete"] and page["saved_complete"]
    assert not page["complete"] and not page["acquisition_complete"]
    assert page["next_cursor"] is None


def test_expiry_refuses_body_and_purge_is_scope_limited(store, monkeypatch, db_path):
    first = new_run(store)
    other = new_run(store, OTHER)
    a = evidence(store, first)
    b = evidence(store, other, scope=OTHER)
    monkeypatch.setattr(execution_store.time, "time", lambda: a["expires_at"] + 1)
    page = store.read_evidence_page(SCOPE, first, a["ref"], 1)
    assert page["content"] is None and not page["complete"]
    assert page["missing_reason"] == "expired"
    with pytest.raises(ValueError):
        store.record_evidence_use(
            SCOPE, first, a["ref"], 1, purpose="presented", start=0, end=1
        )
    assert store.purge_expired(SCOPE) == 1
    assert store.purge_expired(SCOPE) == 0
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT content FROM evidence WHERE ref=?", (a["ref"],)
            ).fetchone()[0]
            is None
        )
        assert (
            conn.execute(
                "SELECT content FROM evidence WHERE ref=?", (b["ref"],)
            ).fetchone()[0]
            is not None
        )
    assert store.get_run(SCOPE, first).status == "starting"


def test_deletion_keeps_tombstone_audit_and_history(store):
    run_id = new_run(store)
    meta = evidence(store, run_id)
    store.record_evidence_use(
        SCOPE, run_id, meta["ref"], 1, purpose="referenced", start=0, end=3
    )
    with pytest.raises(EvidenceNotFound):
        store.delete_evidence(OTHER, run_id, meta["ref"], 1)
    store.delete_evidence(SCOPE, run_id, meta["ref"], 1)
    page = store.read_evidence_page(SCOPE, run_id, meta["ref"], 1)
    assert page["content"] is None and not page["complete"]
    assert page["storage_status"] == "missing" and page["missing_reason"] == "deleted"
    assert (
        page["sha256"] == meta["sha256"] and page["saved_range"] == meta["saved_range"]
    )
    assert store.get_run(SCOPE, run_id).status == "starting"
    assert len(store.list_evidence_uses(SCOPE, run_id, meta["ref"], 1)) == 1


def test_status_and_evidence_commit_atomically(store, db_path):
    run_id = new_run(store)
    meta = evidence(store, run_id, run_status="succeeded")
    assert store.get_run(SCOPE, run_id).status == "succeeded"
    with pytest.raises(InvalidTransition):
        evidence(store, run_id, run_status="running")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT ref FROM evidence").fetchall() == [(meta["ref"],)]


def test_database_write_failure_rolls_back_status_and_propagates(store, db_path):
    run_id = new_run(store)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TRIGGER deny_evidence BEFORE INSERT ON evidence BEGIN SELECT RAISE(ABORT, 'write denied'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="write denied"):
        evidence(store, run_id, run_status="succeeded")
    assert store.get_run(SCOPE, run_id).status == "starting"


def test_database_read_failure_propagates(store, db_path):
    run_id = new_run(store)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE evidence_uses")
        conn.execute("DROP TABLE evidence")
        conn.execute("DROP TABLE runs")
    with pytest.raises(sqlite3.OperationalError):
        store.get_run(SCOPE, run_id)


def test_budget_snapshot_is_structured_unverified_and_not_enforced(store):
    run_id = new_run(store)
    store.save_budget_snapshot(
        SCOPE,
        run_id,
        limits={"tool_calls": 1},
        consumed={"tool_calls": 2},
        stop_reason="limit_reached",
    )
    run = store.get_run(SCOPE, run_id)
    assert run.budget_snapshot == {
        "limits": {"tool_calls": 1},
        "consumed": {"tool_calls": 2},
        "stop_reason": "limit_reached",
        "unverified": True,
    }
    assert run.status == "starting"
    with pytest.raises(ValueError):
        store.save_budget_snapshot(SCOPE, run_id, limits={"api_token": 1}, consumed={})
    with pytest.raises(ValueError):
        store.save_budget_snapshot(
            SCOPE, run_id, limits={}, consumed={}, stop_reason="Bearer hidden"
        )


def test_numeric_token_counters_are_valid_budget_observations(store):
    run_id = new_run(store)
    counters = {
        "tokens": 60,
        "input_tokens": 40,
        "output_tokens": 20,
        "cached_tokens": 10,
    }
    store.save_budget_snapshot(SCOPE, run_id, limits={"tokens": 100}, consumed=counters)
    assert store.get_run(SCOPE, run_id).budget_snapshot["consumed"] == counters
    with pytest.raises(ValueError):
        store.save_budget_snapshot(
            SCOPE, run_id, limits={}, consumed={"tokens": "raw-secret"}
        )


def test_private_filesystem_modes_are_created_and_rechecked(store, db_path):
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    run_id = new_run(store)
    db_path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        store.get_run(SCOPE, run_id)
    db_path.chmod(0o600)
    db_path.parent.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        ExecutionStore(db_path)


def test_symlinks_and_existing_shared_directories_are_rejected(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        ExecutionStore(shared / "runs.db")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    symlink = tmp_path / "link"
    symlink.symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ExecutionStore(symlink / "runs.db")
    real = private / "real"
    real.write_text("")
    real.chmod(0o600)
    linked_db = private / "linked.db"
    linked_db.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        ExecutionStore(linked_db)
    with pytest.raises(ValueError):
        ExecutionStore("relative.sqlite3")


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_symlink_sqlite_sidecar_is_rejected(store, db_path, tmp_path, suffix):
    journal = Path(str(db_path) + suffix)
    journal.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="private"):
        store.create_run(SCOPE, "error_watch", "event-1")


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("kind", ["shared_permissions", "hardlink", "directory"])
def test_unsafe_sqlite_sidecar_is_rejected(store, db_path, suffix, kind):
    sidecar = Path(str(db_path) + suffix)
    if kind == "hardlink":
        linked_file = db_path.parent / "private-file"
        linked_file.touch(mode=0o600)
        os.link(linked_file, sidecar)
        assert db_path.stat().st_nlink == 1
        assert sidecar.stat().st_nlink == 2
    elif kind == "directory":
        sidecar.mkdir(mode=0o700)
    else:
        sidecar.touch(mode=0o644)
        sidecar.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        store.create_run(SCOPE, "error_watch", "event-1")


def test_journal_deleted_by_concurrent_commit_does_not_fail_evidence_write(
    store, db_path, monkeypatch
):
    run_id = new_run(store)
    journal = Path(str(db_path) + "-journal")
    original_check = ExecutionStore._check_private
    committed = False
    with sqlite3.connect(db_path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE runs SET config_version=? WHERE run_id=?", ("concurrent", run_id)
        )
        assert journal.exists()

        def commit_before_validation(path, *, directory=False, allow_unlinked=False):
            nonlocal committed
            if path == journal and not committed:
                writer.commit()
                committed = True
                assert not journal.exists()
            original_check(path, directory=directory, allow_unlinked=allow_unlinked)

        monkeypatch.setattr(
            ExecutionStore, "_check_private", staticmethod(commit_before_validation)
        )
        meta = evidence(store, run_id)
    assert committed
    assert store.get_run(SCOPE, run_id).config_version == "concurrent"
    assert store.read_evidence_page(SCOPE, run_id, meta["ref"], 1)["content"] == (
        "protected evidence"
    )


def test_missing_database_is_still_an_error(store, db_path):
    run_id = new_run(store)
    db_path.unlink()
    with pytest.raises(FileNotFoundError):
        store.get_run(SCOPE, run_id)
    assert not db_path.exists()


def test_unlinked_journal_snapshot_does_not_fail_evidence_write(
    store, db_path, monkeypatch
):
    run_id = new_run(store)
    journal = Path(str(db_path) + "-journal")
    original_lstat = Path.lstat
    committed = False
    with sqlite3.connect(db_path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE runs SET config_version=? WHERE run_id=?", ("concurrent", run_id)
        )

        def unlinked_snapshot(path):
            nonlocal committed
            info = original_lstat(path)
            if path == journal and not committed:
                writer.commit()
                committed = True
                assert not journal.exists()
                # macOS can return the removed inode with zero links from lstat.
                fields = list(info)
                fields[3] = 0
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(Path, "lstat", unlinked_snapshot)
        meta = evidence(store, run_id)
    assert committed
    assert store.get_run(SCOPE, run_id).config_version == "concurrent"
    assert store.read_evidence_page(SCOPE, run_id, meta["ref"], 1)["content"] == (
        "protected evidence"
    )


def test_unlinked_database_snapshot_is_still_rejected(store, db_path, monkeypatch):
    run_id = new_run(store)
    original_lstat = Path.lstat

    def unlinked_snapshot(path):
        info = original_lstat(path)
        if path == db_path:
            fields = list(info)
            fields[3] = 0
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", unlinked_snapshot)
    with pytest.raises(ValueError, match="private"):
        store.get_run(SCOPE, run_id)


@pytest.mark.parametrize("invalid", ["mode", "owner", "type"])
def test_unlinked_sidecar_still_requires_private_regular_file(
    store, db_path, monkeypatch, invalid
):
    sidecar = Path(str(db_path) + "-journal")
    sidecar.touch(mode=0o600)
    original_lstat = Path.lstat

    def unsafe_snapshot(path):
        info = original_lstat(path)
        if path == sidecar:
            fields = list(info)
            fields[3] = 0
            if invalid == "mode":
                fields[0] = stat.S_IFREG | 0o644
            elif invalid == "owner":
                fields[4] = os.getuid() + 1
            else:
                fields[0] = stat.S_IFLNK | 0o600
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", unsafe_snapshot)
    with pytest.raises(ValueError, match="private"):
        store.create_run(SCOPE, "error_watch", "event-1")


@pytest.mark.parametrize("mode", [0o777, 0o770])
def test_nonsticky_shared_ancestor_is_rejected_before_creating_storage(tmp_path, mode):
    ancestor = tmp_path / "shared"
    ancestor.mkdir()
    ancestor.chmod(mode)
    private = ancestor / "private"
    with pytest.raises(ValueError, match="ancestors"):
        ExecutionStore(private / "runs.db")
    assert not private.exists()


@pytest.mark.parametrize("mode", [0o1777, 0o1770])
@pytest.mark.parametrize("owner", [0, os.getuid()])
def test_sticky_shared_ancestor_preserves_private_storage_contract(
    tmp_path, mode, owner, monkeypatch
):
    ancestor = tmp_path / "shared"
    ancestor.mkdir()
    ancestor.chmod(mode)
    original_lstat = Path.lstat

    def owned_lstat(path):
        info = original_lstat(path)
        if path == ancestor:
            fields = list(info)
            fields[4] = owner
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", owned_lstat)
    db_path = ancestor / "private" / "runs.db"
    store = ExecutionStore(db_path)
    run_id = new_run(store)
    assert ExecutionStore(db_path).get_run(SCOPE, run_id).status == "starting"
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o777, 0o770])
def test_ancestor_permissions_are_rechecked_before_each_connection(tmp_path, mode):
    ancestor = tmp_path / "shared"
    ancestor.mkdir(mode=0o755)
    store = ExecutionStore(ancestor / "private" / "runs.db")
    run_id = new_run(store)
    ancestor.chmod(mode)
    with pytest.raises(ValueError, match="ancestors"):
        store.get_run(SCOPE, run_id)
    with pytest.raises(ValueError, match="ancestors"):
        store.create_run(SCOPE, "error_watch", "new-event")
    ancestor.chmod(mode | stat.S_ISVTX)
    assert store.get_run(SCOPE, run_id).status == "starting"


def test_foreign_owner_ancestor_is_rejected_even_when_sticky(tmp_path, monkeypatch):
    ancestor = tmp_path / "shared"
    ancestor.mkdir()
    ancestor.chmod(0o1777)
    original_lstat = Path.lstat

    def foreign_lstat(path):
        info = original_lstat(path)
        if path == ancestor:
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_lstat)
    with pytest.raises(ValueError, match="ancestors"):
        ExecutionStore(ancestor / "private" / "runs.db")


def test_foreign_owner_is_rechecked_for_existing_storage(tmp_path, monkeypatch):
    ancestor = tmp_path / "shared"
    ancestor.mkdir(mode=0o755)
    store = ExecutionStore(ancestor / "private" / "runs.db")
    run_id = new_run(store)
    original_lstat = Path.lstat

    def foreign_lstat(path):
        info = original_lstat(path)
        if path == ancestor:
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_lstat)
    with pytest.raises(ValueError, match="ancestors"):
        store.get_run(SCOPE, run_id)


@pytest.mark.parametrize(
    "name,value",
    [("max_evidence_bytes", 0), ("retention_seconds", -1), ("retention_seconds", True)],
)
def test_invalid_storage_limits_are_rejected(db_path, name, value):
    with pytest.raises(ValueError):
        ExecutionStore(db_path, **{name: value})


def test_hardlinked_database_is_rejected(store, db_path, tmp_path):
    os.link(db_path, tmp_path / "duplicate")
    with pytest.raises(ValueError):
        store.create_run(SCOPE, "error_watch", "event-1")
