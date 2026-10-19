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

import contextlib
import os
from collections.abc import AsyncIterator

from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.execution_identity import ExecutionIdentityMiddleware
from app.config import CONFIG

load_dotenv()
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)
otel_to_cloud = True

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Authentication of the scheduler trigger (/apps/app/trigger/pubsub).
# OPS_TRIGGER_AUDIENCE set: ADK verifies the Google-signed OIDC token (audience = the
#   service URL the scheduler targets) and, with OPS_TRIGGER_SERVICE_ACCOUNTS, its caller.
# Not set: authentication is left to the platform — Cloud Run rejects unauthenticated
#   requests (the service is deployed with --no-allow-unauthenticated) — or the request is
#   local development traffic.
TRIGGER_AUDIENCE = os.getenv("OPS_TRIGGER_AUDIENCE", "").strip()
TRIGGER_SERVICE_ACCOUNTS = [
    sa.strip()
    for sa in os.getenv("OPS_TRIGGER_SERVICE_ACCOUNTS", "").split(",")
    if sa.strip()
]


async def _platform_authenticated(_request: object) -> None:
    """Trigger auth delegated to Cloud Run IAM (or local development)."""
    return None


if TRIGGER_AUDIENCE:
    trigger_auth: dict = {
        "trigger_oidc_audience": TRIGGER_AUDIENCE,
        "trigger_oidc_service_accounts": TRIGGER_SERVICE_ACCOUNTS or None,
    }
else:
    trigger_auth = {"trigger_auth_verifier": _platform_authenticated}


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    # Cloud Scheduler (HTTP + OIDC, Pub/Sub-shaped body) -> POST /apps/app/trigger/pubsub
    trigger_sources=["pubsub"],
    **trigger_auth,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=otel_to_cloud,
    lifespan=lifespan,
)
app.title = "cloud-ops-team"
app.description = "API for interacting with the Agent cloud-ops-team"
app.add_middleware(ExecutionIdentityMiddleware, enabled=bool(CONFIG.execution_db_path))


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
