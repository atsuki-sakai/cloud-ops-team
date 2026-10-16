# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Build the common ADK root, deterministic job workflows and conversation entry.

Scheduled JSON requests resolve through ``ScheduledRouter`` without an LLM
leader. Required evidence is collected by a fixed workflow before a specialist
receives only the tools and instructions for its analysis phase. Natural
language retains a separate, scoped conversation leader.
"""

import asyncio
from collections.abc import Mapping

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app import prompts
from app.config import CONFIG
from app.model import OpsGemini
from app.providers import get_provider
from app.routing import ScheduledJobAdapter, ScheduledRouter, conversation_tool_guard
from app.specialists import SPECIALISTS
from app.specialists.base import Job, Specialist
from app.tool_scope import build_tool_scope
from app.workflows import prepare_job

MODEL = CONFIG.model
CLOUD = get_provider().profile.display_name


def _model() -> Gemini:
    return OpsGemini(
        model=MODEL,
        # Vertex AI answers bursts with 429 RESOURCE_EXHAUSTED; back off instead of failing the run.
        retry_options=types.HttpRetryOptions(
            attempts=5, initial_delay=2.0, max_delay=30.0, exp_base=2.0
        ),
    )


def _gen_config(max_output_tokens: int = 8192) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        temperature=0.2, max_output_tokens=max_output_tokens
    )


def build_specialist(specialist: Specialist, *, phase: str = "legacy") -> Agent:
    """Turn a specialist definition (app/specialists) into an ADK sub-agent."""
    scope = build_tool_scope(specialist, phase=phase)
    return Agent(
        name=specialist.name,
        model=_model(),
        description=prompts.description_for(specialist),
        instruction=prompts.instruction_for(specialist, tools=scope.tools, phase=phase),
        tools=list(scope.tools),
        before_model_callback=scope.before_model_callback,
        before_tool_callback=scope.before_tool_callback,
        mode="single_turn",
        generate_content_config=_gen_config(specialist.max_output_tokens),
    )


async def build_scheduled_agent(
    specialist: Specialist, job: Job, params: Mapping[str, int]
) -> Agent:
    """Collect required evidence without blocking the server's event loop."""
    prepared = await asyncio.to_thread(prepare_job, specialist, job, params)
    return build_specialist(prepared.analysis_specialist, phase="analysis")


def build_job_adapter(specialist: Specialist) -> ScheduledJobAdapter:
    """Expose a fixed workflow, rather than cloud tools, to conversation."""
    jobs = ", ".join(identifier for job in specialist.jobs for identifier in job.ids)
    return ScheduledJobAdapter(
        name=specialist.name,
        description=(
            f"{specialist.role}。固定収集後に分析する。"
            f"request は job を明示した JSON 文字列のみ。受付 job: {jobs}。"
            "引数は担当の期間パラメーターだけ。自然文・別担当 job・full は受け付けない。"
        ),
        specialists=(specialist,),
        job_runner_factory=build_scheduled_agent,
    )


_conversation_adapters = [build_job_adapter(specialist) for specialist in SPECIALISTS]

conversation_leader = Agent(
    name="conversation_leader",
    model=_model(),
    description=f"{CLOUD} 運用保守の対話窓口。自然言語の依頼を固定ワークフローへ割り振る。",
    instruction=prompts.root_instruction,
    tools=[],
    sub_agents=_conversation_adapters,
    before_tool_callback=conversation_tool_guard(_conversation_adapters),
    generate_content_config=_gen_config(),
)


root_agent = ScheduledRouter(
    # Keep in sync with agents-cli-manifest.yaml: agents-cli derives this name
    # from the project `name:` recorded there, and telemetry reports it as
    # gen_ai.agent.name. Renaming the agent only here makes the two disagree,
    # and anything selecting traces by name stops finding this agent's.
    name="cloud_ops_team",
    model=_model(),  # Configuration is preserved; ScheduledRouter never calls it.
    description=f"{CLOUD} 運用保守。定期ジョブを検証して直接実行し、対話は専用窓口へ渡す。",
    specialists=tuple(SPECIALISTS),
    job_runner_factory=build_scheduled_agent,
    conversation_agent=conversation_leader,
)

app = App(
    root_agent=root_agent,
    name="app",
    # Each specialist swaps the system prompt + tool set; give every agent its own
    # prefix cache so long audits do not re-send the whole prompt uncached.
    context_cache_config=ContextCacheConfig(
        min_tokens=2048, ttl_seconds=1800, cache_intervals=10
    ),
)
