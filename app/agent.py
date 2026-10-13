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

"""GCP 運用保守エージェントチーム.

root (gcp_ops_team) ─┬─ error_watch     : エラー監視・原因調査・ISSUE 起票
                     ├─ security_audit  : 月次セキュリティ / 健全性 / 最新化 / 脅威動向の診断 → ISSUE
                     │     └─ web_threat_research ツール（Google 検索グラウンディング、90 秒上限）
                     └─ pr_review       : 週次の ISSUE/PR 進捗確認・完了検証

Specialists are ``mode="single_turn"`` sub-agents: ADK exposes each one to
the leader as a tool and runs it *inline* in the leader's session, so every
tool call streams as an event (keeps SSE / Pub/Sub push connections alive and
shows up in traces) while a single headless trigger
(Cloud Scheduler → Pub/Sub → ``/apps/app/trigger/pubsub``) still yields one
consolidated report.
"""

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app import prompts
from app.tools import (
    ERROR_WATCH_TOOLS,
    PR_REVIEW_TOOLS,
    SECURITY_AUDIT_TOOLS,
    get_run_context,
)

MODEL = "gemini-3.8-flash"


def _model() -> Gemini:
    return Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
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
            "SRE: Cloud Logging / Cloud Run / Cloud Scheduler / Monitoring を調査してエラーをトリアージし、"
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
            "クラウドセキュリティ担当: IAM / SA 鍵 / Cloud Run / Cloud SQL / ファイアウォール / SCC / イメージ脆弱性 / "
            "EOL / CISA KEV / Web 脅威動向（Google 検索）を月次診断し、critical・high を ISSUE 起票、月次レポートを発行する。"
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
    name="gcp_ops_team",
    model=_model(),
    description="GCP 運用保守チームのリーダー。ジョブ（error_watch / security_audit / pr_review）を専門家に割り振り、統合レポートを返す。",
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
