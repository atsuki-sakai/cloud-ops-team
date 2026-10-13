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

"""Cloud ops team: a leader agent and three specialists.

root (cloud_ops_team) ─┬─ error_watch     : error triage, root-cause analysis, issue filing
                       ├─ security_audit  : monthly security / hygiene / EOL / threat-intel audit
                       └─ pr_review       : weekly verification of fixes (PR, CI, recurrence)

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
from app.tools import (
    ERROR_WATCH_TOOLS,
    PR_REVIEW_TOOLS,
    SECURITY_AUDIT_TOOLS,
    get_run_context,
)

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


def create_error_watch_agent() -> Agent:
    return Agent(
        name="error_watch",
        model=_model(),
        description=(
            f"SRE: {CLOUD} のログ・サービス・スケジュールジョブ・メトリクスを調査してエラーをトリアージし、"
            "原因分析付きの GitHub ISSUE を起票（重複は再発コメント）する。request にはジョブ名・遡り時間・制約を書く。"
        ),
        instruction=prompts.error_watch_instruction,
        tools=ERROR_WATCH_TOOLS,
        mode="single_turn",
        generate_content_config=_gen_config(),
    )


def create_security_audit_agent() -> Agent:
    return Agent(
        name="security_audit",
        model=_model(),
        description=(
            f"クラウドセキュリティ担当: {CLOUD} の IAM / 認証情報 / コンピュート / データベース / ネットワーク / "
            "セキュリティサービス / イメージ脆弱性と、EOL・CISA KEV・Web 脅威動向を月次診断し、critical・high を ISSUE 起票、月次レポートを発行する。"
        ),
        instruction=prompts.security_audit_instruction,
        tools=SECURITY_AUDIT_TOOLS,
        mode="single_turn",
        generate_content_config=_gen_config(),
    )


def create_pr_review_agent() -> Agent:
    return Agent(
        name="pr_review",
        model=_model(),
        description=(
            "リリースマネージャー: チーム起票 ISSUE に紐づく PR のマージ状況・CI・ログ再発を検証し、"
            "完了確認コメント / 停滞ラベルを付け、週次レビューをレポート発行する。"
        ),
        instruction=prompts.pr_review_instruction,
        tools=PR_REVIEW_TOOLS,
        mode="single_turn",
        generate_content_config=_gen_config(),
    )


root_agent = Agent(
    # Keep in sync with agents-cli-manifest.yaml: agents-cli derives this name
    # from the project `name:` recorded there, and telemetry reports it as
    # gen_ai.agent.name. Renaming the agent only here makes the two disagree,
    # and anything selecting traces by name stops finding this agent's.
    name="cloud_ops_team",
    model=_model(),
    description=f"{CLOUD} 運用保守チームのリーダー。ジョブ（error_watch / security_audit / pr_review）を専門家に割り振り、統合レポートを返す。",
    instruction=prompts.root_instruction,
    tools=[get_run_context],
    sub_agents=[
        create_error_watch_agent(),
        create_security_audit_agent(),
        create_pr_review_agent(),
    ],
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
