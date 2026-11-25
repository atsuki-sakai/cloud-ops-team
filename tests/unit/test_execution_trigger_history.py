"""Actual ADK trigger/auth/Runner path with SQLite and offline analysis."""

import asyncio
import base64
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from google.adk.agents import BaseAgent
from google.adk.cli.api_server import ApiServer
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.genai import types
from pydantic import Field

from app import execution_history, tools
from app.app_utils.execution_identity import (
    ExecutionIdentityMiddleware,
    current_delivery_identity,
)
from app.core.execution_store import ExecutionScope, ExecutionStore
from app.execution_history import current_execution
from app.routing import ScheduledRouter
from app.specialists import SPECIALISTS
from app.workflows import prepare_job


class NoModel(BaseLlm):
    async def generate_content_async(self, llm_request, stream=False):
        raise AssertionError("Offline trigger tests must not invoke any model")
        yield


class OfflineAnalysis(BaseAgent):
    reader: Callable = Field(exclude=True)

    async def _run_async_impl(self, ctx):
        self.reader()
        yield Event(
            author=self.name,
            content=types.Content(
                role="model", parts=[types.Part.from_text(text="Offline report")]
            ),
        )


@pytest.fixture
def trigger_app(monkeypatch, tmp_path):
    config = replace(
        execution_history.CONFIG,
        provider="gcp",
        target="trigger-fixture",
        execution_db_path=str(tmp_path / "private" / "history.sqlite3"),
    )
    monkeypatch.setattr(execution_history, "CONFIG", config)
    calls = []
    monkeypatch.setattr(
        tools,
        "get_run_context",
        lambda: {
            "status": "ok",
            "provider": config.provider,
            "target": config.target,
        },
    )
    monkeypatch.setattr(
        tools,
        "investigate_access",
        lambda **kwargs: {
            "status": "ok",
            "complete": True,
            "findings": [],
        },
    )

    async def factory(specialist, job, params):
        calls.append(
            (
                job.name,
                current_delivery_identity().event_id
                if current_delivery_identity()
                else None,
            )
        )
        prepared = await asyncio.to_thread(prepare_job, specialist, job, params)
        return OfflineAnalysis(
            name=specialist.name, reader=prepared.analysis_specialist.tools[0]
        )

    root = ScheduledRouter(
        name="cloud_ops_team",
        model=NoModel(model="offline-only"),
        specialists=tuple(SPECIALISTS),
        job_runner_factory=factory,
    )

    async def get_runner(server, app_name):
        return Runner(
            agent=root, app_name=app_name, session_service=server.session_service
        )

    async def verifier(request):
        if request.headers.get("authorization") != "Bearer offline-fixture":
            raise HTTPException(status_code=401)

    monkeypatch.setattr(ApiServer, "get_runner_async", get_runner)
    app = get_fast_api_app(
        agents_dir=".",
        web=False,
        trigger_sources=["pubsub"],
        trigger_auth_verifier=verifier,
        otel_to_cloud=False,
    )
    app.add_middleware(ExecutionIdentityMiddleware, enabled=True)
    return app, config, calls


def payload(
    message_id="delivery-1", *, subscription="projects/example/subscriptions/ops"
):
    value = {
        "message": {
            "messageId": message_id,
            "data": base64.b64encode(b'{"job":"access_watch"}').decode(),
        }
    }
    if subscription is not None:
        value["subscription"] = subscription
    return value


@pytest.mark.asyncio
async def test_real_trigger_rejects_before_execution_and_deduplicates_delivery(
    trigger_app,
):
    app, config, calls = trigger_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://offline"
    ) as client:
        rejected = await client.post("/apps/app/trigger/pubsub", json=payload())
        assert rejected.status_code == 401 and calls == []
        assert not Path(config.execution_db_path).exists()
        for _ in range(2):
            accepted = await client.post(
                "/apps/app/trigger/pubsub",
                json=payload(),
                headers={"authorization": "Bearer offline-fixture"},
            )
            assert (
                accepted.status_code == 200 and accepted.json()["status"] == "success"
            )
    assert len(calls) == 1
    with sqlite3.connect(config.execution_db_path) as conn:
        runs = conn.execute(
            "SELECT run_id,status,trigger,event_id FROM runs"
        ).fetchall()
    assert len(runs) == 1 and runs[0][1:3] == ("succeeded", "pubsub")
    assert runs[0][3] == calls[0][1]
    store = ExecutionStore(config.execution_db_path)
    metadata = store.list_evidence_metadata(
        ExecutionScope(config.provider, config.target), runs[0][0]
    )
    assert {row["source"] for row in metadata} >= {
        "collector/investigate_access",
        "presented/get_collected_evidence",
        "final_report",
    }
    assert current_delivery_identity() is None and current_execution() is None


@pytest.mark.asyncio
async def test_real_trigger_without_subscription_uses_new_invocations(trigger_app):
    app, config, calls = trigger_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline"
    ) as client:
        for _ in range(2):
            response = await client.post(
                "/apps/app/trigger/pubsub",
                json=payload(subscription=None),
                headers={"authorization": "Bearer offline-fixture"},
            )
            assert response.status_code == 200
    assert len(calls) == 2 and all(delivery is None for _, delivery in calls)
    with sqlite3.connect(config.execution_db_path) as conn:
        rows = conn.execute("SELECT trigger,event_id,status FROM runs").fetchall()
    assert len(rows) == 2 and rows[0][1] != rows[1][1]
    assert all(row[0] == "invocation" and row[2] == "succeeded" for row in rows)


@pytest.mark.asyncio
async def test_real_scheduler_headers_record_original_schedule_and_stop_retry(
    trigger_app,
):
    app, config, calls = trigger_app
    headers = {
        "authorization": "Bearer offline-fixture",
        "X-CloudScheduler-JobName": "projects/example/locations/asia/jobs/access-watch",
        "X-CloudScheduler-ScheduleTime": "2026-10-19T00:00:00Z",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline"
    ) as client:
        for _ in range(2):
            response = await client.post(
                "/apps/app/trigger/pubsub", json=payload(), headers=headers
            )
            assert response.status_code == 200
    assert len(calls) == 1
    with sqlite3.connect(config.execution_db_path) as conn:
        rows = conn.execute("SELECT trigger,scheduled_at,status FROM runs").fetchall()
    assert rows == [("scheduler", 1792368000.0, "succeeded")]
