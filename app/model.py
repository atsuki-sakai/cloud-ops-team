"""Model HTTP clients with bounded idle waits and SDK-owned retries."""

import httpx
from google.adk.models import Gemini
from google.adk.utils._event_loop_cache import PerLoopCachedProperty
from google.genai import Client, types

MODEL_HTTP_TIMEOUT_MS = 60_000


class _EnvironmentTransport(httpx.AsyncBaseTransport):
    """Let HTTPX resolve proxy/CA settings while keeping SDK client ownership."""

    def __init__(self):
        self.client = httpx.AsyncClient()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.client.send(request, stream=True)

    async def aclose(self) -> None:
        await self.client.aclose()


class OpsGemini(Gemini):
    """Keep model retry boundaries inside the SDK's HTTP request handling."""

    @PerLoopCachedProperty
    def api_client(self) -> Client:
        if self.client is not None:
            return self.client
        # A custom HTTPX transport selects the SDK path whose retry predicate
        # includes TimeoutException. Its aiohttp path does not retry timeouts.
        # Allocate it per event loop, just like ADK's default client. Let HTTPX
        # build its transport so environment proxy/CA settings still apply.
        client_kwargs = dict(self.client_kwargs or {})
        options = types.HttpOptions.model_validate(
            client_kwargs.pop("http_options", {})
        )
        async_args = dict(options.async_client_args or {})
        if async_args.get("transport") is None and options.httpx_async_client is None:
            async_args["transport"] = _EnvironmentTransport()
        options = options.model_copy(
            update={
                "timeout": MODEL_HTTP_TIMEOUT_MS,
                "retry_options": self.retry_options,
                "async_client_args": async_args,
            }
        )
        configured = Gemini(
            model=self.model,
            base_url=self.base_url,
            api_version=self.api_version,
            retry_options=self.retry_options,
            client_kwargs={
                **client_kwargs,
                "http_options": options,
            },
        )
        return configured.api_client
