"""Exercise the real SDK retry boundary with an in-memory HTTP transport."""

import asyncio
import json

import httpx
import pytest
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import errors, types

from app.model import MODEL_HTTP_TIMEOUT_MS, OpsGemini, _EnvironmentTransport


def _model(handler, *, attempts=5):
    return OpsGemini(
        model="gemini-fixture",
        retry_options=types.HttpRetryOptions(
            attempts=attempts, initial_delay=0.001, max_delay=0.001, jitter=0.001
        ),
        client_kwargs={
            "api_key": "fixture-key",
            "vertexai": False,
            "enterprise": False,
            "http_options": {
                "headers": {"x-fixture": "preserved"},
                "async_client_args": {"transport": httpx.MockTransport(handler)},
            },
        },
    )


def _response(part):
    return {"candidates": [{"content": {"role": "model", "parts": [part]}}]}


@pytest.mark.asyncio
async def test_timeout_retry_does_not_replay_the_report_tool():
    requests = []
    report_calls = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("synthetic idle timeout", request=request)
        part = (
            {"functionCall": {"name": "publish_report", "args": {"body": "ok"}}}
            if len(requests) == 2
            else {"text": "done"}
        )
        return httpx.Response(200, json=_response(part))

    def publish_report(body: str) -> dict:
        """Publish a synthetic report."""
        report_calls.append(body)
        return {"status": "ok"}

    model = _model(handler)
    sessions = InMemorySessionService()
    session = await sessions.create_session(app_name="fixture", user_id="fixture")
    runner = Runner(
        agent=Agent(name="fixture", model=model, tools=[publish_report]),
        app_name="fixture",
        session_service=sessions,
    )
    try:
        events = [
            event
            async for event in runner.run_async(
                user_id="fixture",
                session_id=session.id,
                new_message=types.Content(
                    role="user", parts=[types.Part(text="publish once")]
                ),
            )
        ]
        assert report_calls == ["ok"]
        assert len(requests) == 3
        assert requests[0].content == requests[1].content
        assert requests[0].headers["x-fixture"] == "preserved"
        assert "user-agent" in requests[0].headers
        assert all(
            value == MODEL_HTTP_TIMEOUT_MS / 1000
            for value in requests[0].extensions["timeout"].values()
        )
        assert events[-1].content.parts[0].text == "done"
    finally:
        await model.api_client.aio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "denied", "cancelled"])
async def test_retry_limit_and_nonretryable_failures(failure):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if failure == "cancelled":
            raise asyncio.CancelledError
        return httpx.Response(403, json={"error": {"code": 403, "message": "denied"}})

    model = _model(handler)
    expected = {
        "timeout": httpx.ReadTimeout,
        "denied": errors.ClientError,
        "cancelled": asyncio.CancelledError,
    }[failure]
    try:
        with pytest.raises(expected):
            await model.api_client.aio.models.generate_content(
                model=model.model, contents="fixture"
            )
        assert calls == (5 if failure == "timeout" else 1)
    finally:
        await model.api_client.aio.aclose()


@pytest.mark.asyncio
async def test_stream_timeout_after_function_call_is_not_retried():
    calls = 0

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            body = _response({"functionCall": {"name": "publish_report", "args": {}}})
            yield f"data: {json.dumps(body)}\n\n".encode()
            raise httpx.ReadTimeout("synthetic body timeout")

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=InterruptedStream())

    model = _model(handler)
    received = []
    try:
        stream = await model.api_client.aio.models.generate_content_stream(
            model=model.model, contents="fixture"
        )
        with pytest.raises(httpx.ReadTimeout):
            async for response in stream:
                received.append(response)
        assert len(received) == 1
        assert (
            received[0].candidates[0].content.parts[0].function_call.name
            == "publish_report"
        )
        assert calls == 1
    finally:
        await model.api_client.aio.aclose()


def test_clients_and_transports_are_separate_across_event_loops():
    model = OpsGemini(
        model="gemini-fixture",
        client_kwargs={"api_key": "fixture", "vertexai": False, "enterprise": False},
    )
    clients = []
    transports = []

    async def one():
        client = model.api_client
        assert model.api_client is client
        clients.append(client)
        transport = client._api_client._async_httpx_client._transport
        assert isinstance(transport, _EnvironmentTransport)
        assert not client._api_client._use_aiohttp()
        transports.append(transport)
        await client.aio.aclose()
        assert transport.client.is_closed

    asyncio.run(one())
    asyncio.run(one())
    assert clients[0] is not clients[1]
    assert transports[0] is not transports[1]


@pytest.mark.asyncio
async def test_environment_proxy_configuration_is_preserved(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:19999")
    monkeypatch.delenv("NO_PROXY", raising=False)
    transport = _EnvironmentTransport()
    try:
        mounts = transport.client._mounts
        assert any(
            pattern.scheme == "https" and mounted is not None
            for pattern, mounted in mounts.items()
        )
    finally:
        await transport.aclose()
