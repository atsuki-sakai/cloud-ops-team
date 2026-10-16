#!/usr/bin/env python3
"""Record isolated ADK runs against frozen tools; only model/auth egress is allowed.

Run this script from the candidate Python environment, passing either a candidate
checkout or a read-only git-archive baseline. Tools are replaced before agents
are imported. Each sample gets its own subprocess and trace. See --help.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import functools
import hashlib
import importlib
import inspect
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, get_type_hints
from urllib.parse import urlsplit

DEFAULT_SUITE = Path(__file__).resolve().parents[1] / "tests/eval/routing/cases.json"
DEFAULT_FIXTURE = DEFAULT_SUITE.with_name("frozen-tools-v1.json")
KNOWN_ROUTES = {
    "error_watch",
    "security_audit",
    "pr_review",
    "access_watch",
    "infrastructure_review",
}
ALIASES = {"weekly_pr_review": "pr_review", "monthly_security_audit": "security_audit"}


def verify_recorder_source(expected: str | None) -> None:
    """Reject an edited recorder before tool/model execution, including mid-run."""
    if expected and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != expected:
        raise RuntimeError("Recorder source changed after the batch started")


def _safe_host(host: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:[a-z0-9-]+-)?aiplatform\.googleapis\.com|generativelanguage\.googleapis\.com|oauth2\.googleapis\.com|accounts\.google\.com|iamcredentials\.googleapis\.com",
            host.lower().rstrip("."),
        )
    )


class ModelOnlyEgress:
    """Fail closed before DNS/HTTP for every host except model and ADC refresh."""

    def __init__(self):
        self.allowed_addresses: set[str] = set()
        self.requests: list[dict[str, str]] = []

    def check_url(self, url: Any) -> None:
        parsed = urlsplit(str(url))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not _safe_host(parsed.hostname)
        ):
            raise PermissionError(
                "Non-model network request blocked by routing recorder"
            )
        if parsed.hostname == "iamcredentials.googleapis.com" and not re.fullmatch(
            r"/v1/projects/-/serviceAccounts/[^/]+:generateAccessToken", parsed.path
        ):
            raise PermissionError(
                "Only ADC token issuance is permitted on IAM Credentials"
            )
        self.requests.append({"host": parsed.hostname, "path": parsed.path})

    def install(self) -> None:
        original_lookup = socket.getaddrinfo
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex

        def lookup(host, *args, **kwargs):
            host = host.decode() if isinstance(host, bytes) else host
            if not isinstance(host, str) or not _safe_host(host):
                raise PermissionError(
                    "Non-model DNS lookup blocked by routing recorder"
                )
            result = original_lookup(host, *args, **kwargs)
            self.allowed_addresses.update(item[4][0] for item in result)
            return result

        def connect(sock, address):
            if (
                not isinstance(address, tuple)
                or address[0] not in self.allowed_addresses
            ):
                raise PermissionError("Non-model socket blocked by routing recorder")
            return original_connect(sock, address)

        def connect_ex(sock, address):
            if (
                not isinstance(address, tuple)
                or address[0] not in self.allowed_addresses
            ):
                raise PermissionError("Non-model socket blocked by routing recorder")
            return original_connect_ex(sock, address)

        socket.getaddrinfo = lookup
        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        import httpx
        import requests

        sync_send, async_send = httpx.Client.send, httpx.AsyncClient.send
        request = requests.Session.request

        def send(client, request, *args, **kwargs):
            self.check_url(request.url)
            return sync_send(client, request, *args, **kwargs)

        async def async_send_guard(client, request, *args, **kwargs):
            self.check_url(request.url)
            return await async_send(client, request, *args, **kwargs)

        def rrequest(session, method, url, *args, **kwargs):
            self.check_url(url)
            return request(session, method, url, *args, **kwargs)

        httpx.Client.send = send
        httpx.AsyncClient.send = async_send_guard
        requests.Session.request = rrequest
        import aiohttp

        arequest = aiohttp.ClientSession._request

        async def guarded_aiohttp(session, method, url, *args, **kwargs):
            self.check_url(url)
            return await arequest(session, method, url, *args, **kwargs)

        aiohttp.ClientSession._request = guarded_aiohttp


def completion_assertions(
    case: dict[str, Any],
    calls: list[dict[str, Any]],
    final: str,
    *,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Check observed reads/report artifacts; never infer a real write from prose."""
    successful = [c for c in calls if c["outcome"] == "ok"]
    reads = [
        c
        for c in successful
        if c["phase"] == "analysis"
        and c["tool"]
        not in {
            "publish_report",
            "create_ops_issue",
            "comment_on_issue",
            "add_issue_labels",
            "close_issue",
        }
    ]
    report_bodies = [
        c["arguments"].get("body", "")
        for c in successful
        if c["tool"] == "publish_report"
    ]
    report = "\n".join([final, *report_bodies])
    facts: list[str] = []
    evidence_read = any(c["tool"] == "get_collected_evidence" for c in reads) or any(
        c["tool"] != "get_run_context" for c in reads
    )
    if evidence_read:
        facts.append("evidence_checked")
    # A substantive final report or a captured dry-run report body is required.
    if (
        evidence_read
        and len(report.strip()) >= 80
        and re.search(r"fixture|対象|取得|確認|所見|レポート|報告", report, re.I)
    ):
        facts.append("report_ready")
    rejected = error_code == "invalid_job_request" or (
        case.get("category") == "rejection"
        and not evidence_read
        and bool(
            re.search(
                r"未登録|未対応|無効|不正な(?:引数|型|ジョブ)|拒否|not registered|unsupported|invalid.*(?:job|parameter)",
                final,
                re.I,
            )
        )
    )
    if rejected:
        facts.append("request_rejected")
    boundary = case.get("category") == "future_capability_boundary" and bool(
        re.search(
            r"できません|できない|未対応|未実装|対象外|ありません|提供(?:していません|されて(?:いません|おりません))|cannot|not supported|not available",
            final,
            re.I,
        )
    )
    if boundary and not any(
        c["tool"] in {"backup_create", "restore_database", "create_backup"}
        for c in calls
    ):
        facts.append("capability_boundary_explained")
    partial = case.get("fixture") in {
        "access-partial-v1",
        "access-permission-denied-v1",
    }
    if partial and re.search(
        r"部分|不完全|未取得|取得でき|権限|未確認|partial|incomplete|permission|unavailable",
        report,
        re.I,
    ):
        facts.append("coverage_incomplete_reported")
    if case.get("fixture") == "access-permission-denied-v1" and re.search(
        r"権限|permission|403|アクセス拒否", report, re.I
    ):
        facts.append("permission_gap_reported")
    if re.search(
        r"不正アクセス(?:は|が)存在しないと証明しました|安全性を保証します|no attacks occurred|fully secure",
        report,
        re.I,
    ):
        facts.append("no_attack_proven")
    if re.search(
        r"バックアップを作成しました|backup (?:was )?created successfully", report, re.I
    ):
        facts.append("backup_created")
    status = (
        "unsupported"
        if boundary
        else "refused"
        if rejected
        else "partial"
        if partial
        else "complete"
    )
    return {"status": status, "facts": facts}


def _scripted_job_request(payload: dict[str, Any] | str, requested: str) -> str:
    """Give the offline model the explicit workflow request used by specialists."""
    if isinstance(payload, dict):
        request = dict(payload.get("data", payload))
        request["job"] = requested
    else:
        request = {"job": requested}
        if requested in {"error_watch", "access_watch"}:
            period = re.search(r"(\d+)\s*(?:時間|hours)", payload)
            request["hours"] = int(period.group(1)) if period else 24
        elif requested == "pr_review":
            request["days"] = 7
        elif requested == "security_audit":
            request["kev_days"] = 30
    return json.dumps(request, ensure_ascii=False)


def _fixture_response(
    fixture: dict[str, Any], case: dict[str, Any], name: str, args: dict[str, Any]
) -> dict[str, Any]:
    scenario = fixture.get("scenarios", {}).get(case.get("fixture"), {})
    response = copy.deepcopy(
        scenario.get(
            name,
            fixture["tools"].get(
                name,
                {
                    "status": "not_supported",
                    "message": "No frozen fixture exists for this tool",
                },
            ),
        )
    )
    if name == "get_run_context":
        response["capabilities"] = fixture["capabilities"]
    if name in {
        "summarize_error_groups",
        "get_request_error_rates",
        "investigate_access",
    }:
        response["hours"] = args.get("hours")
    if name in {
        "publish_report",
        "create_ops_issue",
        "comment_on_issue",
        "close_issue",
        "add_issue_labels",
    }:
        response.update(status="dry_run", dry_run=True, preview=copy.deepcopy(args))
    return response


def _patch_tools(
    fixture: dict[str, Any], case: dict[str, Any], recorder: dict[str, Any]
):
    tools = importlib.import_module("app.tools")
    replacements = {}
    for value in list(vars(tools).values()):
        if inspect.isfunction(value) and value.__module__.startswith("app.tools"):
            original = value

            def make_stub(original):
                @functools.wraps(original)
                def stub(*args, **kwargs):
                    bound = inspect.signature(original).bind(*args, **kwargs)
                    bound.apply_defaults()
                    arguments = {
                        k: v for k, v in bound.arguments.items() if k != "tool_context"
                    }
                    result = _fixture_response(
                        fixture, case, original.__name__, arguments
                    )
                    if recorder["collecting"]:
                        recorder["calls"].append(
                            {
                                "phase": "collect",
                                "tool": original.__name__,
                                "arguments": arguments,
                                "outcome": "ok",
                                "facts": [],
                            }
                        )
                    return result

                stub.__signature__ = inspect.signature(original)
                stub.__annotations__ = get_type_hints(original)
                return stub

            replacements[id(original)] = make_stub(original)
    for name, value in list(vars(tools).items()):
        if id(value) in replacements:
            setattr(tools, name, replacements[id(value)])
        elif isinstance(value, list):
            # Specialists retain the original list object created during package
            # import, so update that list in place before rebuilding the agents.
            value[:] = [replacements.get(id(item), item) for item in value]
    # Tool modules retain original aliases; replace those as an additional barrier.
    for name, module in list(sys.modules.items()):
        if name.startswith("app.tools.") and module:
            for attribute, value in list(vars(module).items()):
                if id(value) in replacements:
                    setattr(module, attribute, replacements[id(value)])
    return tools


async def _sample(args) -> dict[str, Any]:
    verify_recorder_source(args.recorder_sha256)
    checkout = args.checkout.resolve()
    os.chdir(checkout)
    sys.path.insert(0, str(checkout))
    fixture = json.loads(args.fixture.read_text())
    suite = json.loads(args.suite.read_text())
    case = next(c for c in suite["cases"] if c["case_id"] == args.child_case)
    os.environ.update(
        {
            "OPS_TARGET": fixture["target"],
            "GCP_TARGET_PROJECT": fixture["target"],
            "OPS_DRY_RUN": "true",
            "OPS_AUTO_CLOSE_ISSUES": "false",
            "GITHUB_REPO": fixture["repository"],
            "OPS_CONTEXT_FILE": "/nonexistent-routing-validation-context",
            # The archive has no .env; keep candidate project-specific stack
            # hints identical to that baseline instead of loading local hints.
            "OPS_STACK_KEYWORDS": "",
            "GOOGLE_GENAI_USE_VERTEXAI": "true",
            "PYTHON_DOTENV_DISABLED": "1",
            "GOOGLE_CLOUD_LOCATION": "global",
        }
    )
    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("GH_TOKEN", None)
    # ADK gives this optional alias precedence over USE_VERTEXAI, so an explicit
    # string 'false' would select the API-key backend. Match the archive's unset
    # alias and prevent the candidate .env from reintroducing it.
    os.environ.pop("GOOGLE_GENAI_USE_ENTERPRISE", None)
    if args.cli_credentials:
        # Read the existing CLI credential into memory only. Neither the user's
        # ADC file nor IAM is modified, and no access token enters the artifacts.
        import google.auth
        from google.oauth2.credentials import Credentials

        token_result = subprocess.run(
            ["gcloud", "auth", "print-access-token", "--quiet"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        credential = Credentials(token=token_result.stdout.strip())
        if not credential.token:
            raise RuntimeError("The existing CLI credential returned no token")
        model_project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not model_project:
            raise RuntimeError("A model billing project must be configured")

        def existing_cli_default(*_args, **_kwargs):
            return credential, model_project

        google.auth.default = existing_cli_default
    egress = ModelOnlyEgress()
    egress.install()
    # Load configuration after the explicit fixture overrides, before app.agent.
    config = importlib.import_module("app.config").CONFIG
    assert (
        config.dry_run
        and config.target == fixture["target"]
        and config.github_repo == fixture["repository"]
    )
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        import google.auth

        _, adc_project = google.auth.default()
        if adc_project:
            os.environ["GOOGLE_CLOUD_PROJECT"] = adc_project
    record: dict[str, Any] = {
        "case_id": case["case_id"],
        "sample": args.child_sample,
        "route": None,
        "trace_complete": False,
        "model_invocations": 0,
        "presentations": [],
        "calls": [],
        "completion": {"status": "partial", "facts": []},
        "evidence": [str(args.child_output)],
    }
    recorder: dict[str, Any] = {
        "collecting": False,
        "calls": record["calls"],
        "schemas": [],
        "events": [],
        "pending": {},
        "final": "",
        "error_code": None,
        "model_responses": 0,
        "collections": [],
    }

    def checkpoint():
        progress = args.child_output.with_suffix(".progress.json")
        progress.write_text(
            json.dumps(
                {
                    "record": record,
                    "model_responses": recorder["model_responses"],
                    "network_hosts": sorted({item["host"] for item in egress.requests}),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            + "\n"
        )
        progress.chmod(0o600)

    _patch_tools(fixture, case, recorder)
    try:
        workflows = importlib.import_module("app.workflows")
    except ModuleNotFoundError:
        workflows = None
    if workflows:
        original_prepare = workflows.prepare_job

        @functools.wraps(original_prepare)
        def prepare(*positional, **kwargs):
            start = len(recorder["calls"])
            recorder["collecting"] = True
            try:
                prepared = original_prepare(*positional, **kwargs)
            finally:
                recorder["collecting"] = False
            actual = recorder["calls"][start:]
            reported = [
                call
                for call in prepared.evidence["collection_calls"]
                if call.get("executed", True)
            ]
            if [(x["tool"], x["arguments"]) for x in actual] != [
                (x["tool"], x["arguments"]) for x in reported
            ]:
                raise AssertionError(
                    "Fixed collection trace differs from collection_calls"
                )
            recorder["collections"].append(prepared.evidence)
            return prepared

        workflows.prepare_job = prepare

    from google.adk.agents.run_config import RunConfig
    from google.adk.models.llm_response import LlmResponse
    from google.adk.plugins.base_plugin import BasePlugin
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    class CapturePlugin(BasePlugin):
        def __init__(self):
            super().__init__(name="routing_recorder")

        async def before_agent_callback(self, *, agent, callback_context):
            if agent.name in KNOWN_ROUTES:
                record["route"] = agent.name

        async def before_model_callback(self, *, callback_context, llm_request):
            verify_recorder_source(args.recorder_sha256)
            agent_name = callback_context.agent_name
            phase = "analysis" if agent_name in KNOWN_ROUTES else "conversation"
            declarations = [
                tool.model_dump(mode="json", exclude_none=True)
                for tool in (llm_request.config.tools or [])
            ]
            names = list(llm_request.tools_dict)
            record["model_invocations"] += 1
            record["presentations"].append({"phase": phase, "tools": names})
            recorder["schemas"].append(
                {"agent": agent_name, "phase": phase, "tools": declarations}
            )
            checkpoint()
            if args.mode == "runtime_stubbed":
                # Scripted model responses exercise the actual runtime only. They
                # are never labelled or interpreted as model selection accuracy.
                seen = {c["tool"] for c in record["calls"] if c["phase"] == phase}
                payload = case["input"]
                requested = (
                    payload.get("data", payload).get("job")
                    if isinstance(payload, dict)
                    else None
                )
                requested = ALIASES.get(requested, requested)
                if isinstance(payload, str):
                    requested = (
                        "error_watch"
                        if "エラー" in payload
                        else "access_watch"
                        if "不正アクセス" in payload
                        else None
                    )
                call_name, call_args = None, {}
                if phase == "conversation":
                    if requested in seen:
                        pass
                    elif requested in KNOWN_ROUTES and requested in names:
                        call_name, call_args = (
                            requested,
                            {"request": _scripted_job_request(payload, requested)},
                        )
                    else:
                        return LlmResponse(
                            content=types.Content(
                                role="model",
                                parts=[
                                    types.Part.from_text(
                                        text="この要求は未対応または無効です。バックアップの作成・復旧は実行できません。対象と必要な機能を確認してください。"
                                    )
                                ],
                            )
                        )
                elif (
                    "get_collected_evidence" in names
                    and "get_collected_evidence" not in seen
                ):
                    call_name = "get_collected_evidence"
                else:
                    plan = fixture["offline_plans"].get(agent_name, [])
                    for step in plan:
                        if step["tool"] in names and step["tool"] not in seen:
                            call_name, call_args = step["tool"], step["arguments"]
                            break
                if call_name:
                    return LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[
                                types.Part.from_function_call(
                                    name=call_name, args=call_args
                                )
                            ],
                        )
                    )
                text = "fixture-project / example/fixture の取得結果を確認しました。固定された観測データに基づく調査結果です。対象のサービスは正常な状態で、設定の変更や実Issueの作成は行っていません。取得範囲と未確認事項を報告します。"
                if case.get("fixture") == "access-partial-v1":
                    text += " ログ取得は部分的で未取得のソースがあります。安全性は断定できません。"
                if case.get("fixture") == "access-permission-denied-v1":
                    text += " 権限不足のためログを取得できず、調査には未確認の範囲があります。"
                return LlmResponse(
                    content=types.Content(
                        role="model", parts=[types.Part.from_text(text=text)]
                    )
                )
            return None

        async def after_model_callback(self, *, callback_context, llm_response):
            recorder["model_responses"] += 1
            checkpoint()

        async def before_tool_callback(self, *, tool, tool_args, tool_context):
            agent_name = tool_context.agent_name
            phase = "analysis" if agent_name in KNOWN_ROUTES else "conversation"
            item = {
                "phase": phase,
                "tool": tool.name,
                "arguments": copy.deepcopy(tool_args),
                "outcome": "error",
                "facts": [],
            }
            record["calls"].append(item)
            recorder["pending"][tool_context.function_call_id] = item
            if tool.name in KNOWN_ROUTES:
                record["route"] = tool.name
            checkpoint()
            return None

        async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
            item = recorder["pending"].pop(tool_context.function_call_id, None)
            if item is None:
                raise AssertionError("Tool result without a recorded attempt")
            item["outcome"] = (
                "blocked"
                if isinstance(result, dict) and result.get("status") == "rejected"
                else "ok"
            )
            if item["outcome"] == "blocked":
                item["rejection"] = {
                    "status": "rejected",
                    "reason": result.get("reason", "runtime_rejected"),
                }
            if tool.name == "get_collected_evidence" or (
                tool.name in fixture["tools"] and tool.name != "get_run_context"
            ):
                item["facts"] = ["evidence_checked"]
            checkpoint()
            return None

    started = time.monotonic()
    try:
        # app.__init__ imports app.agent before configuration/tools can be
        # intercepted. Rebuild it only after all exported callables and lists
        # have been replaced and the collection wrapper has been installed.
        agent = importlib.reload(importlib.import_module("app.agent")).root_agent
        sessions = InMemorySessionService()
        session = await sessions.create_session(
            app_name="routing_validation", user_id="fixture-user"
        )
        runner = Runner(
            agent=agent,
            app_name="routing_validation",
            session_service=sessions,
            plugins=[CapturePlugin()],
        )
        prompt = (
            case["input"]
            if isinstance(case["input"], str)
            else json.dumps(case["input"], ensure_ascii=False)
        )
        async with asyncio.timeout(args.timeout):
            async for event in runner.run_async(
                user_id="fixture-user",
                session_id=session.id,
                new_message=types.Content(
                    role="user", parts=[types.Part.from_text(text=prompt)]
                ),
                run_config=RunConfig(max_llm_calls=12),
            ):
                recorder["events"].append(
                    event.model_dump(mode="json", exclude_none=True)
                )
                if event.error_code:
                    recorder["error_code"] = event.error_code
                if event.is_final_response() and event.content:
                    text = "".join(
                        part.text
                        for part in (event.content.parts or [])
                        if part.text and not part.thought
                    )
                    if text:
                        recorder["final"] = text
        record["trace_complete"] = not recorder["pending"]
    except Exception as exc:
        recorder["error"] = {
            "type": type(exc).__name__,
            "message": str(exc)[:2000],
            "traceback": traceback.format_exc()[-6000:],
        }
    record["completion"] = completion_assertions(
        case, record["calls"], recorder["final"], error_code=recorder["error_code"]
    )
    if recorder.get("error"):
        record["completion"]["status"] = "partial"
    result = {
        "record": record,
        "model": config.model,
        "environment_assertions": {
            "target": config.target,
            "repository": config.github_repo,
            "dry_run": config.dry_run,
            "stack_keywords": list(config.stack_keywords),
            "enterprise_backend": os.environ.get("GOOGLE_GENAI_USE_ENTERPRISE"),
            "vertex_backend": os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"),
            "dotenv_disabled": os.environ.get("PYTHON_DOTENV_DISABLED") == "1",
            "sample_timeout_seconds": args.timeout,
        },
        "details": {
            **recorder,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "network_requests": egress.requests,
        },
    }
    args.child_output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    return result


async def _record(args) -> int:
    from dotenv import dotenv_values

    # Both checkouts inherit the same model/auth project without importing either
    # application's .env secrets. Operations scope is overridden in every child.
    local = dotenv_values(DEFAULT_SUITE.parents[3] / ".env")
    for key in ("OPS_MODEL", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        if not os.environ.get(key) and local.get(key):
            os.environ[key] = str(local[key])
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    recorder_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    suite = json.loads(args.suite.read_text())
    fixture_bytes = args.fixture.read_bytes()
    fixture = json.loads(fixture_bytes)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.parent.chmod(0o700)
    detail_dir = output.parent / (output.stem + "-traces")
    detail_dir.mkdir(exist_ok=True)
    detail_dir.chmod(0o700)
    cases = [
        case for case in suite["cases"] if not args.case or case["case_id"] in args.case
    ]
    if not cases:
        raise ValueError("The selected cases do not exist in the suite")
    semaphore = asyncio.Semaphore(min(args.parallel, 2))
    results = []

    async def one(case, sample):
        async with semaphore:
            detail = detail_dir / f"{case['case_id']}-{sample}.json"
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--checkout",
                str(args.checkout.resolve()),
                "--revision",
                args.revision,
                "--suite",
                str(args.suite.resolve()),
                "--fixture",
                str(args.fixture.resolve()),
                "--mode",
                args.mode,
                "--timeout",
                str(args.timeout),
                "--child-case",
                case["case_id"],
                "--child-sample",
                str(sample),
                "--child-output",
                str(detail),
                "--recorder-sha256",
                recorder_sha256,
            ]
            if args.cli_credentials:
                command.append("--cli-credentials")
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=args.timeout + 45
                )
            except TimeoutError:
                process.kill()
                await process.communicate()
                raise RuntimeError(
                    f"Sample subprocess exceeded its bound: {case['case_id']} {sample}"
                ) from None
            if not detail.exists():
                (detail.with_suffix(".stderr.txt")).write_bytes(stderr[-8000:])
                raise RuntimeError(
                    f"No sample trace: {case['case_id']} {sample}; exit {process.returncode}"
                )
            detail.chmod(0o600)
            result = json.loads(detail.read_text())
            results.append(result)
            status = "recorded" if result["record"]["trace_complete"] else "incomplete"
            print(
                f"{case['case_id']} sample={sample} {status} model_calls={result['record']['model_invocations']} seconds={result['details']['elapsed_seconds']}",
                flush=True,
            )

    errors = await asyncio.gather(
        *(one(case, sample) for case in cases for sample in range(args.samples)),
        return_exceptions=True,
    )
    failures = [str(error) for error in errors if isinstance(error, BaseException)]
    models = {item["model"] for item in results}
    environments = {
        json.dumps(item["environment_assertions"], sort_keys=True) for item in results
    }
    bundle = {
        "schema_version": 1,
        "run_id": output.stem,
        "revision": args.revision,
        "mode": args.mode,
        "suite_sha256": hashlib.sha256(args.suite.read_bytes()).hexdigest(),
        "conditions": {
            "model": next(iter(models)) if len(models) == 1 else "unresolved",
            "model_config": {
                "temperature": 0.2,
                "max_llm_calls": 12,
                "context_cache": False,
                "sample_timeout_seconds": args.timeout,
            },
            "runtime_environment": json.loads(next(iter(environments)))
            if len(environments) == 1
            else "inconsistent",
            "snapshot_id": hashlib.sha256(fixture_bytes).hexdigest(),
            "clock": fixture["clock"],
            "target": fixture["target"],
            "permissions_profile": "fixture-tools-only-model-egress-github-dry-run",
        },
        "records": sorted(
            [item["record"] for item in results],
            key=lambda r: (r["case_id"], r["sample"]),
        ),
        "recorder_errors": failures,
        "instrumentation": {"recorder_sha256": recorder_sha256},
    }
    output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
    output.chmod(0o600)
    return (
        1
        if failures or any(not item["record"]["trace_complete"] for item in results)
        else 0
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--mode", choices=["runtime_stubbed", "live_model"], default="runtime_stubbed"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--parallel", type=int, choices=[1, 2], default=2)
    parser.add_argument("--timeout", type=int, default=150)
    parser.add_argument("--case", action="append")
    parser.add_argument(
        "--cli-credentials",
        action="store_true",
        help="Use the existing active gcloud token in memory; never modify saved ADC/IAM",
    )
    parser.add_argument("--child-case", help=argparse.SUPPRESS)
    parser.add_argument("--child-sample", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--child-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--recorder-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_case:
        asyncio.run(_sample(args))
        return 0
    if not args.output:
        parser.error("--output is required")
    if args.samples < 1 or args.timeout < 1:
        parser.error("samples and timeout must be positive")
    return asyncio.run(_record(args))


if __name__ == "__main__":
    raise SystemExit(main())
