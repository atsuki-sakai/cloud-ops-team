"""Run one ops job in-process and print every tool call (development helper).

Unlike ``agents-cli run`` this does not go through HTTP/SSE, so long jobs do
not hit client read timeouts, and a tracing plugin shows what the specialists
(sub-agents) are doing while the leader waits.

    uv run python scripts/run_job.py error_watch --hours 72
    uv run python scripts/run_job.py weekly_pr_review --days 7
    uv run python scripts/run_job.py monthly_security_audit --kev-days 30
    uv run python scripts/run_job.py --text "直近のエラーを見て"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from google.adk.apps import App
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import root_agent


def _short(value: object, n: int = 260) -> str:
    text = (
        json.dumps(value, ensure_ascii=False, default=str)
        if not isinstance(value, str)
        else value
    )
    return text if len(text) <= n else text[:n] + "…"


class DevTracePlugin(BasePlugin):
    """Print tool calls/results of every agent (including AgentTool children)."""

    def __init__(self) -> None:
        super().__init__(name="dev_trace")
        self.t0 = time.time()

    def _stamp(self) -> str:
        return f"[{time.time() - self.t0:6.1f}s]"

    async def before_tool_callback(self, **kw):  # type: ignore[override]
        tool, args, ctx = kw.get("tool"), kw.get("tool_args"), kw.get("tool_context")
        agent = getattr(ctx, "agent_name", "?")
        print(
            f"{self._stamp()} {agent} → {getattr(tool, 'name', tool)}({_short(args, 200)})",
            flush=True,
        )
        return None

    async def after_tool_callback(self, **kw):  # type: ignore[override]
        tool, ctx, result = kw.get("tool"), kw.get("tool_context"), kw.get("result")
        agent = getattr(ctx, "agent_name", "?")
        status = result.get("status") if isinstance(result, dict) else None
        print(
            f"{self._stamp()} {agent} ← {getattr(tool, 'name', tool)} status={status} {_short(result, 220)}",
            flush=True,
        )
        return None

    async def on_tool_error_callback(self, **kw):  # type: ignore[override]
        print(
            f"{self._stamp()} !! tool error {getattr(kw.get('tool'), 'name', '?')}: {kw.get('error')}",
            flush=True,
        )
        return None


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "job",
        nargs="?",
        default="error_watch",
        help="error_watch | weekly_pr_review | monthly_security_audit | full",
    )
    parser.add_argument("--hours", type=int)
    parser.add_argument("--days", type=int)
    parser.add_argument("--kev-days", type=int)
    parser.add_argument(
        "--text", help="free-form message instead of a JSON job payload"
    )
    args = parser.parse_args()

    if args.text:
        message = args.text
    else:
        data: dict[str, object] = {"job": args.job}
        if args.hours:
            data["hours"] = args.hours
        if args.days:
            data["days"] = args.days
        if args.kev_days:
            data["kev_days"] = args.kev_days
        message = json.dumps({"data": data, "attributes": {}}, ensure_ascii=False)

    adk_app = App(name="app", root_agent=root_agent, plugins=[DevTracePlugin()])
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name=adk_app.name, user_id="local-dev"
    )
    runner = Runner(app=adk_app, session_service=session_service)

    print(f"▶ message: {message}\n", flush=True)
    t0 = time.time()
    final_text: list[str] = []
    async for event in runner.run_async(
        user_id="local-dev",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=message)]
        ),
    ):
        if not event.content or not event.content.parts:
            continue
        for part in event.content.parts:
            if part.text and event.author == root_agent.name and not part.thought:
                final_text.append(part.text)
    print("\n===== FINAL REPORT =====\n", flush=True)
    print("".join(final_text).strip())
    print(f"\n(elapsed {time.time() - t0:.0f}s)")


if __name__ == "__main__":
    asyncio.run(main())
