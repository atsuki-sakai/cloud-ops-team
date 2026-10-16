"""Per-stage tool exposure and argument checks enforced outside the model.

Tool names come from the supplied callables. The same checks protect ADK's
before-tool callback and direct calls of the exposed wrappers; prompts alone
are not an authorization boundary.
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import wraps
from threading import Event
from typing import Any

from google.adk.tools.tool_context import ToolContext

from app.config import CONFIG
from app.core.run_budget import (
    ISSUES_CREATED_STATE_KEY,
    RunBudget,
    current_run_budget,
)
from app.providers import CAPABILITIES
from app.specialists.base import Specialist

ToolFunction = Callable[..., Any]

_BOUNDS = {
    "hours": (1, 168),
    "window_minutes": (1, 120),
    "max_findings": (1, 200),
    "issue_number": (1, 2**31 - 1),
    "pr_number": (1, 2**31 - 1),
}
_LIMITS = {
    "list_error_logs": 200,
    "list_deployments": 20,
    "get_recent_workflow_runs": 30,
}
_DAYS = {
    "fetch_cisa_kev": (1, 365),
    "web_threat_research": (7, 90),
    "list_recent_pull_requests": (1, 90),
}
_TEXT_LIMITS = {
    "title": 256,
    "body": 65_536,
    "reason_comment": 65_536,
    "query": 1000,
    "search_text": 4000,
    "focus": 2000,
    "fingerprint": 1024,
    "service": 1024,
    "service_name": 1024,
    "trace": 1024,
    "branch": 256,
    "product": 100,
    "version": 100,
}


def tool_names(tools: Iterable[ToolFunction]) -> tuple[str, ...]:
    """Derive the names actually exposed to the model, preserving order."""
    return tuple(tool.__name__ for tool in tools)


def capabilities_for(tools: Iterable[ToolFunction]) -> tuple[str, ...]:
    """Cloud glossary/context entries for only these callable tools."""
    names = set(tool_names(tools))
    return tuple(name for name in CAPABILITIES if name in names)


def _reject(name: str, reason: str) -> dict[str, str]:
    # Do not reflect attacker-controlled arguments or credentials into reports.
    return {"status": "rejected", "tool": name, "reason": reason}


def _check_timestamp(value: str, *, empty_allowed: bool) -> bool:
    if not value:
        return empty_allowed
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if stamp.tzinfo is None:
        return False
    age = dt.datetime.now(dt.UTC) - stamp.astimezone(dt.UTC)
    return -dt.timedelta(minutes=5) <= age <= dt.timedelta(days=365)


@dataclass(frozen=True)
class ToolScope:
    """A stage's callable tools, model metadata and execution boundary."""

    specialist_name: str
    phase: str
    originals: tuple[ToolFunction, ...]
    target: str
    repository: str
    provider: str
    _run_budget: RunBudget | None = field(
        default_factory=current_run_budget, init=False, repr=False, compare=False
    )
    _evidence_read: Event = field(
        default_factory=Event, init=False, repr=False, compare=False
    )

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tool_names(self.originals)

    @property
    def capabilities(self) -> tuple[str, ...]:
        return capabilities_for(self.originals)

    def before_model_callback(self, callback_context: Any, llm_request: Any) -> None:
        """Record actual model-visible schema sizes, without content or arguments.

        A JSON line on stdout is structured by Cloud Run and also remains
        available in local runs. No prompt, schema text or tool result is logged.
        """
        tools = getattr(llm_request, "tools_dict", {})
        records = []

        def description_size(value: Any) -> int:
            if isinstance(value, dict):
                return sum(
                    len(item)
                    if key == "description" and isinstance(item, str)
                    else description_size(item)
                    for key, item in value.items()
                )
            if isinstance(value, list):
                return sum(description_size(item) for item in value)
            return 0

        for name, tool in tools.items():
            declaration = tool._get_declaration()
            schema = (
                declaration.model_dump(exclude_none=True)
                if declaration is not None
                else {}
            )
            records.append(
                {
                    "name": name,
                    "schema_characters": len(
                        json.dumps(schema, ensure_ascii=False, sort_keys=True)
                    ),
                    "description_characters": description_size(schema),
                }
            )
        payload = {
            "event": "model_tool_presentation",
            "invocation_id": getattr(callback_context, "invocation_id", None),
            "specialist": self.specialist_name,
            "phase": self.phase,
            "tool_names": [record["name"] for record in records],
            "tool_count": len(records),
            "schema_characters": sum(item["schema_characters"] for item in records),
            "description_characters": sum(
                item["description_characters"] for item in records
            ),
            "tools": records,
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    def validate(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        with_context: bool = False,
    ) -> dict[str, str] | None:
        """Reject unknown tools/arguments, excessive queries and scope escapes."""
        functions = {function.__name__: function for function in self.originals}
        function = functions.get(name)
        if function is None:
            return _reject(name, "tool_not_allowed_in_this_stage")
        if not isinstance(arguments, Mapping):
            return _reject(name, "arguments_must_be_an_object")
        if (
            self.phase == "analysis"
            and "get_collected_evidence" in functions
            and name != "get_collected_evidence"
            and not self._evidence_read.is_set()
        ):
            return _reject(name, "collected_evidence_must_be_read_first")
        parameters = inspect.signature(function).parameters
        if any(key not in parameters for key in arguments):
            return _reject(name, "unknown_argument")
        for key, parameter in parameters.items():
            if key == "tool_context":
                if key in arguments and (
                    not with_context or not isinstance(arguments[key], ToolContext)
                ):
                    return _reject(name, "tool_context_is_runtime_managed")
                continue
            if key not in arguments:
                if parameter.default is inspect.Parameter.empty:
                    return _reject(name, "missing_required_argument")
                continue
            value = arguments[key]
            annotation = parameter.annotation
            if annotation in (int, "int") and type(value) is not int:
                return _reject(name, "integer_argument_required")
            if annotation in (str, "str") and not isinstance(value, str):
                return _reject(name, "string_argument_required")
            if annotation in (list[str], "list[str]") and (
                not isinstance(value, list)
                or len(value) > 20
                or any(not isinstance(item, str) or len(item) > 100 for item in value)
            ):
                return _reject(name, "invalid_string_list")
            bounds = _BOUNDS.get(key)
            if key == "days":
                bounds = _DAYS.get(name)
            if key == "limit":
                bounds = (1, _LIMITS.get(name, 200))
            if bounds and (
                type(value) is not int or not bounds[0] <= value <= bounds[1]
            ):
                return _reject(name, "argument_outside_query_budget")
            if key in _TEXT_LIMITS and (
                not isinstance(value, str) or len(value) > _TEXT_LIMITS[key]
            ):
                return _reject(name, "text_argument_too_large")
            if key in ("since", "around_time") and not _check_timestamp(
                value, empty_allowed=key == "around_time"
            ):
                return _reject(name, "invalid_or_excessive_time_range")
            if key in ("service_name", "trace") and isinstance(value, str):
                # Full resource paths are permitted only inside the configured scope.
                projects = re.findall(r"(?:^|/)projects/([^/]+)", value)
                subscriptions = re.findall(
                    r"(?:^|/)subscriptions/([^/]+)", value, re.IGNORECASE
                )
                if any(project != self.target for project in projects) or any(
                    subscription.lower() != self.target.lower()
                    for subscription in subscriptions
                ):
                    return _reject(name, "resource_outside_configured_target")
                if self.provider == "aws" and value.startswith("arn:"):
                    pieces = value.split(":", 5)
                    if len(pieces) != 6:
                        return _reject(name, "invalid_resource_identifier")
                    account = pieces[4]
                    if account and account != self.target:
                        return _reject(name, "resource_outside_configured_target")
            if key == "query" and isinstance(value, str):
                # Quoted log text is literal; unquoted repository qualifiers can
                # expand GitHub search beyond the server-supplied repo qualifier.
                qualifiers = re.sub(r'"(?:\\.|[^"\\])*"', "", value)
                repositories = re.findall(
                    r"(?:^|\s)-?repo:\s*([^\s]+)", qualifiers, re.IGNORECASE
                )
                if any(
                    repo.lower().rstrip("/") != self.repository.lower()
                    for repo in repositories
                ) or re.search(r"(?:^|\s)(?:org|user):", qualifiers, re.IGNORECASE):
                    return _reject(name, "search_outside_configured_repository")
        return None

    def before_tool_callback(
        self, tool: Any, args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, str] | None:
        """ADK callback: a rejection skips the tool and becomes its response."""
        return self.validate(getattr(tool, "name", ""), args)

    def _wrap(self, function: ToolFunction) -> ToolFunction:
        def arguments_for(args: tuple[Any, ...], kwargs: dict[str, Any]):
            try:
                return inspect.signature(function).bind(*args, **kwargs).arguments
            except TypeError:
                return None

        def checked_result(result: Any) -> Any:
            if function.__name__ == "get_collected_evidence" and isinstance(
                result, dict
            ):
                # Only the real wrapper's completed read satisfies the prerequisite.
                # Partial/error evidence still has to be reportable; calling the
                # callback or starting an async read must never unlock other tools.
                self._evidence_read.set()
            if function.__name__ == "get_run_context" and isinstance(result, dict):
                result = dict(result)
                for key in ("capabilities", "unsupported_capabilities"):
                    result[key] = [
                        item
                        for item in result.get(key, [])
                        if item in self.capabilities
                    ]
            return result

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                arguments = arguments_for(args, kwargs)
                if arguments is None:
                    return _reject(function.__name__, "invalid_arguments")
                rejected = self.validate(
                    function.__name__, arguments, with_context=True
                )
                if rejected:
                    return rejected
                return checked_result(await function(*args, **kwargs))

            return async_wrapper

        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            arguments = arguments_for(args, kwargs)
            if arguments is None:
                return _reject(function.__name__, "invalid_arguments")
            rejected = self.validate(function.__name__, arguments, with_context=True)
            if rejected:
                return rejected
            if function.__name__ == "create_ops_issue" and self._run_budget is not None:
                # Keep the existing issue function's dry-run, duplicate and cap
                # behavior. Only its trusted count crosses the stage boundary;
                # all other state and conversation history remain isolated.
                context = arguments.get("tool_context")
                if not isinstance(context, ToolContext):
                    return _reject(function.__name__, "tool_context_is_runtime_managed")
                with self._run_budget.lock:
                    context.state[ISSUES_CREATED_STATE_KEY] = (
                        self._run_budget.issues_created
                    )
                    try:
                        result = function(*args, **kwargs)
                    finally:
                        self._run_budget.issues_created = max(
                            self._run_budget.issues_created,
                            int(context.state.get(ISSUES_CREATED_STATE_KEY, 0) or 0),
                        )
                    return checked_result(result)
            return checked_result(function(*args, **kwargs))

        if function.__name__ == "get_run_context":
            wrapper.__doc__ = (
                "Return current time, configured target/repository, dry-run flags "
                "and only the cloud capabilities available in this stage."
            )
        return wrapper

    @property
    def tools(self) -> tuple[ToolFunction, ...]:
        return tuple(self._wrap(function) for function in self.originals)


def build_tool_scope(
    specialist: Specialist,
    *,
    phase: str = "analysis",
    excluded_tools: Iterable[ToolFunction] = (),
    allowed_tools: Iterable[ToolFunction] | None = None,
) -> ToolScope:
    """Select from a specialist's actual callables, never a global registry.

    A prepared analysis specialist may add a zero-argument evidence reader and
    omit its fixed collectors before this function is called.
    """
    selected = tuple(specialist.tools if allowed_tools is None else allowed_tools)
    if any(function not in specialist.tools for function in selected):
        raise ValueError("A stage cannot add tools outside its specialist definition")
    excluded = tuple(excluded_tools)
    selected = tuple(function for function in selected if function not in excluded)
    names = tool_names(selected)
    if len(names) != len(set(names)):
        raise ValueError("Tool names must be unique within a stage")
    if any(
        parameter.kind
        in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
        for function in selected
        for parameter in inspect.signature(function).parameters.values()
    ):
        raise ValueError("Stage tools must expose explicit argument schemas")
    return ToolScope(
        specialist.name,
        phase,
        selected,
        CONFIG.target,
        CONFIG.github_repo,
        CONFIG.provider,
    )
