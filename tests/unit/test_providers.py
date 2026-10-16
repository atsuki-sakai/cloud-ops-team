"""Provider contract and architecture tests (no network, no LLM)."""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

import pytest

import app.providers as providers
from app.core.fingerprint import fingerprint, message_template
from app.providers.base import CAPABILITIES, CloudProvider

SKELETONS = ("aws", "azure")


def _dummy_args(fn: Any) -> dict[str, Any]:
    values = {int: 1, str: ""}
    args = {}
    for name, param in inspect.signature(fn).parameters.items():
        annotation = param.annotation
        if isinstance(annotation, str):
            annotation = {"int": int, "str": str}.get(annotation, str)
        args[name] = values.get(annotation, "")
    return args


@pytest.fixture
def use_provider(monkeypatch):
    """Switch the selected provider for the duration of a test."""

    def _use(key: str) -> CloudProvider:
        monkeypatch.setattr(
            providers, "CONFIG", dataclasses.replace(providers.CONFIG, provider=key)
        )
        providers.get_provider.cache_clear()
        return providers.get_provider()

    yield _use
    providers.get_provider.cache_clear()


# --------------------------------------------------------------------------- dedupe


@pytest.mark.parametrize(
    ("resource_type", "service", "message", "expected"),
    [
        (
            "cloud_run_revision",
            "demo-worker",
            "HTTP 500 POST https://demo-worker-x.a.run.app/api/v1/worker/expire-reservations",
            "6e81986cd62c04dc",
        ),
        (
            "cloud_scheduler_job",
            "reconcile-reservations",
            "job=reconcile-reservations status=INTERNAL URL_UNREACHABLE-UNREACHABLE_5xx. Original HTTP response code number = 500",
            "15685eff3c9da72b",
        ),
        (
            "cloudsql_database",
            "proj:db",
            "2026-10-08 11:30:05.682 UTC [3437463]: [2-1] db=app,user=app,host=[local] FATAL:  connection to client lost",
            "a4badb29a284066d",
        ),
    ],
)
def test_fingerprints_are_stable(resource_type, service, message, expected) -> None:
    """Changing the fingerprint formula would re-file every open issue."""
    assert fingerprint(resource_type, service, message_template(message)) == expected


# --------------------------------------------------------------------------- registry


def test_builtin_providers_are_registered() -> None:
    assert {"gcp", "aws", "azure"} <= set(providers.available_providers())


def test_unknown_provider_has_a_clear_error() -> None:
    with pytest.raises(ValueError, match="Unknown cloud provider"):
        providers.load_provider_class("nope")


def test_gcp_implements_every_capability() -> None:
    gcp = providers.load_provider_class("gcp")
    assert gcp.capabilities() == list(CAPABILITIES)


@pytest.mark.parametrize("key", SKELETONS)
def test_skeleton_providers_return_not_supported(key) -> None:
    cls = providers.load_provider_class(key)
    provider = cls(target="t", region="r")
    assert cls.capabilities() == []
    for capability in CAPABILITIES:
        method = getattr(provider, capability)
        out = method(**_dummy_args(method))
        assert out["status"] == "not_supported"
        assert out["capability"] == capability
        assert out["provider"] == key


@pytest.mark.parametrize("key", ("gcp", *SKELETONS))
def test_profiles_cover_every_capability(key) -> None:
    profile = providers.load_provider_class(key).profile
    assert profile.key == key
    assert profile.display_name and profile.cli and profile.target_label_ja
    missing = [
        c
        for c in CAPABILITIES
        if c not in profile.glossary
        and c not in ("list_error_logs", "get_log_context", "count_matching_errors")
    ]
    assert missing == []


# --------------------------------------------------------------------------- tools


def test_every_capability_has_a_neutral_tool() -> None:
    from app.tools import cloud

    for capability in CAPABILITIES:
        tool = getattr(cloud, capability)
        base = getattr(CloudProvider, capability)
        tool_params = list(inspect.signature(tool).parameters)
        base_params = [p for p in inspect.signature(base).parameters if p != "self"]
        assert tool_params == base_params, capability


def test_tool_wrapper_delegates_and_tags_provider(monkeypatch) -> None:
    from app.tools import cloud

    class Fake(CloudProvider):
        profile = providers.load_provider_class("aws").profile

        def summarize_error_groups(self, hours: int) -> dict:
            return {"status": "ok", "hours": hours}

        def list_services(self) -> dict:
            raise RuntimeError("boom")

    monkeypatch.setattr(cloud, "get_provider", lambda: Fake(target="t", region="r"))
    assert cloud.summarize_error_groups(5) == {
        "status": "ok",
        "hours": 5,
        "provider": "aws",
    }
    broken = cloud.list_services()
    assert broken["status"] == "error" and "boom" in broken["message"]
    assert cloud.audit_iam()["status"] == "not_supported"


def test_agent_tools_follow_adk_conventions() -> None:
    from app.specialists import SPECIALISTS

    for fn in {tool for specialist in SPECIALISTS for tool in specialist.tools}:
        assert inspect.getdoc(fn), (
            f"{fn.__name__} needs a docstring (the model reads it)"
        )
        for name, param in inspect.signature(fn).parameters.items():
            if name == "tool_context":
                continue
            assert param.default is inspect.Parameter.empty, (
                f"{fn.__name__}.{name} must not have a default"
            )
            assert param.annotation is not inspect.Parameter.empty, (
                f"{fn.__name__}.{name} needs a type hint"
            )


# --------------------------------------------------------------------------- prompts


@pytest.mark.parametrize("key", ("gcp", *SKELETONS))
def test_prompts_render_for_every_provider(key, use_provider) -> None:
    from app import prompts
    from app.specialists import SPECIALISTS

    provider = use_provider(key)
    renders = [
        prompts.root_instruction,
        *(prompts.instruction_for(s) for s in SPECIALISTS),
    ]
    for render in renders:
        text = render(None)
        assert "__" not in text.replace("__init__", ""), (
            f"unrendered placeholder in {render.__name__}"
        )
        assert provider.profile.display_name in text
    if key in SKELETONS:
        security = next(s for s in SPECIALISTS if s.name == "security_audit")
        assert "未対応" in prompts.instruction_for(security)(None)


def test_run_context_reports_capabilities(use_provider) -> None:
    from app.tools.context import get_run_context

    use_provider("aws")
    ctx = get_run_context()
    assert ctx["provider"] == "aws"
    assert ctx["capabilities"] == []
    assert set(ctx["unsupported_capabilities"]) == set(CAPABILITIES)


# --------------------------------------------------------------------------- issues


def test_dedupe_is_scoped_to_the_target(monkeypatch) -> None:
    from app.core.fingerprint import build_meta_comment
    from app.tools import issues

    def fake_issue(number: int, meta: dict) -> dict:
        return {
            "number": number,
            "title": "t",
            "state": "open",
            "labels": [],
            "body": "x\n" + build_meta_comment(meta),
        }

    open_issues = [
        fake_issue(1, {"fingerprint": "f1", "target": "staging"}),
        fake_issue(2, {"fingerprint": "f1", "project": "prod"}),  # legacy key
        fake_issue(3, {"fingerprint": "f2"}),  # no target recorded
    ]
    monkeypatch.setattr(
        issues,
        "_list_issues",
        lambda state, labels: (True, open_issues if state == "open" else []),
    )
    monkeypatch.setattr(
        issues, "CONFIG", dataclasses.replace(issues.CONFIG, target="prod")
    )
    assert issues.find_existing_ops_issue("f1")["number"] == 2
    assert issues.find_existing_ops_issue("f2")["number"] == 3
    monkeypatch.setattr(
        issues, "CONFIG", dataclasses.replace(issues.CONFIG, target="dev")
    )
    assert issues.find_existing_ops_issue("f1")["status"] == "not_found"


@pytest.mark.parametrize("lang", ("ja", "en"))
def test_issue_messages_exist_for_each_language(lang, monkeypatch) -> None:
    from app.tools import issues

    monkeypatch.setattr(
        issues, "CONFIG", dataclasses.replace(issues.CONFIG, report_language=lang)
    )
    keys = set(issues.MESSAGES["ja"])
    assert set(issues.MESSAGES[lang]) == keys
    assert "#7" in issues._msg("related_closed", number=7, closed="2026-01-01")


def test_config_resolves_provider_specific_env(monkeypatch) -> None:
    from app.config import load_config

    for key in ("OPS_TARGET", "OPS_TARGET_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPS_CLOUD_PROVIDER", "aws")
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    cfg = load_config()
    assert (cfg.provider, cfg.target, cfg.region) == (
        "aws",
        "123456789012",
        "us-west-2",
    )
    monkeypatch.setenv("OPS_TARGET", "override")
    assert load_config().target == "override"
    monkeypatch.setenv("OPS_CLOUD_PROVIDER", "azure")
    monkeypatch.delenv("OPS_TARGET")
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1")
    cfg = load_config()
    assert (cfg.target, cfg.region) == ("sub-1", "japaneast")
