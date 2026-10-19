"""Bind transport delivery identity without trusting it as authentication.

The existing trigger verifier still authenticates the request. Only opaque IDs
and a scheduler timestamp reach the ledger; request bodies and headers do not.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class DeliveryIdentity:
    event_id: str
    trigger: str
    scheduled_at: float | None = None
    correlation_id: str | None = None


_delivery: ContextVar[DeliveryIdentity | None] = ContextVar(
    "ops_delivery_identity", default=None
)


def current_delivery_identity() -> DeliveryIdentity | None:
    return _delivery.get()


@contextmanager
def bind_delivery_identity(identity: DeliveryIdentity | None) -> Iterator[None]:
    token = _delivery.set(identity)
    try:
        yield
    finally:
        _delivery.reset(token)


def _opaque(trigger: str, values: list[str]) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([trigger, *values])))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate delivery fields")
        value[key] = item
    return value


def _scheduler_identity(scope: Scope) -> DeliveryIdentity | None:
    names = (b"x-cloudscheduler-jobname", b"x-cloudscheduler-scheduletime")
    values = [[v for k, v in scope.get("headers", []) if k.lower() == n] for n in names]
    if not any(values):
        return None
    if any(len(group) != 1 for group in values):
        raise ValueError("Invalid scheduler delivery metadata")
    job, schedule = (group[0].decode("ascii") for group in values)
    if not job or len(job) > 1024 or any(ord(char) <= 32 for char in job):
        raise ValueError("Invalid scheduler job")
    moment = dt.datetime.fromisoformat(schedule.replace("Z", "+00:00"))
    if moment.tzinfo is None or moment.timestamp() < 0:
        raise ValueError("Invalid scheduler time")
    normalized = moment.astimezone(dt.UTC).isoformat()
    event_id = _opaque("scheduler", [job, normalized])
    return DeliveryIdentity(event_id, "scheduler", moment.timestamp(), event_id)


def _pubsub_identity(body: bytes) -> DeliveryIdentity | None:
    data = json.loads(body, object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
        return None
    message = data["message"]
    if "messageId" in message and "message_id" in message:
        raise ValueError("Ambiguous Pub/Sub delivery ID")
    message_id = message.get("messageId", message.get("message_id"))
    if message_id is None:
        return None
    subscription = data.get("subscription", "")
    if (
        not isinstance(message_id, str)
        or not 1 <= len(message_id) <= 256
        or any(ord(char) <= 32 for char in message_id)
        or not isinstance(subscription, str)
        or len(subscription) > 2048
    ):
        raise ValueError("Invalid Pub/Sub delivery metadata")
    if not subscription.strip():
        # A message ID alone has no known subscription namespace. Direct HTTP
        # callers may reuse it; those requests must remain invocation-scoped.
        return None
    event_id = _opaque("pubsub", [subscription, message_id])
    return DeliveryIdentity(event_id, "pubsub", correlation_id=event_id)


class ExecutionIdentityMiddleware:
    """Read bounded trigger metadata and preserve the original request body.

    This middleware grants no access. The downstream ADK/platform verifier is
    unchanged and must succeed before a job can enter the execution ledger.
    """

    def __init__(
        self, app: ASGIApp, *, enabled: bool = False, max_body_bytes: int = 65_536
    ) -> None:
        self.app = app
        self.enabled = enabled
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            not self.enabled
            or scope["type"] != "http"
            or scope.get("method") != "POST"
            or not scope.get("path", "").endswith("/trigger/pubsub")
        ):
            await self.app(scope, receive, send)
            return
        try:
            identity = _scheduler_identity(scope)
            # Bound buffering even for scheduler requests. Never retain their
            # body in a ledger, log, exception or delivery identity.
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > self.max_body_bytes:
                    await self._reject(send, 413)
                    return
                if not message.get("more_body", False):
                    break
            payload = bytes(body)
            if identity is None:
                identity = _pubsub_identity(payload)
        except (ValueError, UnicodeError, OverflowError, RecursionError):
            await self._reject(send, 400)
            return

        delivered = False

        async def replay() -> dict:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return await receive()

        with bind_delivery_identity(identity):
            await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send: Send, status: int) -> None:
        body = b'{"status":"rejected","reason":"invalid_delivery_metadata"}'
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
