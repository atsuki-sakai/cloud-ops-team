"""Transport identity does not replace authentication or change job payloads."""

import asyncio
import json
import uuid
from dataclasses import replace

import pytest

from app.app_utils.execution_identity import (
    DeliveryIdentity,
    ExecutionIdentityMiddleware,
    bind_delivery_identity,
    current_delivery_identity,
)
from app.config import load_config


async def invoke(
    body=b'{"data":{"job":"error_watch"}}',
    *,
    headers=(),
    enabled=True,
    path="/apps/app/trigger/pubsub",
    downstream_status=200,
    chunks=None,
):
    observed = {}
    responses = []
    incoming = list(chunks or [body])

    async def receive():
        if incoming:
            chunk = incoming.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(incoming)}
        return {"type": "http.disconnect"}

    async def send(message):
        responses.append(message)

    async def app(scope, receive, send):
        observed["identity"] = current_delivery_identity()
        observed["thread_identity"] = await asyncio.to_thread(current_delivery_identity)
        data = bytearray()
        while True:
            message = await receive()
            data.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        observed["body"] = bytes(data)
        await send(
            {"type": "http.response.start", "status": downstream_status, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"unchanged"})

    middleware = ExecutionIdentityMiddleware(app, enabled=enabled)
    await middleware(
        {"type": "http", "method": "POST", "path": path, "headers": list(headers)},
        receive,
        send,
    )
    assert current_delivery_identity() is None
    return observed, responses


SCHEDULE_HEADERS = (
    (b"x-cloudscheduler-jobname", b"projects/example/locations/asia/jobs/error-watch"),
    (b"x-cloudscheduler-scheduletime", b"2026-10-19T00:00:00Z"),
)


@pytest.mark.asyncio
async def test_scheduler_retries_share_opaque_identity_and_preserve_body():
    body = b'{"data":{"job":"error_watch","hours":24}}'
    first, responses = await invoke(
        body, headers=SCHEDULE_HEADERS, chunks=[body[:9], body[9:]]
    )
    again, _ = await invoke(body, headers=SCHEDULE_HEADERS)
    identity = first["identity"]
    assert identity == again["identity"] == first["thread_identity"]
    assert identity.trigger == "scheduler" and identity.scheduled_at == 1792368000
    assert str(uuid.UUID(identity.event_id)) == identity.event_id
    assert "example" not in identity.event_id
    assert identity.correlation_id == identity.event_id
    assert first["body"] == body and responses[0]["status"] == 200


@pytest.mark.asyncio
async def test_scheduler_instant_normalization_and_job_separation():
    first, _ = await invoke(headers=SCHEDULE_HEADERS)
    offset, _ = await invoke(
        headers=[
            SCHEDULE_HEADERS[0],
            (SCHEDULE_HEADERS[1][0], b"2026-10-19T09:00:00+09:00"),
        ]
    )
    next_job, _ = await invoke(
        headers=[
            (SCHEDULE_HEADERS[0][0], b"projects/example/locations/asia/jobs/other"),
            SCHEDULE_HEADERS[1],
        ]
    )
    assert first["identity"] == offset["identity"]
    assert first["identity"].event_id != next_job["identity"].event_id


@pytest.mark.asyncio
async def test_standard_pubsub_message_and_subscription_bind_identity():
    body = json.dumps(
        {
            "message": {"messageId": "1234567890123456", "data": "c2FmZQ=="},
            "subscription": "projects/example/subscriptions/ops",
        }
    ).encode()
    first, _ = await invoke(body)
    again, _ = await invoke(body)
    other = json.loads(body)
    other["subscription"] += "-other"
    different, _ = await invoke(json.dumps(other).encode())
    assert first["identity"] == again["identity"]
    assert first["identity"].trigger == "pubsub"
    assert first["identity"].scheduled_at is None
    assert first["identity"].event_id != different["identity"].event_id
    assert first["body"] == body


@pytest.mark.asyncio
@pytest.mark.parametrize("subscription", [None, "", " "])
async def test_pubsub_without_a_known_subscription_is_invocation_scoped(subscription):
    payload = {"message": {"messageId": "reused-id", "data": "c2FmZQ=="}}
    if subscription is not None:
        payload["subscription"] = subscription
    observed, responses = await invoke(json.dumps(payload).encode())
    assert observed["identity"] is None
    assert responses[0]["status"] == 200


@pytest.mark.asyncio
async def test_identity_scopes_are_task_local():
    result = await asyncio.gather(invoke(headers=SCHEDULE_HEADERS), invoke())
    assert result[0][0]["identity"] is not None
    assert result[1][0]["identity"] is None
    previous = DeliveryIdentity(str(uuid.uuid4()), "manual")
    with bind_delivery_identity(previous):
        with bind_delivery_identity(replace(previous, trigger="scheduler")):
            assert current_delivery_identity().trigger == "scheduler"
        assert current_delivery_identity() == previous
    assert current_delivery_identity() is None


@pytest.mark.asyncio
async def test_delivery_metadata_grants_no_authentication():
    observed, responses = await invoke(headers=SCHEDULE_HEADERS, downstream_status=401)
    assert observed["identity"] is not None
    assert responses[0]["status"] == 401
    assert responses[1]["body"] == b"unchanged"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [SCHEDULE_HEADERS[0]],
        [SCHEDULE_HEADERS[1]],
        [*SCHEDULE_HEADERS, SCHEDULE_HEADERS[0]],
        [SCHEDULE_HEADERS[0], (SCHEDULE_HEADERS[1][0], b"2026-10-19T00:00:00")],
        [SCHEDULE_HEADERS[0], (SCHEDULE_HEADERS[1][0], b"not-a-time")],
        [(SCHEDULE_HEADERS[0][0], b"bad job"), SCHEDULE_HEADERS[1]],
    ],
)
async def test_invalid_scheduler_metadata_fails_before_downstream(headers):
    observed, responses = await invoke(headers=headers)
    assert not observed and responses[0]["status"] == 400
    assert b"bad job" not in responses[1]["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b'{"message":{"messageId":42}}',
        b'{"message":{"messageId":""}}',
        b'{"message":{"messageId":"a","message_id":"a"}}',
        b'{"message":{"messageId":"a"},"subscription":false}',
        b'{"message":{"messageId":"first","messageId":"second"}}',
        b"not-json",
    ],
)
async def test_invalid_pubsub_metadata_is_rejected(body):
    observed, responses = await invoke(body)
    assert not observed and responses[0]["status"] == 400


@pytest.mark.asyncio
async def test_bounded_body_disabled_and_non_trigger_compatibility():
    observed, responses = await invoke(b"a" * 65_537)
    assert not observed and responses[0]["status"] == 413
    for options in ({"enabled": False}, {"path": "/run_sse"}):
        observed, responses = await invoke(b"not-json", **options)
        assert observed["body"] == b"not-json" and observed["identity"] is None
        assert responses[0]["status"] == 200


@pytest.fixture
def clean_history_env(monkeypatch):
    for name in (
        "OPS_EXECUTION_DB_PATH",
        "OPS_EXECUTION_MAX_EVIDENCE_BYTES",
        "OPS_EXECUTION_RETENTION_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_history_config_defaults_and_explicit_settings(monkeypatch, clean_history_env):
    config = load_config()
    assert config.execution_db_path == ""
    assert config.execution_max_evidence_bytes == 8 * 1024 * 1024
    assert config.execution_retention_seconds == 30 * 24 * 3600
    monkeypatch.setenv("OPS_EXECUTION_DB_PATH", " /srv/private/history.sqlite3 ")
    monkeypatch.setenv("OPS_EXECUTION_MAX_EVIDENCE_BYTES", "1024")
    monkeypatch.setenv("OPS_EXECUTION_RETENTION_SECONDS", "3600")
    config = load_config()
    assert config.execution_db_path == "/srv/private/history.sqlite3"
    assert config.execution_max_evidence_bytes == 1024
    assert config.execution_retention_seconds == 3600


@pytest.mark.parametrize(
    "key", ["OPS_EXECUTION_MAX_EVIDENCE_BYTES", "OPS_EXECUTION_RETENTION_SECONDS"]
)
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "false", "invalid"])
def test_invalid_history_limits_fail_closed(monkeypatch, clean_history_env, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=key):
        load_config()
