"""Job-ledger contracts with actual SQLite and offline agents/collectors."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from functools import wraps
from threading import Barrier
from threading import Event as ThreadEvent

import pytest
from google.adk.agents import Agent, BaseAgent
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import Field

from app import execution_history, tools
from app.app_utils.execution_identity import DeliveryIdentity, bind_delivery_identity
from app.core.execution_store import ExecutionScope, ExecutionStore
from app.execution_history import begin_execution, bind_execution, current_execution
from app.routing import ScheduledJobAdapter, ScheduledRouter, conversation_tool_guard
from app.specialists import SPECIALISTS
from app.tool_scope import build_tool_scope
from app.workflows import collection_steps, prepare_job


class OfflineAgent(BaseAgent):
    action: Callable | None = Field(default=None, exclude=True)
    mode: str = "ok"

    async def _run_async_impl(self, ctx):
        if self.action:
            result = self.action()
            if inspect.isawaitable(result):
                await result
        if self.mode == "empty":
            return
        if self.mode == "timeout":
            raise TimeoutError("password=should-never-be-recorded")
        if self.mode == "error_event":
            yield Event(author=self.name, error_code="offline_failure")
            return
        yield Event(
            author=self.name,
            partial=self.mode == "partial_only",
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="   "
                        if self.mode == "whitespace"
                        else "Offline final report password=hidden-value"
                    )
                ],
            ),
        )


class DelegateModel(BaseLlm):
    turns: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.turns += 1
        part = (
            types.Part.from_function_call(
                name="access_watch",
                args={"request": '{"job":"access_watch"}'},
            )
            if self.turns == 1
            else types.Part.from_text(text="Conversation complete")
        )
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


class AuditFailureModel(BaseLlm):
    attempted: list[str] = Field(default_factory=list)
    responses: list[dict] = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        self.responses.extend(
            [
                {
                    "name": part.function_response.name,
                    "response": part.function_response.response,
                }
                for content in llm_request.contents
                for part in content.parts or []
                if part.function_response
            ]
        )
        names = ("get_collected_evidence", "read_details", "publish_report")
        if len(self.attempted) < len(names):
            name = names[len(self.attempted)]
            self.attempted.append(name)
            part = types.Part.from_function_call(name=name, args={})
        else:
            part = types.Part.from_text(text="A final text cannot erase audit failure")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


@pytest.fixture
def history(monkeypatch, tmp_path):
    config = replace(
        execution_history.CONFIG,
        target="project-a",
        provider="gcp",
        execution_db_path=str(tmp_path / "private" / "history.sqlite3"),
    )
    monkeypatch.setattr(execution_history, "CONFIG", config)
    return config


def stored_runs(config):
    with sqlite3.connect(config.execution_db_path) as conn:
        return conn.execute(
            "SELECT run_id,job,status FROM runs ORDER BY started_at"
        ).fetchall()


def evidence_bodies(config, run_id):
    store = ExecutionStore(config.execution_db_path)
    scope = ExecutionScope(config.provider, config.target)
    bodies = {}
    for meta in store.list_evidence_metadata(scope, run_id):
        content, cursor = "", None
        while True:
            page = store.read_evidence_page(
                scope, run_id, meta["ref"], 1, cursor=cursor
            )
            content += page["content"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        bodies.setdefault(meta["source"], []).append((meta, content))
    return bodies


def fake_collectors(monkeypatch, config, *, overrides=None):
    seen = []
    names = {
        step.name
        for specialist in SPECIALISTS
        for job in specialist.jobs
        for step in collection_steps(job.name, job.params)
    }
    for name in names:

        def fake(_name=name, **kwargs):
            seen.append(_name)
            value = (overrides or {}).get(_name)
            if isinstance(value, Exception):
                raise value
            if value is not None:
                return value
            if _name == "get_run_context":
                return {"provider": config.provider, "target": config.target}
            return {"status": "ok", "complete": True}

        monkeypatch.setattr(tools, name, fake)
    return seen


async def run_request(factory, text='{"job":"access_watch"}', *, adapter=False):
    service = InMemorySessionService()
    session = await service.create_session(app_name="app", user_id="offline")
    conversation = None
    if adapter:
        access = next(s for s in SPECIALISTS if s.name == "access_watch")
        delegated = ScheduledJobAdapter(
            name=access.name,
            specialists=(access,),
            job_runner_factory=factory,
        )
        conversation = Agent(
            name="conversation_leader",
            model=DelegateModel(model="offline"),
            sub_agents=[delegated],
            before_tool_callback=conversation_tool_guard([delegated]),
        )
        text = "Delegate the registered job"
    router = ScheduledRouter(
        name="cloud_ops_team",
        specialists=tuple(SPECIALISTS),
        job_runner_factory=factory,
        conversation_agent=conversation,
    )
    runner = Runner(agent=router, app_name="app", session_service=service)
    return [
        event
        async for event in runner.run_async(
            user_id="offline",
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text=text)]
            ),
        )
    ]


def prepared_factory(*, read=True, mode="ok"):
    def factory(specialist, job, params):
        prepared = prepare_job(specialist, job, params)
        return OfflineAgent(
            name=specialist.name,
            mode=mode,
            action=prepared.analysis_specialist.tools[0] if read else None,
        )

    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", [False, True])
async def test_job_and_adapter_save_full_raw_and_only_actual_presentation(
    history, monkeypatch, adapter
):
    fake_collectors(monkeypatch, history)
    with bind_delivery_identity(
        DeliveryIdentity("event-1", "scheduler", 1234, "corr-1")
    ):
        events = await run_request(prepared_factory(), adapter=adapter)
    assert events[-1].error_code is None
    run_id, job, status = stored_runs(history)[0]
    assert (job, status) == ("access_watch", "succeeded")
    bodies = evidence_bodies(history, run_id)
    assert set(bodies) == {
        "collector/get_run_context",
        "collector/investigate_access",
        "presented/get_collected_evidence",
        "final_report",
        "execution_outcome",
    }
    assert "hidden-value" not in str(bodies)
    store = ExecutionStore(history.execution_db_path)
    scope = ExecutionScope("gcp", "project-a")
    run = store.get_run(scope, run_id)
    assert run.trigger == "scheduler" and run.scheduled_at == 1234
    assert run.correlation_id == "corr-1"
    assert run.ended_at is not None and run.ended_at >= run.started_at
    for source, entries in bodies.items():
        meta, _body = entries[0]
        uses = store.list_evidence_uses(scope, run_id, meta["ref"], 1)
        assert [use["purpose"] for use in uses] == (
            ["presented"] if source.startswith("presented/") else []
        )


@pytest.mark.asyncio
async def test_unread_raw_is_not_claimed_as_presented(history, monkeypatch):
    fake_collectors(monkeypatch, history)
    await run_request(prepared_factory(read=False))
    run_id, _, status = stored_runs(history)[0]
    assert status == "partial"
    bodies = evidence_bodies(history, run_id)
    assert "presented/get_collected_evidence" not in bodies
    outcome = json.loads(bodies["execution_outcome"][0][1])
    assert outcome["unverified"] and outcome["evidence_unpresented"]


@pytest.mark.asyncio
async def test_long_raw_is_saved_before_preview_and_secrets_are_protected(
    history, monkeypatch
):
    fake_collectors(
        monkeypatch,
        history,
        overrides={
            "investigate_access": {
                "status": "ok",
                "text": "x" * 80000 + "TAIL",
                "authorization": "private-auth",
                "secret": "private-secret",
                "url": "password=private-password",
                "count": 2**62 + 3,
                "enabled": True,
                "absent": None,
                "coverage": [{"complete": True, "scope": {"sampled": False}}],
            }
        },
    )
    await run_request(prepared_factory())
    run_id, _, status = stored_runs(history)[0]
    assert status == "partial"
    bodies = evidence_bodies(history, run_id)
    raw = bodies["collector/investigate_access"][0][1]
    presented = bodies["presented/get_collected_evidence"][0][1]
    assert "TAIL" in raw and "TAIL" not in presented
    parsed = json.loads(raw)
    assert parsed["count"] == 2**62 + 3 and type(parsed["count"]) is int
    assert parsed["enabled"] is True and parsed["absent"] is None
    assert parsed["coverage"] == [{"complete": True, "scope": {"sampled": False}}]
    assert all(
        secret not in str(bodies)
        for secret in ("private-auth", "private-secret", "private-password")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        {"status": "permission_denied"},
        {"value": float("nan")},
        {"value": object()},
        RuntimeError("password=secret"),
    ],
)
async def test_partial_invalid_and_collector_failure_are_safe(
    history, monkeypatch, value
):
    fake_collectors(monkeypatch, history, overrides={"investigate_access": value})
    await run_request(prepared_factory())
    run_id, _, status = stored_runs(history)[0]
    assert status == "partial"
    assert "password=secret" not in str(evidence_bodies(history, run_id))


@pytest.mark.asyncio
async def test_context_mismatch_stops_before_other_collectors(history, monkeypatch):
    seen = fake_collectors(
        monkeypatch,
        history,
        overrides={"get_run_context": {"provider": "gcp", "target": "project-b"}},
    )
    events = await run_request(prepared_factory())
    assert events[-1].error_code == "job_execution_failed"
    assert seen == ["get_run_context"] and stored_runs(history)[0][2] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("empty", "failed"),
        ("error_event", "failed"),
        ("timeout", "timed_out"),
        ("whitespace", "failed"),
        ("partial_only", "failed"),
    ],
)
async def test_non_successful_streams_have_terminal_records(history, mode, status):
    events = await run_request(
        lambda specialist, job, params: OfflineAgent(name=specialist.name, mode=mode)
    )
    assert events[-1].error_code == "job_execution_failed"
    run_id, _, actual = stored_runs(history)[0]
    assert actual == status
    assert "should-never-be-recorded" not in str(evidence_bodies(history, run_id))


@pytest.mark.asyncio
async def test_factory_failure_is_recorded_without_exception_text(history):
    def fail(*args):
        raise RuntimeError("password=private-factory-error")

    await run_request(fail)
    run_id, _, status = stored_runs(history)[0]
    assert status == "failed"
    assert "private-factory-error" not in str(evidence_bodies(history, run_id))


@pytest.mark.asyncio
async def test_report_failure_is_not_overwritten_by_final_text(history):
    def factory(specialist, job, params):
        def publish_report(body: str) -> dict:
            return {"status": "permission_denied"}

        scope = build_tool_scope(
            replace(specialist, tools=[publish_report]), phase="report"
        )
        return OfflineAgent(
            name=specialist.name, action=lambda: scope.tools[0](body="report")
        )

    events = await run_request(factory)
    assert events[-1].error_code is None
    assert stored_runs(history)[0][2] == "report_failed"


@pytest.mark.asyncio
async def test_duplicates_and_parallel_delivery_never_repeat_factory(history):
    calls = []

    def factory(specialist, job, params):
        calls.append(job.name)
        return OfflineAgent(name=specialist.name, action=lambda: asyncio.sleep(0.01))

    with bind_delivery_identity(DeliveryIdentity("event-1", "pubsub")):
        result = await asyncio.gather(run_request(factory), run_request(factory))
        third = await run_request(factory)
    assert calls == ["access_watch"] and len(stored_runs(history)) == 1
    texts = [run[-1].content.parts[0].text for run in result] + [
        third[-1].content.parts[0].text
    ]
    assert sum('"status": "duplicate"' in text for text in texts) == 2


@pytest.mark.asyncio
async def test_delivery_configuration_conflict_rejected(history, monkeypatch):
    calls = []

    def factory(specialist, job, params):
        calls.append(dict(params))
        return OfflineAgent(name=specialist.name)

    with bind_delivery_identity(DeliveryIdentity("same-event", "pubsub")):
        await run_request(factory)
        second = await run_request(factory, '{"job":"access_watch","hours":2}')
    assert len(calls) == 1 and second[-1].error_code == "job_execution_failed"


def test_provider_and_target_partition_same_delivery(history, monkeypatch):
    records = []
    with bind_delivery_identity(DeliveryIdentity("same-event", "pubsub")):
        for provider, target in (
            ("gcp", "project-a"),
            ("gcp", "project-b"),
            ("aws", "account-a"),
        ):
            monkeypatch.setattr(
                execution_history,
                "CONFIG",
                replace(history, provider=provider, target=target),
            )
            execution = begin_execution("invocation", "access_watch", {"hours": 24})
            assert execution.created
            records.append(execution.run.run_id)
    assert len(set(records)) == 3


@pytest.mark.asyncio
async def test_full_preserves_job_order(history, monkeypatch):
    fake_collectors(monkeypatch, history)
    await run_request(prepared_factory(), '{"job":"full"}')
    assert [job for _, job, _ in stored_runs(history)] == [
        job.name for specialist in SPECIALISTS for job in specialist.jobs
    ]
    assert {status for _, _, status in stored_runs(history)} == {"succeeded"}
    store = ExecutionStore(history.execution_db_path)
    records = [
        store.get_run(ExecutionScope(history.provider, history.target), run_id)
        for run_id, _, _ in stored_runs(history)
    ]
    assert len({record.correlation_id for record in records}) == 1
    assert {record.correlation_id for record in records} == {
        record.event_id for record in records
    }


@pytest.mark.asyncio
async def test_cancelled_thread_cannot_write_late_raw_or_start_next_collector(
    history, monkeypatch
):
    started, release, ended = ThreadEvent(), ThreadEvent(), ThreadEvent()
    seen = fake_collectors(monkeypatch, history)

    def context():
        started.set()
        release.wait(5)
        ended.set()
        return {"provider": "gcp", "target": "project-a"}

    monkeypatch.setattr(tools, "get_run_context", context)

    async def factory(specialist, job, params):
        prepared = await asyncio.to_thread(prepare_job, specialist, job, params)
        return OfflineAgent(
            name=specialist.name, action=prepared.analysis_specialist.tools[0]
        )

    task = asyncio.create_task(run_request(factory))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.to_thread(ended.wait, 3)
    await asyncio.sleep(0.05)
    run_id, _, status = stored_runs(history)[0]
    assert status == "interrupted" and seen == []
    assert set(evidence_bodies(history, run_id)) == {"execution_outcome"}


@pytest.mark.asyncio
async def test_bad_storage_configuration_fails_before_factory(history, monkeypatch):
    monkeypatch.setattr(
        execution_history,
        "CONFIG",
        replace(history, execution_db_path="relative.sqlite"),
    )
    calls = []
    events = await run_request(lambda *args: calls.append(args))
    assert not calls and events[-1].error_code == "job_execution_failed"


@pytest.mark.asyncio
async def test_raw_write_failure_fails_closed(history, monkeypatch):
    seen = fake_collectors(monkeypatch, history)
    original = ExecutionStore.put_evidence

    def put(self, *args, **kwargs):
        if args[2].startswith("collector/"):
            raise OSError("password=storage-private")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ExecutionStore, "put_evidence", put)
    events = await run_request(prepared_factory())
    assert events[-1].error_code == "job_execution_failed"
    assert seen == ["get_run_context"] and stored_runs(history)[0][2] == "failed"


def test_context_binding_resets_and_stopped_execution_rejects_work(history):
    execution = begin_execution("invocation", "access_watch", {"hours": 24})
    with bind_execution(execution):
        assert current_execution() is execution
    assert current_execution() is None
    execution.finish("interrupted", reason="cancelled")
    with pytest.raises(execution_history.ExecutionStopped):
        execution.record_json("collector/late", {"status": "ok"})


def test_metadata_listing_requires_matching_scope_and_has_no_bodies(history):
    execution = begin_execution("invocation", "access_watch", {"hours": 24})
    execution.record_json("collector/example", {"value": "private body"})
    reopened = ExecutionStore(history.execution_db_path)
    assert "private body" not in str(
        reopened.list_evidence_metadata(execution.scope, execution.run.run_id)
    )
    with pytest.raises(LookupError):
        reopened.list_evidence_metadata(
            ExecutionScope("gcp", "project-b"), execution.run.run_id
        )


@pytest.mark.asyncio
async def test_final_report_write_failure_cannot_commit_success(history, monkeypatch):
    original = ExecutionStore.put_evidence

    def put(self, *args, **kwargs):
        if args[2] == "final_report":
            raise OSError("password=report-storage-private")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ExecutionStore, "put_evidence", put)
    events = await run_request(
        lambda specialist, job, params: OfflineAgent(name=specialist.name)
    )
    assert events[-1].error_code == "job_execution_failed"
    run_id, _, status = stored_runs(history)[0]
    bodies = evidence_bodies(history, run_id)
    assert status == "failed" and "final_report" not in bodies
    outcome = json.loads(bodies["execution_outcome"][-1][1])
    assert outcome["storage_failed"] is True
    assert outcome["storage_error_type"] == "OSError"
    assert "report-storage-private" not in str(bodies)


@pytest.mark.parametrize("failed_source", ["execution_outcome", "final_report"])
def test_terminal_storage_failure_stops_work_and_retains_failure_on_retry(
    history, monkeypatch, failed_source
):
    execution = begin_execution("terminal-storage-failure", "access_watch", {})
    original = execution.store.put_evidence
    attempts = []
    failure = sqlite3.OperationalError("password=terminal-storage-private")

    def put(*args, **kwargs):
        source = args[2]
        attempts.append(source)
        if source == failed_source:
            raise failure
        return original(*args, **kwargs)

    monkeypatch.setattr(execution.store, "put_evidence", put)
    with pytest.raises(sqlite3.OperationalError) as caught:
        execution.finish("succeeded", report="Completed report", reason="completed")
    assert caught.value is failure
    assert execution.storage_failed is True
    assert execution.storage_error_type == "OperationalError"
    assert (
        execution.store.get_run(execution.scope, execution.run.run_id).status
        == "running"
    )
    failed_attempts = attempts.copy()
    with pytest.raises(execution_history.ExecutionStopped):
        execution.ensure_active()
    with pytest.raises(execution_history.ExecutionStopped):
        execution.record_json("collector/late", {"status": "ok"})
    with pytest.raises(execution_history.ExecutionStopped):
        execution.finish("succeeded", report="Late report", reason="completed")
    assert attempts == failed_attempts

    monkeypatch.setattr(execution.store, "put_evidence", original)
    execution.finish("failed", reason="execution_error")
    bodies = evidence_bodies(history, execution.run.run_id)
    outcome = json.loads(bodies["execution_outcome"][-1][1])
    assert outcome["storage_failed"] is True
    assert outcome["storage_error_type"] == "OperationalError"
    assert "terminal-storage-private" not in str(bodies)
    assert "final_report" not in bodies
    assert stored_runs(history)[0][2] == "failed"


def test_storage_diagnostics_do_not_record_custom_exception_names(history, monkeypatch):
    execution = begin_execution("private-exception-name", "access_watch", {})
    original = execution.store.put_evidence
    private_error = type("password=private-exception-name", (Exception,), {})

    def put(*args, **kwargs):
        raise private_error("password=private-exception-message")

    monkeypatch.setattr(execution.store, "put_evidence", put)
    with pytest.raises(private_error):
        execution.record_json("collector/private", {})
    assert execution.storage_error_type == "Exception"
    monkeypatch.setattr(execution.store, "put_evidence", original)
    execution.finish("failed", reason="execution_error")
    bodies = evidence_bodies(history, execution.run.run_id)
    outcome = json.loads(bodies["execution_outcome"][-1][1])
    assert outcome["storage_failed"] is True
    assert outcome["storage_error_type"] == "Exception"
    assert "private-exception-name" not in str(bodies)
    assert "private-exception-message" not in str(bodies)


@pytest.mark.asyncio
async def test_history_disabled_never_opens_a_store(history, monkeypatch):
    monkeypatch.setattr(
        execution_history, "CONFIG", replace(history, execution_db_path="")
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled history must not touch storage")

    monkeypatch.setattr(execution_history, "ExecutionStore", forbidden)
    events = await run_request(
        lambda specialist, job, params: OfflineAgent(name=specialist.name)
    )
    assert events[-1].error_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize("translate_error", [False, True])
async def test_audit_failure_sdk_abort_and_translated_error_paths(
    history, monkeypatch, translate_error
):
    fake_collectors(monkeypatch, history)
    writes, scopes = [], []
    model = AuditFailureModel(model="offline")
    original = ExecutionStore.put_evidence

    def put(self, *args, **kwargs):
        if args[2] == "tool/read_details":
            raise OSError("password=private-audit-error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ExecutionStore, "put_evidence", put)

    def factory(specialist, job, params):
        prepared = prepare_job(specialist, job, params)

        def read_details() -> dict:
            """Read additional offline evidence."""
            return {"status": "ok"}

        def publish_report() -> dict:
            """Publish the reviewed report."""
            writes.append(True)
            return {"status": "created"}

        scope = build_tool_scope(
            replace(
                specialist,
                tools=[
                    prepared.analysis_specialist.tools[0],
                    read_details,
                    publish_report,
                ],
            )
        )
        scopes.append(scope)
        reader, details, writer = scope.tools

        @wraps(details)
        def translated_details():
            # Explicitly model a host that turns a tool exception into a
            # response. The real ADK Agent still drives subsequent turns.
            try:
                return details()
            except OSError:
                return {"status": "error", "reason": "audit_persistence_failed"}

        return Agent(
            name=specialist.name,
            model=model,
            tools=[reader, translated_details if translate_error else details, writer],
            before_tool_callback=scope.before_tool_callback,
        )

    events = await run_request(factory)
    assert events[-1].error_code == "job_execution_failed"
    assert writes == [] and stored_runs(history)[0][2] == "failed"
    assert model.attempted == ["get_collected_evidence", "read_details"] + (
        ["publish_report"] if translate_error else []
    )
    failures = [
        response["response"]
        for response in model.responses
        if response["name"] == "read_details"
    ]
    assert failures == (
        [{"status": "error", "reason": "audit_persistence_failed"}]
        if translate_error
        else []
    )
    # The default SDK aborted at the OSError; the translated host allowed a
    # second write attempt, which the callback rejected before execution.
    # The direct wrapper retains the same stop boundary in either case.
    with pytest.raises(execution_history.ExecutionStopped):
        scopes[0].tools[-1]()
    assert writes == []


@pytest.mark.asyncio
async def test_pr_review_auth_error_remains_unverified(history, monkeypatch):
    fake_collectors(
        monkeypatch,
        history,
        overrides={
            "list_ops_issues": {
                "status": "auth_error",
                "message": "Permission unavailable",
            },
        },
    )
    await run_request(prepared_factory(), '{"job":"pr_review"}')
    run_id, _, status = stored_runs(history)[0]
    assert status == "partial"
    bodies = evidence_bodies(history, run_id)
    metadata, raw = bodies["collector/list_ops_issues"][0]
    assert not metadata["acquisition_complete"]
    assert json.loads(raw)["status"] == "auth_error"
    assert (
        json.loads(bodies["presented/get_collected_evidence"][0][1])["status"]
        == "partial"
    )


@pytest.mark.asyncio
async def test_full_first_duplicate_does_not_start_later_jobs(history):
    calls = []

    def factory(specialist, job, params):
        calls.append(job.name)
        return OfflineAgent(name=specialist.name)

    with bind_delivery_identity(DeliveryIdentity("event-full", "pubsub")):
        await run_request(factory, '{"job":"error_watch"}')
        duplicate = await run_request(factory, '{"job":"full"}')
    assert calls == ["error_watch"]
    assert len(stored_runs(history)) == 1
    assert json.loads(duplicate[-1].content.parts[0].text)["status"] == "duplicate"


class StreamingOfflineAgent(BaseAgent):
    async def _run_async_impl(self, ctx):
        yield Event(
            author=self.name,
            partial=True,
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="Collecting offline evidence")],
            ),
        )
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_runner_stream_close_interrupts_current_job_and_stops_full(history):
    calls = []

    def factory(specialist, job, params):
        calls.append(job.name)
        return StreamingOfflineAgent(name=specialist.name)

    service = InMemorySessionService()
    session = await service.create_session(app_name="app", user_id="offline")
    router = ScheduledRouter(
        name="cloud_ops_team",
        specialists=tuple(SPECIALISTS),
        job_runner_factory=factory,
    )
    runner = Runner(agent=router, app_name="app", session_service=service)
    stream = runner.run_async(
        user_id="offline",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text='{"job":"full"}')]
        ),
    )
    first = await anext(stream)
    assert first.partial
    await stream.aclose()
    # The real runner closes an execution task; let its async-generator
    # cleanup finish before checking the persistent terminal record.
    for _ in range(100):
        if stored_runs(history)[0][2] == "interrupted":
            break
        await asyncio.sleep(0.01)
    run_id, _, status = stored_runs(history)[0]
    assert status == "interrupted" and calls == ["error_watch"]
    assert "final_report" not in evidence_bodies(history, run_id)


class ParallelHistoryModel(BaseLlm):
    turns: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.turns += 1
        parts = (
            [
                types.Part.from_function_call(
                    name=name,
                    args={
                        "request": json.dumps({"job": name}),
                    },
                )
                for name in ("access_watch", "pr_review")
            ]
            if self.turns == 1
            else [types.Part.from_text(text="Parallel jobs ended")]
        )
        yield LlmResponse(content=types.Content(role="model", parts=parts))


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_one", [False, True])
async def test_parallel_adapters_isolate_run_context_and_stop_flags(
    history, monkeypatch, fail_one
):
    fake_collectors(monkeypatch, history)
    barrier = Barrier(2)
    contexts, run_jobs = {}, {}
    original_put = ExecutionStore.put_evidence

    def context():
        execution = current_execution()
        assert execution is not None
        barrier.wait(timeout=5)
        return {
            "provider": history.provider,
            "target": history.target,
            "marker": execution.run.run_id,
        }

    monkeypatch.setattr(tools, "get_run_context", context)

    def put(self, *args, **kwargs):
        if (
            fail_one
            and run_jobs.get(args[1]) == "access_watch"
            and args[2] == "collector/get_run_context"
        ):
            raise OSError("Offline audit storage failure")
        return original_put(self, *args, **kwargs)

    monkeypatch.setattr(ExecutionStore, "put_evidence", put)

    async def factory(specialist, job, params):
        execution = current_execution()
        assert execution is not None
        run_jobs[execution.run.run_id] = job.name
        contexts[job.name] = execution
        prepared = await asyncio.to_thread(prepare_job, specialist, job, params)
        assert current_execution() is execution
        return OfflineAgent(
            name=specialist.name, action=prepared.analysis_specialist.tools[0]
        )

    selected = [s for s in SPECIALISTS if s.name in {"access_watch", "pr_review"}]
    adapters = [
        ScheduledJobAdapter(name=s.name, specialists=(s,), job_runner_factory=factory)
        for s in selected
    ]
    leader = Agent(
        name="conversation_leader",
        model=ParallelHistoryModel(model="offline"),
        sub_agents=adapters,
        before_tool_callback=conversation_tool_guard(adapters),
    )
    router = ScheduledRouter(
        name="cloud_ops_team",
        specialists=tuple(SPECIALISTS),
        job_runner_factory=factory,
        conversation_agent=leader,
    )
    service = InMemorySessionService()
    session = await service.create_session(app_name="app", user_id="offline")
    runner = Runner(agent=router, app_name="app", session_service=service)
    outer = begin_execution("outer-invocation", "outer", {})
    with bind_execution(outer):
        _events = [
            event
            async for event in runner.run_async(
                user_id="offline",
                session_id=session.id,
                new_message=types.Content(
                    role="user", parts=[types.Part.from_text(text="Run both jobs")]
                ),
            )
        ]
        assert current_execution() is outer
    assert current_execution() is None
    assert contexts["access_watch"] is not contexts["pr_review"]
    for job, execution in contexts.items():
        actual = execution.store.get_run(execution.scope, execution.run.run_id)
        expected = "failed" if fail_one and job == "access_watch" else "succeeded"
        assert actual.status == expected
        assert execution.storage_failed == (fail_one and job == "access_watch")
        bodies = evidence_bodies(history, execution.run.run_id)
        if expected == "succeeded":
            assert (
                json.loads(bodies["collector/get_run_context"][0][1])["marker"]
                == execution.run.run_id
            )
            other = next(e.run.run_id for e in contexts.values() if e is not execution)
            assert other not in str(bodies)
    outer.finish("interrupted", reason="cancelled")
