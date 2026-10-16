"""Route real ADK Runner invocations without network or a routing model."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import Callable

import pytest
from google.adk.agents import Agent, BaseAgent
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import Field

from app.core.run_budget import current_run_budget
from app.routing import (
    JobRequestError,
    ScheduledJobAdapter,
    ScheduledRouter,
    conversation_tool_guard,
    normalize_job_params,
    parse_job_request,
)
from app.specialists import SPECIALISTS
from app.specialists.base import Job


def content(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


class RecordingAgent(BaseAgent):
    record: Callable = Field(exclude=True)

    async def _run_async_impl(self, ctx):
        self.record(
            self.name,
            ctx.user_content,
            list(ctx.session.events),
            dict(ctx.session.state),
        )
        yield Event(
            author=self.name,
            content=types.Content(
                role="model", parts=[types.Part.from_text(text=f"Report: {self.name}")]
            ),
        )


class NoRoutingModel(BaseLlm):
    async def generate_content_async(self, llm_request, stream=False):
        raise AssertionError("A structured job must never invoke the routing model.")
        yield  # This implements BaseLlm's asynchronous generator contract.


def make_router(calls, conversations=None, *, factory_override=None):
    def factory(specialist, job, params):
        calls.append((specialist.name, job.name, dict(params)))
        if factory_override:
            return factory_override(specialist, job, params)
        return RecordingAgent(name=specialist.name, record=lambda *args: None)

    return ScheduledRouter(
        name="cloud_ops_team",
        model=NoRoutingModel(model="forbidden-routing-model"),
        specialists=tuple(SPECIALISTS),
        job_runner_factory=factory,
        conversation_agent=RecordingAgent(
            name="conversation_leader",
            record=lambda *args: (
                conversations.append(args) if conversations is not None else None
            ),
        ),
    )


async def run_request(router, text, *, state=None):
    service = InMemorySessionService()
    session = await service.create_session(
        app_name="app", user_id="offline", state=state or {}
    )
    runner = Runner(agent=router, app_name="app", session_service=service)
    events = [
        event
        async for event in runner.run_async(
            user_id="offline", session_id=session.id, new_message=content(text)
        )
    ]
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identifier", "canonical", "params"),
    [
        ("error_watch", "error_watch", {"hours": 12}),
        ("access_watch", "access_watch", {"hours": 12}),
        ("pr_review", "pr_review", {"days": 9}),
        ("weekly_pr_review", "pr_review", {"days": 9}),
        ("security_audit", "security_audit", {"kev_days": 8}),
        ("monthly_security_audit", "security_audit", {"kev_days": 8}),
        ("infrastructure_review", "infrastructure_review", {}),
    ],
)
@pytest.mark.parametrize("enveloped", [False, True])
async def test_runner_routes_every_alias_without_calling_leader(
    identifier, canonical, params, enveloped
):
    calls, conversations = [], []
    router = make_router(calls, conversations)
    payload = {"job": identifier, **params}
    if enveloped:
        payload = {"data": payload, "attributes": {"note": "untrusted metadata"}}
    events = await run_request(router, json.dumps(payload))
    assert calls == [(canonical, canonical, params)]
    assert conversations == []
    assert events[-1].author == "cloud_ops_team"
    assert events[-1].content.parts[0].text == f"Report: {canonical}"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{}",
        "[]",
        "null",
        "true",
        '"error_watch"',
        '{"job":',
        '```json\n{"job":"error_watch"}\n```',
        '{"job":"unknown"}',
        '{"job":null}',
        '{"job":true}',
        '{"job":"error_watch","target":"other-project"}',
        '{"job":"error_watch","days":7}',
        '{"job":"infrastructure_review","hours":24}',
        '{"job":"full","target":"other-project"}',
        '{"job":"full","hours":169}',
        '{"job":"error_watch","hours":"24"}',
        '{"job":"error_watch","hours":24.0}',
        '{"job":"error_watch","hours":true}',
        '{"job":"error_watch","hours":NaN}',
        '{"job":"error_watch","hours":0}',
        '{"job":"access_watch","hours":169}',
        '{"job":"pr_review","days":366}',
        '{"job":"pr_review","days":91}',
        '{"job":"security_audit","kev_days":-1}',
        '{"job":"access_watch","job":"error_watch"}',
        '{"data":{"job":"error_watch"},"target":"other-project"}',
        '{"data":"{\\"job\\":\\"error_watch\\"}"}',
        '{"data":{"job":"error_watch"},"attributes":null}',
        '{"data":{"job":"error_watch"},"attributes":{"hours":72}}',
    ],
)
@pytest.mark.asyncio
async def test_invalid_request_never_runs_a_job_or_a_model(text):
    calls, conversations = [], []
    events = await run_request(make_router(calls, conversations), text)
    assert calls == [] and conversations == []
    assert events[-1].error_code == "invalid_job_request"
    assert json.loads(events[-1].content.parts[0].text)["status"] == "rejected"


@pytest.mark.asyncio
async def test_full_is_explicit_validated_and_runs_in_registration_order():
    calls = []
    events = await run_request(
        make_router(calls),
        json.dumps({"job": "full", "hours": 4, "days": 6, "kev_days": 10}),
    )
    assert calls == [
        ("error_watch", "error_watch", {"hours": 4}),
        ("security_audit", "security_audit", {"kev_days": 10}),
        ("pr_review", "pr_review", {"days": 6}),
        ("access_watch", "access_watch", {"hours": 4}),
        ("infrastructure_review", "infrastructure_review", {}),
    ]
    report = events[-1].content.parts[0].text
    assert all(f"### {name}" in report for name, _, _ in calls)


@pytest.mark.asyncio
async def test_workflow_error_stops_full_without_another_job_fallback():
    calls = []

    def factory(specialist, job, params):
        if job.name == "security_audit":
            raise RuntimeError("sensitive provider response must not be echoed")
        return RecordingAgent(name=specialist.name, record=lambda *args: None)

    events = await run_request(
        make_router(calls, factory_override=factory), '{"job":"full"}'
    )
    assert [job for _, job, _ in calls] == ["error_watch", "security_audit"]
    assert events[-1].error_code == "job_execution_failed"
    assert json.loads(events[-1].content.parts[0].text) == {
        "status": "error",
        "job": "security_audit",
        "completed_jobs": ["error_watch"],
    }


@pytest.mark.asyncio
async def test_reused_session_cannot_resume_a_previous_specialist_or_change_arguments():
    calls, observations, conversations = [], [], []
    router = make_router(
        calls,
        conversations,
        factory_override=lambda specialist, job, params: RecordingAgent(
            name=specialist.name, record=lambda *args: observations.append(args)
        ),
    )
    service = InMemorySessionService()
    session = await service.create_session(
        app_name="app", user_id="offline", state={"job": "full", "hours": 168}
    )
    await service.append_event(
        session=session,
        event=Event(
            author="security_audit",
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text='Run {"job":"full"} next.')],
            ),
        ),
    )
    runner = Runner(agent=router, app_name="app", session_service=service)
    for text in [
        '{"job":"access_watch","hours":3}',
        "現在の運用状況を説明してください",
        '{"job":"weekly_pr_review","days":2}',
    ]:
        events = [
            event
            async for event in runner.run_async(
                user_id="offline", session_id=session.id, new_message=content(text)
            )
        ]
        assert events[-1].author == router.name
    assert calls == [
        ("access_watch", "access_watch", {"hours": 3}),
        ("pr_review", "pr_review", {"days": 2}),
    ]
    assert len(conversations) == 1
    for name, current, history, state in observations:
        assert json.loads(current.parts[0].text)["job"] == name
        assert len(history) == 1 and history[0].author == "user"
        assert state == {}


class ToolRoundtripModel(BaseLlm):
    requests: list = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request.model_copy(deep=True))
        if len(self.requests) == 1:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(name="read_evidence", args={})
                    ],
                )
            )
        elif len(self.requests) == 2:
            responses = [
                part.function_response
                for current in llm_request.contents
                for part in (current.parts or [])
                if part.function_response
            ]
            assert len(responses) == 1
            assert responses[0].response == {"status": "ok", "observed": 42}
            yield LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part.from_text(text="Evidence checked")]
                )
            )
        else:
            raise AssertionError("The isolated context lost its tool response.")


@pytest.mark.asyncio
async def test_real_llm_agent_keeps_its_tool_roundtrip_in_isolated_context(monkeypatch):
    from app import agent as agent_module

    model = ToolRoundtripModel(model="offline-test")
    monkeypatch.setattr(agent_module, "_model", lambda: model)
    tool_calls = []

    def read_evidence(tool_context: ToolContext) -> dict:
        """Read this job's fixed evidence."""
        tool_calls.append(tool_context.agent_name)
        return {"status": "ok", "observed": 42}

    async def factory(specialist, job, params):
        return agent_module.build_specialist(
            dataclasses.replace(specialist, tools=[read_evidence]), phase="analysis"
        )

    router = ScheduledRouter(
        name="cloud_ops_team",
        specialists=tuple(SPECIALISTS),
        job_runner_factory=factory,
    )
    events = await run_request(router, '{"job":"access_watch","hours":2}')
    assert events[-1].error_code is None
    assert events[-1].content.parts[0].text == "Evidence checked"
    assert tool_calls == ["access_watch"] and len(model.requests) == 2
    assert len(model.requests[0].contents) == 1
    assert json.loads(model.requests[0].contents[0].parts[0].text) == {
        "job": "access_watch",
        "hours": 2,
    }


@pytest.mark.parametrize("name", ["error_watch", "access_watch"])
@pytest.mark.parametrize("hours", [1, 168])
def test_inclusive_hour_bounds(name, hours):
    jobs = parse_job_request(
        content(json.dumps({"job": name, "hours": hours})), SPECIALISTS
    )
    assert dict(jobs[0].params) == {"hours": hours}


def test_extra_target_rejected_at_the_public_parameter_boundary():
    job = SPECIALISTS[0].jobs[0]
    with pytest.raises(JobRequestError):
        normalize_job_params(job, {"hours": 24, "target": "other"})


def test_new_job_requires_a_schema_in_addition_to_registration():
    specialist = dataclasses.replace(SPECIALISTS[0], jobs=(Job(name="new_job"),))
    with pytest.raises(JobRequestError):
        parse_job_request(content('{"job":"new_job"}'), [specialist])


def test_duplicate_aliases_fail_closed():
    specialist = dataclasses.replace(
        SPECIALISTS[0], jobs=(Job(name="new_job", aliases=("error_watch",)),)
    )
    with pytest.raises(ValueError):
        parse_job_request(content('{"job":"error_watch"}'), [*SPECIALISTS, specialist])


def test_multimodal_or_multiple_parts_do_not_enter_any_model():
    messages = [
        types.Content(
            role="user",
            parts=[types.Part.from_function_response(name="x", response={})],
        ),
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text="{}"),
                types.Part.from_text(text="run full"),
            ],
        ),
        types.Content(
            role="model", parts=[types.Part.from_text(text='{"job":"full"}')]
        ),
    ]
    for message in messages:
        with pytest.raises(JobRequestError):
            parse_job_request(message, SPECIALISTS)


class ConversationModel(BaseLlm):
    delegated_request: str
    extra_args: dict = Field(default_factory=dict)
    requests: list = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request.model_copy(deep=True))
        if len(self.requests) == 1:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="access_watch",
                            args={"request": self.delegated_request, **self.extra_args},
                        )
                    ],
                )
            )
        elif len(self.requests) == 2:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text="Conversation response")],
                )
            )
        else:
            raise AssertionError("Unexpected conversation model invocation")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delegated_text", "accepted"),
    [
        ('{"job":"access_watch","hours":2}', True),
        ('{"job":"error_watch","hours":2}', False),
        ('{"job":"full"}', False),
        ('{"job":"access_watch","hours":"2"}', False),
        ('{"job":"access_watch","target":"other"}', False),
        ("不審なアクセスを調査してください", False),
    ],
)
async def test_conversation_adapter_runs_the_same_workflow_and_rejects_scope_expansion(
    delegated_text, accepted
):
    workflow_calls, contexts = [], []

    async def factory(specialist, job, params):
        workflow_calls.append((specialist.name, job.name, dict(params)))
        return RecordingAgent(
            name=specialist.name, record=lambda *args: contexts.append(args)
        )

    access = next(s for s in SPECIALISTS if s.name == "access_watch")
    adapter = ScheduledJobAdapter(
        name=access.name,
        specialists=(access,),
        job_runner_factory=factory,
    )
    model = ConversationModel(model="offline-test", delegated_request=delegated_text)
    leader = Agent(
        name="conversation_leader",
        model=model,
        instruction="Choose the specialist's fixed workflow, then summarize.",
        sub_agents=[adapter],
        before_tool_callback=conversation_tool_guard([adapter]),
    )
    router = ScheduledRouter(
        name="cloud_ops_team",
        specialists=tuple(SPECIALISTS),
        job_runner_factory=factory,
        conversation_agent=leader,
    )
    events = await run_request(router, "本日の不審アクセスを確認してください")
    assert events[-1].author == router.name
    assert len(model.requests) == 2
    assert [tool.name for tool in leader.tools] == ["access_watch"]
    if accepted:
        assert workflow_calls == [("access_watch", "access_watch", {"hours": 2})]
        assert len(contexts[0][2]) == 1 and contexts[0][3] == {}
        assert any(
            event.content
            and event.content.parts
            and event.content.parts[0].text == "Report: access_watch"
            for event in events
        ), "The analysis report must stream through the conversation adapter."
    else:
        assert workflow_calls == [] and contexts == []


@pytest.mark.asyncio
async def test_conversation_outer_tool_arguments_cannot_override_the_target():
    workflow_calls = []
    access = next(s for s in SPECIALISTS if s.name == "access_watch")
    adapter = ScheduledJobAdapter(
        name=access.name,
        specialists=(access,),
        job_runner_factory=lambda *args: workflow_calls.append(args),
    )
    model = ConversationModel(
        model="offline-test",
        delegated_request='{"job":"access_watch"}',
        extra_args={"target": "other-project"},
    )
    leader = Agent(
        name="conversation_leader",
        model=model,
        sub_agents=[adapter],
        before_tool_callback=conversation_tool_guard([adapter]),
    )
    router = ScheduledRouter(
        name="cloud_ops_team",
        specialists=tuple(SPECIALISTS),
        job_runner_factory=lambda *args: workflow_calls.append(args),
        conversation_agent=leader,
    )
    await run_request(router, "不審アクセスを確認してください")
    assert workflow_calls == []
    responses = [
        part.function_response.response
        for current in model.requests[-1].contents
        for part in (current.parts or [])
        if part.function_response
    ]
    assert responses == [{"status": "rejected", "reason": "invalid_workflow_arguments"}]


def test_production_conversation_exposes_only_fixed_job_adapters():
    from app.agent import conversation_leader, root_agent

    assert root_agent.sub_agents == []
    assert len(conversation_leader.tools) == len(SPECIALISTS)
    assert {tool.name for tool in conversation_leader.tools} == {
        specialist.name for specialist in SPECIALISTS
    }
    assert all(
        isinstance(adapter, ScheduledJobAdapter)
        and adapter.conversation_agent is None
        and adapter.allow_full is False
        for adapter in conversation_leader.sub_agents
    )


@pytest.mark.asyncio
async def test_routing_logs_validated_arguments_and_rejection_code_without_input(
    caplog,
):
    caplog.set_level(logging.INFO, logger="app.routing")
    calls = []
    await run_request(make_router(calls), '{"job":"access_watch","hours":3}')
    await run_request(
        make_router(calls),
        '{"job":"access_watch","target":"must-not-appear-in-logs"}',
    )
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "app.routing"
    ]
    assert len(records) == 2
    assert records[0]["event"] == "job_dispatched"
    assert records[0]["job"] == "access_watch"
    assert records[0]["params"] == {"hours": 3}
    assert records[0]["invocation_id"]
    assert records[1]["event"] == "job_rejected"
    assert records[1]["code"] == "invalid_job_request"
    assert "must-not-appear-in-logs" not in json.dumps(records)


class IssueBudgetAgent(BaseAgent):
    evidence_reader: Callable = Field(exclude=True)
    issue_writer: Callable = Field(exclude=True)
    observed: Callable = Field(exclude=True)

    async def _run_async_impl(self, ctx):
        # Yield so simultaneously delegated adapters can all prepare their
        # scopes before any issue writer consumes the shared budget.
        await asyncio.sleep(0)
        self.evidence_reader()
        tool_context = ToolContext(ctx)
        result = await asyncio.to_thread(
            self.issue_writer,
            kind="security",
            title=f"finding from {self.name}",
            body="Offline evidence",
            severity="high",
            fingerprint=f"offline-{self.name}",
            service=self.name,
            search_text="",
            tool_context=tool_context,
        )
        self.observed(self.name, result)
        yield Event(
            author=self.name,
            content=types.Content(
                role="model", parts=[types.Part.from_text(text=json.dumps(result))]
            ),
            actions=tool_context.actions,
        )


def issue_budget_factory(monkeypatch, observed, budgets):
    from app.tool_scope import build_tool_scope
    from app.tools import issues

    monkeypatch.setattr(
        issues,
        "CONFIG",
        dataclasses.replace(issues.CONFIG, dry_run=True, max_issues_per_run=1),
    )
    monkeypatch.setattr(
        issues, "find_existing_ops_issue", lambda fingerprint: {"status": "not_found"}
    )

    def no_network(*args, **kwargs):
        raise AssertionError("A dry run must not write to GitHub.")

    monkeypatch.setattr(issues, "gh_rest", no_network)

    def factory(specialist, job, params):
        budgets.append(current_run_budget())

        def get_collected_evidence() -> dict:
            """Read the fixed evidence for this isolated analysis."""
            return {"status": "ok", "job": job.name}

        scope = build_tool_scope(
            dataclasses.replace(
                specialist, tools=[get_collected_evidence, issues.create_ops_issue]
            ),
            phase="analysis",
        )
        reader, writer = scope.tools
        return IssueBudgetAgent(
            name=specialist.name,
            evidence_reader=reader,
            issue_writer=writer,
            observed=lambda *args: observed.append(args),
        )

    return factory


@pytest.mark.asyncio
async def test_full_has_one_shared_issue_cap_and_new_invocation_resets_it(monkeypatch):
    observed, budgets = [], []
    factory = issue_budget_factory(monkeypatch, observed, budgets)
    router = ScheduledRouter(
        name="cloud_ops_team",
        specialists=tuple(SPECIALISTS),
        job_runner_factory=factory,
    )
    service = InMemorySessionService()
    session = await service.create_session(
        app_name="app",
        user_id="offline",
        state={"ops:issues_created": 999, "poison": True},
    )
    runner = Runner(agent=router, app_name="app", session_service=service)
    for run_index in range(2):
        events = [
            event
            async for event in runner.run_async(
                user_id="offline",
                session_id=session.id,
                new_message=content('{"job":"full"}'),
            )
        ]
        assert events[-1].error_code is None
        results = [
            result for _, result in observed[run_index * 5 : (run_index + 1) * 5]
        ]
        assert [result["status"] for result in results].count("dry_run") == 1
        assert [result["status"] for result in results].count("skipped") == 4
        assert results[0]["action"] == "create"
        assert all(
            budget is budgets[run_index * 5]
            for budget in budgets[run_index * 5 : (run_index + 1) * 5]
        )
        assert budgets[run_index * 5].issues_created == 1
        assert current_run_budget() is None
    assert budgets[0] is not budgets[5]
    assert budgets[0].invocation_id != budgets[5].invocation_id


class ParallelDelegationModel(BaseLlm):
    requests: list = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request.model_copy(deep=True))
        if len(self.requests) == 1:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name=specialist.name,
                            args={
                                "request": json.dumps({"job": specialist.jobs[0].name})
                            },
                        )
                        for specialist in SPECIALISTS
                    ],
                )
            )
        else:
            yield LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part.from_text(text="Combined report")]
                )
            )


@pytest.mark.asyncio
async def test_parallel_conversation_adapters_share_the_invocation_issue_cap(
    monkeypatch,
):
    observed, budgets = [], []
    factory = issue_budget_factory(monkeypatch, observed, budgets)
    adapters = [
        ScheduledJobAdapter(
            name=specialist.name,
            specialists=(specialist,),
            job_runner_factory=factory,
        )
        for specialist in SPECIALISTS
    ]
    model = ParallelDelegationModel(model="offline-test")
    leader = Agent(
        name="conversation_leader",
        model=model,
        sub_agents=adapters,
        before_tool_callback=conversation_tool_guard(adapters),
    )
    router = ScheduledRouter(
        name="cloud_ops_team",
        specialists=tuple(SPECIALISTS),
        job_runner_factory=factory,
        conversation_agent=leader,
    )
    events = await run_request(router, "各担当で調査してください")
    assert events[-1].error_code is None
    assert len(observed) == 5 and len(model.requests) == 2
    assert [result["status"] for _, result in observed].count("dry_run") == 1
    assert [result["status"] for _, result in observed].count("skipped") == 4
    assert budgets[0] is not None and all(budget is budgets[0] for budget in budgets)
    assert budgets[0].issues_created == 1
    assert current_run_budget() is None
