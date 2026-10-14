"""Building blocks for specialists and their jobs.

A *specialist* is one member of the team (an ADK sub-agent): a role, an
instruction, a tool list and the *jobs* it handles. The leader's routing
table and the Cloud Scheduler jobs are generated from these definitions, so
adding a specialist or a job is a matter of adding a module and registering
it in ``app/specialists/__init__.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Job:
    """A unit of work the leader can be asked to do (by the scheduler or a human).

    Attributes:
        name: Job id in the request, e.g. ``{"data": {"job": "error_watch"}}``.
        aliases: Other accepted ids (e.g. ``weekly_pr_review``).
        params: Parameter -> how the leader picks its value, shown in the routing table.
        schedule: Default cron expression; empty means "on demand only".
        schedule_key: Scheduler job suffix, e.g. ``error-watch`` -> ``cloud-ops-error-watch``.
        scheduled_name: Job id sent by the scheduler (defaults to ``name``).
        scheduled_params: Parameter values sent by the scheduler.
        cron_env: Environment variable overriding ``schedule``
            (default ``<SCHEDULE_KEY>_CRON``, e.g. ``COST_REPORT_CRON``).
        param_env: Parameter -> environment variable overriding its scheduled value.
    """

    name: str
    aliases: tuple[str, ...] = ()
    params: Mapping[str, str] = field(default_factory=dict)
    schedule: str = ""
    schedule_key: str = ""
    scheduled_name: str = ""
    scheduled_params: Mapping[str, Any] = field(default_factory=dict)
    cron_env: str = ""
    param_env: Mapping[str, str] = field(default_factory=dict)

    @property
    def ids(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def cron(self, env: Mapping[str, str] = os.environ) -> str:
        var = self.cron_env or f"{self.schedule_key.upper().replace('-', '_')}_CRON"
        return (env.get(var) or self.schedule).strip()

    def payload(self, env: Mapping[str, str] = os.environ) -> str:
        """JSON body the scheduler sends as ``data``."""
        data: dict[str, Any] = {"job": self.scheduled_name or self.name}
        for param, default in self.scheduled_params.items():
            raw = (
                env.get(self.param_env.get(param, ""), "")
                if self.param_env.get(param)
                else ""
            )
            data[param] = (
                (int(raw) if raw.strip().isdigit() else raw.strip())
                if raw.strip()
                else default
            )
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class Specialist:
    """One member of the team.

    Attributes:
        name: Agent name (also the tool name the leader calls). snake_case.
        role: One line shown to the leader, e.g. "エラー監視・原因調査・ISSUE 起票（SRE）".
        description: Tool description the leader reads; may use prompt placeholders
            such as ``__PROVIDER__``.
        instruction: Prompt template (placeholders: ``__PROVIDER__``, ``__TARGET__``,
            ``__TARGET_LABEL__``, ``__REGION__``, ``__REPO__``, ``__LOOKBACK__``,
            ``__STACK__``, ``__CLI__``, ``__LANG__``). The shared tool glossary, output
            language and project context are appended automatically.
        tools: Functions the specialist may call (see ``app/tools``).
        jobs: Jobs routed to this specialist.
        max_output_tokens: Output budget per model call.
    """

    name: str
    role: str
    description: str
    instruction: str
    tools: list[Callable[..., Any]]
    jobs: tuple[Job, ...] = ()
    max_output_tokens: int = 8192
