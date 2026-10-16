"""Stage metadata and execution boundaries are checked without cloud/model calls."""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
import json
from types import SimpleNamespace

import pytest
from google.adk.tools import FunctionTool

from app import prompts, tool_scope
from app.providers import CAPABILITIES
from app.specialists import SPECIALISTS
from app.specialists.base import Specialist
from app.tools import cloud, context


def _specialist(name: str) -> Specialist:
    return next(specialist for specialist in SPECIALISTS if specialist.name == name)


def _prepared(name: str, tools: list) -> Specialist:
    def get_collected_evidence() -> dict:
        """Read the evidence already gathered by this job's fixed workflow."""
        return {"status": "ok", "context": {}, "results": {}}

    return dataclasses.replace(
        _specialist(name), tools=[get_collected_evidence, *tools]
    )


def test_specialist_glossary_excludes_other_specialists_capabilities():
    text = prompts.instruction_for(_specialist("access_watch"))(None)
    assert "- investigate_access:" in text
    assert "- audit_iam:" in text
    assert "list_optimization_recommendations" not in text
    assert "summarize_error_groups" not in text
    assert "scan_deployed_images" not in text
    assert "- list_services:" not in prompts.root_instruction(None)


def test_fixed_collection_is_not_exposed_in_analysis_schema_or_glossary():
    specialist = _prepared("access_watch", [])
    scope = tool_scope.build_tool_scope(specialist)
    declarations = [
        FunctionTool(function)._get_declaration().model_dump(exclude_none=True)
        for function in scope.tools
    ]
    schema = json.dumps(declarations)
    text = prompts.instruction_for(specialist, tools=scope.tools, phase="analysis")(
        None
    )
    assert scope.tool_names == ("get_collected_evidence",)
    assert "get_collected_evidence" in schema and "get_collected_evidence" in text
    for forbidden in ("investigate_access", "list_optimization_recommendations"):
        assert forbidden not in schema
        assert forbidden not in text
    assert "2. investigate_access(hours)" not in text
    assert "http_status_counts" in text
    assert "応答内容と防御の有効性は未確認" in text
    assert "fingerprint=" in text and "coverage" in text


@pytest.mark.parametrize("specialist", SPECIALISTS, ids=lambda item: item.name)
def test_analysis_keeps_reporting_and_domain_requirements(specialist):
    prepared = _prepared(specialist.name, [])
    text = prompts.instruction_for(prepared, phase="analysis")(None)
    assert "固定の収集処理はプログラムが完了済み" in text
    assert "取得範囲や欠落を明記" in text
    assert "__" not in text
    assert "## 手順" not in text
    assert "## 実行手順" not in text
    assert "## 診断チェックリスト" not in text
    if specialist.name == "security_audit":
        assert "engine/engine_version" in text and "最大6件" in text
        assert "critical/high" in text and "月次レポート" in text
    if specialist.name == "infrastructure_review":
        assert "cost_estimate_complete=true" in text
        assert "exclusive_group" in text
        assert "代替権限の検証後" in text
    if specialist.name == "pr_review":
        assert "24時間以上" in text and "14日以上" in text
        assert "ログ未確認" in text


def test_context_wrapper_filters_capabilities_without_mutating_original(monkeypatch):
    original = {
        "target": "configured-target",
        "capabilities": ["investigate_access", "audit_iam", "list_services"],
        "unsupported_capabilities": ["get_log_context", "scan_deployed_images"],
        "dry_run": True,
    }

    def get_run_context() -> dict:
        """Return execution context."""
        return original

    specialist = dataclasses.replace(
        _specialist("access_watch"),
        tools=[get_run_context, cloud.investigate_access],
    )
    scope = tool_scope.build_tool_scope(specialist)
    wrapped = scope.tools[0]
    result = wrapped()
    assert result["capabilities"] == ["investigate_access"]
    assert result["unsupported_capabilities"] == []
    assert result["dry_run"] is True
    assert original["capabilities"] == [
        "investigate_access",
        "audit_iam",
        "list_services",
    ]
    assert inspect.signature(wrapped) == inspect.signature(get_run_context)


def test_scoped_context_uses_real_provider_contract_without_network():
    specialist = dataclasses.replace(
        _specialist("error_watch"),
        tools=[context.get_run_context, cloud.get_log_context],
    )
    wrapped = tool_scope.build_tool_scope(specialist).tools[0]
    result = wrapped()
    assert set(result["capabilities"] + result["unsupported_capabilities"]) == {
        "get_log_context"
    }


def test_direct_exposed_tool_rejects_excessive_range_before_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(cloud, "_call", lambda *a, **kw: calls.append((a, kw)))
    specialist = dataclasses.replace(
        _specialist("access_watch"), tools=[cloud.investigate_access]
    )
    tool = tool_scope.build_tool_scope(specialist).tools[0]
    assert tool(hours=169)["status"] == "rejected"
    assert tool(hours=True)["status"] == "rejected"
    assert tool(hours="24")["status"] == "rejected"
    assert calls == []
    tool(hours=24)
    assert calls == [(("investigate_access",), {"hours": 24})]


def test_callback_rejects_a_tool_absent_from_schema():
    scope = tool_scope.build_tool_scope(_prepared("access_watch", []))
    result = scope.before_tool_callback(SimpleNamespace(name="audit_iam"), {}, None)
    assert result["status"] == "rejected"
    assert result["reason"] == "tool_not_allowed_in_this_stage"


@pytest.mark.parametrize("status", ["ok", "partial", "error"])
@pytest.mark.parametrize("tool_name", ["publish_report", "create_ops_issue"])
def test_analysis_requires_actual_evidence_read_before_reports(status, tool_name):
    calls = []

    def get_collected_evidence() -> dict:
        return {"status": status, "limitations": ["Missing sources remain unverified"]}

    def publish_report(body: str) -> dict:
        calls.append(body)
        return {"status": "dry_run"}

    publish_report.__name__ = tool_name
    specialist = dataclasses.replace(
        _specialist("access_watch"),
        tools=[get_collected_evidence, publish_report],
    )
    scope = tool_scope.build_tool_scope(specialist, phase="analysis")
    reader, reporter = scope.tools
    arguments = {"body": "Coverage is incomplete"}
    callback = scope.before_tool_callback
    assert (
        callback(SimpleNamespace(name=tool_name), arguments, None)["reason"]
        == "collected_evidence_must_be_read_first"
    )
    assert reporter(**arguments)["status"] == "rejected"
    # Merely validating a read request does not prove that evidence was returned.
    assert callback(SimpleNamespace(name="get_collected_evidence"), {}, None) is None
    assert reporter(**arguments)["status"] == "rejected"
    assert calls == []
    assert reader()["status"] == status
    assert callback(SimpleNamespace(name=tool_name), arguments, None) is None
    assert reporter(**arguments) == {"status": "dry_run"}
    assert calls == [arguments["body"]]
    # A new job's scope never inherits another job's completed read.
    other_scope = tool_scope.build_tool_scope(specialist, phase="analysis")
    assert other_scope.tools[1](**arguments)["status"] == "rejected"


def test_failed_or_invalid_evidence_read_keeps_analysis_locked():
    def get_collected_evidence() -> dict:
        raise RuntimeError("Reader failed")

    def publish_report(body: str) -> dict:
        pytest.fail("A report must not be published without evidence")

    specialist = dataclasses.replace(
        _specialist("access_watch"), tools=[get_collected_evidence, publish_report]
    )
    scope = tool_scope.build_tool_scope(specialist)
    reader, reporter = scope.tools
    with pytest.raises(RuntimeError, match="Reader failed"):
        reader()
    assert reporter(body="Unavailable")["status"] == "rejected"
    assert (
        scope.before_tool_callback(
            SimpleNamespace(name="publish_report"), {"body": "Unavailable"}, None
        )["status"]
        == "rejected"
    )

    def invalid_reader() -> dict:
        return None

    invalid_reader.__name__ = "get_collected_evidence"
    invalid = tool_scope.build_tool_scope(
        dataclasses.replace(specialist, tools=[invalid_reader, publish_report])
    )
    invalid.tools[0]()
    assert invalid.tools[1](body="Unavailable")["status"] == "rejected"


@pytest.mark.asyncio
async def test_parallel_calls_cannot_report_while_evidence_read_is_pending():
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()
    reports = []

    async def get_collected_evidence() -> dict:
        started.set()
        await release.wait()
        return {"status": "partial"}

    async def publish_report(body: str) -> dict:
        reports.append(body)
        return {"status": "dry_run"}

    scope = tool_scope.build_tool_scope(
        dataclasses.replace(
            _specialist("access_watch"), tools=[get_collected_evidence, publish_report]
        )
    )
    reader, reporter = scope.tools
    reading = asyncio.create_task(reader())
    await started.wait()
    try:
        assert (
            scope.before_tool_callback(
                SimpleNamespace(name="publish_report"), {"body": "Too early"}, None
            )["status"]
            == "rejected"
        )
        assert (await reporter(body="Too early"))["status"] == "rejected"
        assert reports == []
    finally:
        release.set()
        await reading
    assert (
        scope.before_tool_callback(
            SimpleNamespace(name="publish_report"), {"body": "Missing sources"}, None
        )
        is None
    )
    assert await reporter(body="Missing sources") == {"status": "dry_run"}
    assert reports == ["Missing sources"]


def test_legacy_scope_does_not_require_the_analysis_evidence_reader():
    def publish_report(body: str) -> dict:
        return {"status": "dry_run"}

    specialist = _prepared("access_watch", [publish_report])
    scope = tool_scope.build_tool_scope(specialist, phase="legacy")
    assert scope.tools[1](body="Existing path") == {"status": "dry_run"}


def test_model_presentation_log_uses_actual_request_schema_without_content(capsys):
    def read_evidence() -> dict:
        """Sensitive evidence description: SECRET_DESCRIPTION."""
        return {"secret": "SECRET_RESULT"}

    specialist = dataclasses.replace(
        _specialist("access_watch"), tools=[read_evidence, cloud.investigate_access]
    )
    scope = tool_scope.build_tool_scope(specialist)
    # The current model call may expose less than the stage's full superset.
    request = SimpleNamespace(
        tools_dict={"read_evidence": FunctionTool(scope.tools[0])},
        contents=["SECRET_PROMPT"],
    )
    scope.before_model_callback(SimpleNamespace(invocation_id="run-123"), request)
    output = capsys.readouterr().out
    record = json.loads(output)
    assert record["event"] == "model_tool_presentation"
    assert record["invocation_id"] == "run-123"
    assert record["phase"] == "analysis"
    assert record["specialist"] == "access_watch"
    assert record["tool_names"] == ["read_evidence"]
    assert record["tool_count"] == 1
    assert record["schema_characters"] > record["description_characters"] > 0
    assert "investigate_access" not in output
    for secret in ("SECRET_DESCRIPTION", "SECRET_PROMPT", "SECRET_RESULT"):
        assert secret not in output


def test_model_presentation_log_records_empty_exposure(capsys):
    scope = tool_scope.build_tool_scope(_prepared("access_watch", []))
    scope.before_model_callback(SimpleNamespace(), SimpleNamespace(tools_dict={}))
    record = json.loads(capsys.readouterr().out)
    assert record["tool_names"] == [] and record["tool_count"] == 0
    assert record["schema_characters"] == record["description_characters"] == 0


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("list_error_logs", {"hours": 24, "service_name": "api", "limit": 201}),
        ("list_deployments", {"service_name": "api", "limit": 21}),
        (
            "get_log_context",
            {
                "service_name": "api",
                "around_time": "",
                "window_minutes": 121,
                "trace": "t",
            },
        ),
        (
            "get_log_context",
            {
                "service_name": "",
                "around_time": "",
                "window_minutes": 10,
                "trace": "projects/other/traces/t",
            },
        ),
        (
            "list_deployments",
            {"service_name": "projects/other/locations/r/services/api", "limit": 10},
        ),
        ("summarize_error_groups", {"hours": 24, "target": "other"}),
        ("summarize_error_groups", {"hours": 24, "repository": "other/repo"}),
        (
            "count_matching_errors",
            {
                "service_name": "api",
                "search_text": "error",
                "since": "1900-01-01T00:00:00Z",
            },
        ),
        (
            "count_matching_errors",
            {"service_name": "api", "search_text": "error", "since": "2026-10-01"},
        ),
        ("search_issues", {"query": "repo:other/project error"}),
        ("search_issues", {"query": "org:other error"}),
        ("search_issues", {"query": "error", "repo": "other/project"}),
    ],
)
def test_execution_boundary_rejects_scope_and_query_budget_injection(name, args):
    scope = tool_scope.build_tool_scope(_specialist("error_watch"))
    result = scope.before_tool_callback(SimpleNamespace(name=name), args, None)
    assert result is not None and result["status"] == "rejected"


def test_valid_existing_arguments_preserve_schema_and_invocation(monkeypatch):
    calls = []
    monkeypatch.setattr(cloud, "_call", lambda *a, **kw: calls.append((a, kw)))
    scope = tool_scope.build_tool_scope(_specialist("error_watch"))
    args = {
        "service_name": "api",
        "around_time": dt.datetime.now(dt.UTC).isoformat(),
        "window_minutes": 10,
        "trace": "",
    }
    assert (
        scope.before_tool_callback(SimpleNamespace(name="get_log_context"), args, None)
        is None
    )
    function = next(fn for fn in scope.tools if fn.__name__ == "get_log_context")
    assert inspect.signature(function) == inspect.signature(cloud.get_log_context)
    function(**args)
    assert calls == [(("get_log_context",), args)]
    # Literal quoted log text that resembles a qualifier is not a scope change.
    assert (
        scope.validate("search_issues", {"query": '"repo:other/project" in:body'})
        is None
    )


def test_runtime_context_cannot_be_supplied_by_the_model():
    scope = tool_scope.build_tool_scope(_specialist("access_watch"))
    arguments = {
        "kind": "security",
        "title": "Finding",
        "body": "Evidence",
        "severity": "high",
        "fingerprint": "audit:test",
        "service": "resource",
        "search_text": "",
    }
    assert scope.validate("create_ops_issue", arguments) is None
    assert (
        scope.validate(
            "create_ops_issue", {**arguments, "tool_context": {"dry_run": False}}
        )["status"]
        == "rejected"
    )
    declaration = FunctionTool(
        next(fn for fn in scope.tools if fn.__name__ == "create_ops_issue")
    )._get_declaration()
    assert "tool_context" not in declaration.parameters_json_schema["properties"]


def test_stage_selection_uses_callable_identity_not_just_an_allowed_name():
    specialist = _specialist("access_watch")

    def investigate_access(hours: int) -> dict:
        return {"unexpected": True}

    with pytest.raises(ValueError, match="outside"):
        tool_scope.build_tool_scope(specialist, allowed_tools=[investigate_access])
    scope = tool_scope.build_tool_scope(
        specialist,
        allowed_tools=[cloud.investigate_access],
        excluded_tools=[cloud.investigate_access],
    )
    assert scope.tools == () and scope.capabilities == ()
    assert scope.validate("investigate_access", {"hours": 24})["status"] == "rejected"


def test_capability_names_are_derived_from_the_actual_callable_subset():
    selected = [cloud.get_log_context, cloud.list_deployments]
    assert set(tool_scope.capabilities_for(selected)) == {
        "get_log_context",
        "list_deployments",
    }
    assert len(tool_scope.capabilities_for(selected)) < len(CAPABILITIES)


def test_async_exposed_tool_is_checked_before_execution():
    calls = []

    async def investigate_access(hours: int) -> dict:
        calls.append(hours)
        return {"status": "ok"}

    specialist = dataclasses.replace(
        _specialist("access_watch"), tools=[investigate_access]
    )
    function = tool_scope.build_tool_scope(specialist).tools[0]

    async def run():
        assert (await function(hours=1000))["status"] == "rejected"
        assert await function(hours=24) == {"status": "ok"}

    import asyncio

    asyncio.run(run())
    assert calls == [24]


def test_aws_resource_identifier_is_bounded_to_the_configured_account(monkeypatch):
    monkeypatch.setattr(
        tool_scope,
        "CONFIG",
        dataclasses.replace(tool_scope.CONFIG, provider="aws", target="123456789012"),
    )
    scope = tool_scope.build_tool_scope(_specialist("error_watch"))
    for resource in ("arn:bad", "arn:aws:lambda:r:999999999999:function:f"):
        assert (
            scope.validate("list_deployments", {"service_name": resource, "limit": 1})[
                "status"
            ]
            == "rejected"
        )
    assert (
        scope.validate(
            "list_deployments",
            {"service_name": "arn:aws:lambda:r:123456789012:function:f", "limit": 1},
        )
        is None
    )
