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

"""Cloud ops team: a leader agent and five specialists.

root (cloud_ops_team) ─┬─ error_watch     : error triage, root-cause analysis, issue filing
                       ├─ security_audit  : monthly security / hygiene / EOL / threat-intel audit
                       ├─ pr_review       : weekly verification of fixes (PR, CI, recurrence)
                       ├─ access_watch    : daily investigation of suspicious access
                       └─ infrastructure_review : weekly efficiency and configuration review

Specialists are defined in ``app/specialists/`` (one module each) and built
here; add one by copying ``app/specialists/_template.py`` and registering it.

The cloud is pluggable (``OPS_CLOUD_PROVIDER=gcp|aws|azure``): specialists use
cloud-neutral tools that delegate to the selected provider
(``app/providers``). Specialists are ``mode="single_turn"`` sub-agents: ADK
exposes each one to the leader as a tool and runs it *inline* in the
leader's session, so every tool call streams as an event and shows up in
traces, while one headless trigger (Cloud Scheduler →
``/apps/app/trigger/pubsub``) still yields one consolidated report.
"""

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app import prompts
from app.config import CONFIG
from app.providers import get_provider
from app.specialists import SPECIALISTS
from app.specialists.base import Specialist
from app.tools import get_run_context

MODEL = CONFIG.model
CLOUD = get_provider().profile.display_name


def _model() -> Gemini:
    return Gemini(
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


def build_specialist(specialist: Specialist) -> Agent:
    """Turn a specialist definition (app/specialists) into an ADK sub-agent."""
    return Agent(
        name=specialist.name,
        model=_model(),
        description=prompts.description_for(specialist),
        instruction=prompts.instruction_for(specialist),
        tools=list(specialist.tools),
        mode="single_turn",
        generate_content_config=_gen_config(specialist.max_output_tokens),
    )


root_agent = Agent(
    # Keep in sync with agents-cli-manifest.yaml: agents-cli derives this name
    # from the project `name:` recorded there, and telemetry reports it as
    # gen_ai.agent.name. Renaming the agent only here makes the two disagree,
    # and anything selecting traces by name stops finding this agent's.
    name="cloud_ops_team",
    model=_model(),
    description=f"{CLOUD} 運用保守チームのリーダー。ジョブを専門家に割り振り、統合レポートを返す。",
    instruction=prompts.root_instruction,
    tools=[get_run_context],
    sub_agents=[build_specialist(specialist) for specialist in SPECIALISTS],
    generate_content_config=_gen_config(),
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
