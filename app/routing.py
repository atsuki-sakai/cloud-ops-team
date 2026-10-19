"""Validated, deterministic routing for scheduled jobs at every ADK entry point.

Only natural-language requests enter the conversation leader. Structured requests
are resolved against the registered job identifiers without a model decision.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from google.adk.agents import Agent, BaseAgent
from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import Field

from app.config import CONFIG
from app.core.run_budget import invocation_budget
from app.execution_history import begin_execution, bind_execution
from app.specialists.base import Job, Specialist

logger = logging.getLogger(__name__)

# A new job must declare a request schema as well as register a specialist. An
# unknown schema never falls back to another job or a model-picked default.
JOB_PARAMETER_BOUNDS: dict[str, dict[str, tuple[int, int]]] = {
    "error_watch": {"hours": (1, 168)},
    "security_audit": {"kev_days": (1, 365)},
    "pr_review": {"days": (1, 90)},
    "access_watch": {"hours": (1, 168)},
    "infrastructure_review": {},
}


class JobRequestError(ValueError):
    """A rejected structured request; its message contains no user-provided data."""


@dataclass(frozen=True)
class RoutedJob:
    """One registered job and its fully validated, canonical arguments."""

    specialist: Specialist
    job: Job
    params: Mapping[str, int]

    def content(self) -> types.Content:
        return types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text=json.dumps({"job": self.job.name, **self.params})
                )
            ],
        )


JobRunnerFactory = Callable[
    [Specialist, Job, Mapping[str, int]], BaseAgent | Awaitable[BaseAgent]
]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise JobRequestError("Duplicate JSON fields are not allowed.")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise JobRequestError("Non-finite JSON values are not allowed.")


def _registry(specialists: Sequence[Specialist]) -> dict[str, tuple[Specialist, Job]]:
    result: dict[str, tuple[Specialist, Job]] = {}
    for specialist in specialists:
        for job in specialist.jobs:
            for identifier in job.ids:
                if identifier == "full" or identifier in result:
                    raise ValueError(
                        "Job identifiers must be unique and cannot be full."
                    )
                result[identifier] = (specialist, job)
    return result


def normalize_job_params(job: Job, supplied: Mapping[str, object]) -> dict[str, int]:
    """Validate request arguments before any collection or model/tool execution."""
    bounds = JOB_PARAMETER_BOUNDS.get(job.name)
    if bounds is None or set(bounds) != set(job.params):
        raise JobRequestError("The requested job has no registered parameter schema.")
    if set(supplied) - set(bounds):
        raise JobRequestError("The job request contains unsupported fields.")
    params: dict[str, int] = {}
    for name, (minimum, maximum) in bounds.items():
        default = (
            CONFIG.error_lookback_hours
            if job.name == "error_watch" and name == "hours"
            else job.scheduled_params.get(name)
        )
        value = supplied.get(name, default)
        # bool is an int subclass; neither bool nor numeric strings are accepted.
        if type(value) is not int or not minimum <= value <= maximum:
            raise JobRequestError("A job parameter has an invalid type or range.")
        params[name] = value
    return params


def _normalize_job(
    specialist: Specialist, job: Job, supplied: Mapping[str, object]
) -> RoutedJob:
    params = normalize_job_params(
        job, {name: supplied[name] for name in job.params if name in supplied}
    )
    return RoutedJob(specialist, job, MappingProxyType(params))


def parse_job_request(
    content: types.Content | None,
    specialists: Sequence[Specialist],
    *,
    allow_full: bool = True,
) -> tuple[RoutedJob, ...] | None:
    """Return validated jobs, or None for plain natural language.

    The caller must pass the current invocation's content, never session history.
    Invalid JSON-like input is rejected instead of falling into the LLM path.
    Both direct job JSON and the existing ``data``/``attributes`` envelope work.
    """
    if content is not None and content.role != "user":
        raise JobRequestError("A user request is required.")
    parts = content.parts if content else None
    if not parts or len(parts) != 1 or not parts[0].text:
        raise JobRequestError("A single text request is required.")
    if set(parts[0].model_dump(exclude_none=True)) != {"text"}:
        raise JobRequestError("Only a plain text request is accepted.")
    text = parts[0].text.strip()
    if not text:
        raise JobRequestError("A non-empty request is required.")
    try:
        data = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except json.JSONDecodeError as exc:
        if text.startswith(("{", "[", '"', "```")):
            raise JobRequestError("The structured request is not valid JSON.") from exc
        return None
    if not isinstance(data, dict):
        raise JobRequestError("The structured request must be a JSON object.")
    if "data" in data:
        if set(data) - {"data", "attributes"}:
            raise JobRequestError("The request envelope contains unsupported fields.")
        attributes = data.get("attributes", {})
        if not isinstance(attributes, dict) or any(
            not isinstance(value, str) for value in attributes.values()
        ):
            raise JobRequestError("Envelope attributes must be a string map.")
        data = data["data"]
        if not isinstance(data, dict):
            raise JobRequestError("Envelope data must be a JSON object.")
    identifier = data.get("job")
    if not isinstance(identifier, str) or not identifier:
        raise JobRequestError("An explicit job identifier is required.")
    registry = _registry(specialists)
    if identifier == "full":
        if not allow_full:
            raise JobRequestError(
                "This specialist accepts only its own job identifiers."
            )
        allowed = {"job", "hours", "days", "kev_days"}
        if set(data) - allowed:
            raise JobRequestError("The job request contains unsupported fields.")
        jobs = [
            (specialist, job) for specialist in specialists for job in specialist.jobs
        ]
    else:
        if identifier not in registry:
            raise JobRequestError("The job identifier is not registered.")
        specialist, job = registry[identifier]
        if set(data) - {"job", *job.params}:
            raise JobRequestError("The job request contains unsupported fields.")
        jobs = [(specialist, job)]
    return tuple(_normalize_job(specialist, job, data) for specialist, job in jobs)


def _text(event: Event) -> str:
    return (
        "".join(
            part.text
            for part in (event.content.parts or [])
            if part.text and not part.thought
        )
        if event.content
        else ""
    )


class ScheduledRouter(Agent):
    """Programmatic root; the optional conversation tree cannot bypass this root.

    The Agent base selects ADK 2's node runtime (event queue and inline agent
    tools). This override never invokes that base class's model execution.
    Agents used for individual jobs and for conversation are deliberately not
    exposed as persistent ``sub_agents``. ADK's session-resume router would
    otherwise be able to select a previous conversation child before this
    validation boundary runs on the next scheduled invocation.
    """

    specialists: tuple[Specialist, ...] = Field(exclude=True)
    job_runner_factory: JobRunnerFactory = Field(exclude=True)
    conversation_agent: BaseAgent | None = Field(default=None, exclude=True)
    allow_full: bool = True

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        # Session history is isolated per stage, but safety limits belong to
        # this whole invocation, including concurrently delegated adapters.
        with invocation_budget(ctx.invocation_id):
            async with aclosing(self._dispatch_async(ctx)) as events:
                async for event in events:
                    yield event

    async def _dispatch_async(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        try:
            jobs = parse_job_request(
                ctx.user_content, self.specialists, allow_full=self.allow_full
            )
        except JobRequestError as exc:
            self._audit(ctx, "job_rejected", code="invalid_job_request")
            yield self._result(
                ctx,
                json.dumps({"status": "rejected", "reason": str(exc)}),
                error_code="invalid_job_request",
            )
            return
        if jobs is None:
            if self.conversation_agent is None:
                self._audit(ctx, "job_rejected", code="invalid_job_request")
                yield self._result(
                    ctx,
                    json.dumps(
                        {
                            "status": "rejected",
                            "reason": "A JSON job request is required.",
                        }
                    ),
                    error_code="invalid_job_request",
                )
                return
            final_text = ""
            async with aclosing(self.conversation_agent.run_async(ctx)) as events:
                async for event in events:
                    if event.is_final_response() and _text(event):
                        final_text = _text(event)
                    yield event
            if final_text:
                yield self._result(ctx, final_text)
            return

        reports: list[str] = []
        completed: list[str] = []
        for routed in jobs:
            self._audit(
                ctx,
                "job_dispatched",
                job=routed.job.name,
                specialist=routed.specialist.name,
                params=dict(routed.params),
            )
            execution = None
            try:
                execution = begin_execution(
                    ctx.invocation_id, routed.job.name, routed.params
                )
                if execution is not None and not execution.created:
                    yield self._result(
                        ctx,
                        json.dumps(
                            {
                                "status": "duplicate",
                                "job": routed.job.name,
                                "run_id": execution.run.run_id,
                                "run_status": execution.run.status,
                                "completed_jobs": completed,
                            }
                        ),
                    )
                    return
                with bind_execution(execution):
                    agent = self.job_runner_factory(
                        routed.specialist, routed.job, routed.params
                    )
                    if inspect.isawaitable(agent):
                        agent = await agent
                    isolated = self._job_context(ctx, routed)
                    report = ""
                    async with aclosing(agent.run_async(isolated)) as events:
                        async for event in events:
                            # Keep only this job's events for its next model turn.
                            if not event.partial:
                                isolated.session.events.append(event)
                                isolated.session.state.update(event.actions.state_delta)
                            if event.error_code:
                                raise RuntimeError(
                                    "The selected job returned an error."
                                )
                            if (
                                not event.partial
                                and event.is_final_response()
                                and _text(event)
                            ):
                                report = _text(event)
                            yield event
                    if not report.strip():
                        raise RuntimeError("The selected job produced no final report.")
                    if execution is not None:
                        execution.ensure_active()
                        status = (
                            "report_failed"
                            if execution.report_failed
                            else "partial"
                            if execution.partial or execution.evidence_read_required
                            else "succeeded"
                        )
                        execution.finish(
                            status, report=report, reason="stream_completed"
                        )
            except (asyncio.CancelledError, GeneratorExit):
                if execution is not None:
                    try:
                        execution.finish("interrupted", reason="cancelled")
                    except Exception:
                        self._audit(
                            ctx, "history_failed", code="history_finalization_failed"
                        )
                raise
            except Exception as exc:
                if execution is not None:
                    try:
                        execution.finish(
                            "timed_out" if isinstance(exc, TimeoutError) else "failed",
                            reason="deadline"
                            if isinstance(exc, TimeoutError)
                            else "execution_error",
                        )
                    except Exception:
                        self._audit(
                            ctx, "history_failed", code="history_finalization_failed"
                        )
                self._audit(
                    ctx,
                    "job_failed",
                    job=routed.job.name,
                    code="job_execution_failed",
                )
                yield self._result(
                    ctx,
                    json.dumps(
                        {
                            "status": "error",
                            "job": routed.job.name,
                            "completed_jobs": completed,
                        }
                    ),
                    error_code="job_execution_failed",
                )
                return
            completed.append(routed.job.name)
            reports.append(
                report if len(jobs) == 1 else f"### {routed.job.name}\n\n{report}"
            )
        yield self._result(ctx, "\n\n".join(reports))

    def _audit(self, ctx: InvocationContext, event: str, **details: Any) -> None:
        logger.info(
            json.dumps(
                {
                    "event": event,
                    "invocation_id": ctx.invocation_id,
                    "router": self.name,
                    **details,
                },
                sort_keys=True,
            )
        )

    def _result(
        self, ctx: InvocationContext, text: str, *, error_code: str | None = None
    ) -> Event:
        return Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(
                role="model", parts=[types.Part.from_text(text=text)]
            ),
            error_code=error_code,
        )

    @staticmethod
    def _job_context(ctx: InvocationContext, routed: RoutedJob) -> InvocationContext:
        content = routed.content()
        session = ctx.session.model_copy(
            update={
                "state": {},
                "events": [
                    Event(
                        author="user", invocation_id=ctx.invocation_id, content=content
                    )
                ],
            }
        )
        return ctx.model_copy(
            update={
                "user_content": content,
                "session": session,
                "agent_states": {},
                "end_of_agents": {},
                "end_invocation": False,
                "canonical_tools_cache": None,
            }
        )


class ScheduledJobAdapter(ScheduledRouter):
    """A conversation tool with no model and no authority to run another job.

    ADK 2 wraps any BaseAgent declaring single_turn mode as an inline agent
    tool, so collector/analysis events remain visible in the parent's stream.
    """

    mode: Literal["single_turn"] = "single_turn"
    allow_full: Literal[False] = False

    async def _run_impl(
        self, *, ctx: Context, node_input: Any
    ) -> AsyncGenerator[Event, None]:
        """Use this tool invocation's input, never the leader's original text.

        ADK's standard single-turn LLM wrapper appends node input to the
        conversation history but leaves InvocationContext.user_content pointing
        to the outer user request. A deterministic adapter consumes node_input
        directly and does not reconstruct its request from session events.
        """
        current = (
            types.Content(role="user", parts=[types.Part.from_text(text=node_input)])
            if isinstance(node_input, str)
            else None
        )
        invocation = ctx.get_invocation_context().model_copy(
            update={"agent": self, "user_content": current}
        )
        async with aclosing(self.run_async(invocation)) as events:
            async for event in events:
                if event.author:
                    ctx.event_author = event.author
                if event.author == self.name and event.is_final_response():
                    event.output = _text(event) or None
                yield event


def conversation_tool_guard(
    adapters: Sequence[ScheduledJobAdapter],
) -> Callable[..., dict[str, str] | None]:
    """Reject extra outer tool arguments before ADK's adapter consumes request."""
    allowed = {adapter.name for adapter in adapters}

    def before_tool_callback(tool: Any, args: dict, tool_context: Any):
        if getattr(tool, "name", None) not in allowed:
            return {"status": "rejected", "reason": "workflow_not_allowed"}
        if not isinstance(args, dict) or set(args) != {"request"}:
            return {"status": "rejected", "reason": "invalid_workflow_arguments"}
        if not isinstance(args["request"], str):
            return {
                "status": "rejected",
                "reason": "workflow_request_must_be_json_text",
            }
        return None

    return before_tool_callback
