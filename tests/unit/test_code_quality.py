"""The code-quality gate catches bad code, passes good code and fails open.

Three layers (docs/code-quality.md):
1. ast-grep rule tests (valid/invalid snippets in lint/ast-grep/rule-tests).
2. The ruff configuration in pyproject.toml: families are on, path ignores work.
3. The Claude Code hook itself (scripts/quality/hook.py), end to end.
Bad samples live in strings here, never as .py files that CI would lint.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BIN = Path(sys.executable).parent
HOOK = ROOT / "scripts" / "quality" / "hook.py"
SKILL = ROOT / ".claude" / "skills" / "python-quality" / "SKILL.md"
RULES = ROOT / "lint" / "ast-grep" / "rules"

pytestmark = pytest.mark.skipif(
    not (BIN / "ruff").exists() or not (BIN / "ast-grep").exists(),
    reason="lint extra not installed (uv sync --extra lint)",
)

BAD_TOOL = """\
import subprocess

import httpx
from google.cloud import logging


def list_keys(accounts, token: str = "hunter2"):
    out = []
    for account in accounts:
        out.append(httpx.get(f"https://example.com/{account}", timeout=5))
    try:
        data = httpx.get("https://example.com", timeout=5)
    except Exception as exc:
        return {"status": "error", "message": f"failed: {exc}"}
    assert data
    subprocess.run("ls " + token, shell=True, check=False)
    return out
"""

GOOD_TOOL = '''\
"""Example boundary written the project way."""

from typing import Any

import httpx

from app.core.errors import tool_error


def fetch_status(url: str) -> dict[str, Any]:
    """Fetch a status page.

    Args:
        url: Page to fetch.
    """
    try:
        resp = httpx.get(url, timeout=5)
    except httpx.HTTPError as exc:
        return tool_error(exc, url=url)
    return {"status": "ok", "code": resp.status_code}
'''


def _load_hook():
    spec = importlib.util.spec_from_file_location("quality_hook", HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway project with the real rule configuration."""
    shutil.copy(ROOT / "pyproject.toml", tmp_path)
    shutil.copy(ROOT / "sgconfig.yml", tmp_path)
    shutil.copytree(ROOT / "lint", tmp_path / "lint")
    for pkg in ("app", "app/tools", "app/core"):
        (tmp_path / pkg).mkdir(exist_ok=True)
        (tmp_path / pkg / "__init__.py").write_text("")
    shutil.copy(ROOT / "app" / "core" / "errors.py", tmp_path / "app" / "core")
    shutil.copy(ROOT / "app" / "core" / "redact.py", tmp_path / "app" / "core")
    return tmp_path


def _run_hook(command: str, payload: dict, root: Path, **env: str):
    return subprocess.run(
        [sys.executable, "-I", str(HOOK), command],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=root,
        env={"CLAUDE_PROJECT_DIR": str(root), "PATH": "/usr/bin:/bin", **env},
        timeout=60,
        check=False,
    )


def _edit(path: Path) -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path)},
        "session_id": "t",
    }


# ------------------------------------------------------------- 1. ast-grep rules


def test_ast_grep_rule_tests_pass():
    proc = subprocess.run(
        [str(BIN / "ast-grep"), "test", "--skip-snapshot-tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_every_rule_has_a_test_with_valid_and_invalid_cases():
    tests = ROOT / "lint" / "ast-grep" / "rule-tests"
    for rule in RULES.glob("*.yml"):
        rule_id = re.search(r"^id: (\S+)", rule.read_text(), re.MULTILINE).group(1)
        case = tests / f"{rule_id}-test.yml"
        assert case.exists(), f"{rule_id} has no rule test"
        text = case.read_text()
        assert "valid:" in text and "invalid:" in text, rule_id


# ------------------------------------------------------------- 2. ruff config


def _ruff_codes(source: str, filename: str) -> set[str]:
    proc = subprocess.run(
        [
            str(BIN / "ruff"),
            "check",
            "--no-fix",
            "--output-format",
            "json",
            "--stdin-filename",
            filename,
            "-",
        ],
        input=source,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return {item["code"] for item in json.loads(proc.stdout or "[]")}


def test_strict_ruff_families_are_enabled_for_app_code():
    codes = _ruff_codes(BAD_TOOL, "app/tools/sample.py")
    for expected in ("ANN001", "ANN201", "S107", "BLE001", "S101", "S602", "PERF401"):
        assert expected in codes, (expected, codes)


def test_tests_may_assert_but_app_code_may_not():
    source = "def test_x() -> None:\n    assert 1 + 1 == 2\n"
    assert "S101" in _ruff_codes(source, "app/core/sample.py")
    assert "S101" not in _ruff_codes(source, "tests/unit/test_sample.py")


def test_frozen_fingerprint_formula_is_exempt_from_s324():
    source = "import hashlib\n\n\ndef f(raw: bytes) -> str:\n    return hashlib.sha1(raw).hexdigest()\n"
    assert "S324" in _ruff_codes(source, "app/core/other.py")
    assert "S324" not in _ruff_codes(source, "app/core/fingerprint.py")


def test_good_boundary_passes_ruff():
    assert _ruff_codes(GOOD_TOOL, "app/tools/sample.py") == set()


# ------------------------------------------------------------- 3. the hook


def test_hook_blocks_bad_code_with_rules_and_fixes(project: Path):
    bad = project / "app" / "tools" / "bad.py"
    bad.write_text(BAD_TOOL)
    proc = _run_hook("post-edit", _edit(bad), project)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    for rule in (
        "tools-cloud-neutral",
        "boundary-error-dict",
        "exception-text-unredacted",
        "api-call-in-loop",
        "BLE001",
        "S101",
        "ANN001",
    ):
        assert rule in proc.stderr, rule
    assert "直し方" in proc.stderr
    assert proc.stderr.startswith("[py-quality]")


def test_hook_passes_good_code_silently(project: Path):
    good = project / "app" / "tools" / "good.py"
    good.write_text(GOOD_TOOL)
    proc = _run_hook("post-edit", _edit(good), project)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == proc.stderr == ""


def test_warnings_inform_without_blocking(project: Path):
    loop = project / "app" / "tools" / "loop.py"
    loop.write_text(
        '"""Loop."""\n\nimport httpx\n\n\n'
        "def fetch(names: list[str]) -> list[int]:\n"
        '    """Fetch."""\n'
        "    return [httpx.get(n, timeout=5).status_code for n in names]\n"
    )
    proc = _run_hook("post-edit", _edit(loop), project)
    assert proc.returncode == 0
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "api-call-in-loop" in context


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_input": {"file_path": "README.md"}},
        {"tool_input": {"file_path": "/etc/passwd.py"}},
        {"tool_input": {"file_path": "app/missing.py"}},
        {"tool_input": {}},
    ],
)
def test_out_of_scope_edits_are_ignored(project: Path, payload: dict):
    proc = _run_hook("post-edit", payload, project)
    assert proc.returncode == 0
    assert proc.stdout == proc.stderr == ""


def test_warn_mode_never_blocks(project: Path):
    bad = project / "app" / "tools" / "bad.py"
    bad.write_text(BAD_TOOL)
    proc = _run_hook("post-edit", _edit(bad), project, CLOUD_OPS_PYCHECK="warn")
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert "boundary-error-dict" in out["hookSpecificOutput"]["additionalContext"]
    assert "warn" in out["systemMessage"]


def test_off_mode_skips_everything(project: Path):
    bad = project / "app" / "tools" / "bad.py"
    bad.write_text(BAD_TOOL)
    proc = _run_hook("post-edit", _edit(bad), project, CLOUD_OPS_PYCHECK="off")
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_malformed_input_fails_open_visibly(project: Path):
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK), "post-edit"],
        input="{not json",
        capture_output=True,
        text=True,
        cwd=project,
        check=False,
    )
    assert proc.returncode == 0
    assert "SETUP" in json.loads(proc.stdout)["systemMessage"]


def test_missing_tool_fails_open_visibly(project: Path, monkeypatch, capsys):
    hook = _load_hook()
    bad = project / "app" / "tools" / "bad.py"
    bad.write_text(BAD_TOOL)
    monkeypatch.setattr(hook.sys, "executable", str(project / "no-venv" / "python"))
    monkeypatch.setattr(hook.sys, "stdin", _Stdin(json.dumps(_edit(bad))))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    assert hook.main(["hook.py", "post-edit"]) == 0
    message = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "SETUP" in message and "uv sync --extra lint" in message


def test_stop_blocks_twice_then_lets_the_session_finish(project: Path):
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "app" / "tools" / "bad.py").write_text(BAD_TOOL)
    payload = {"session_id": f"test-{uuid.uuid4().hex}", "stop_hook_active": False}
    first = _run_hook("stop", payload, project, PATH="/usr/bin:/bin:/opt/homebrew/bin")
    second = _run_hook("stop", payload, project, PATH="/usr/bin:/bin:/opt/homebrew/bin")
    third = _run_hook("stop", payload, project, PATH="/usr/bin:/bin:/opt/homebrew/bin")
    assert (first.returncode, second.returncode, third.returncode) == (2, 2, 0)
    assert "最終チェック" in first.stderr
    assert "未解決" in json.loads(third.stdout)["systemMessage"]


def test_hook_is_fast_enough_for_every_edit(project: Path):
    hook = _load_hook()
    good = project / "app" / "tools" / "good.py"
    good.write_text(GOOD_TOOL)
    _, timings = hook.run_checks(["app/tools/good.py"], project)
    # Checks run in parallel; the budget is per edit (docs/code-quality.md).
    assert max(timings.values()) < 1.0, timings


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


# ------------------------------------------------------------- Skill & ADK sync


def test_skill_lists_every_custom_rule_and_nothing_else():
    skill = SKILL.read_text()
    rule_ids = {
        re.search(r"^id: (\S+)", p.read_text(), re.MULTILINE).group(1)
        for p in RULES.glob("*.yml")
    }
    cited = set(re.findall(r"`ast-grep:([\w-]+)`", skill))
    assert cited == rule_ids, {"missing": rule_ids - cited, "unknown": cited - rule_ids}


def test_skill_cites_only_ruff_families_that_are_enabled():
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["ruff"][
        "lint"
    ]
    enabled = set(config["select"])
    cited = set(re.findall(r"`ruff:([A-Z]+\d*)`", SKILL.read_text()))
    assert cited, "the Skill should name the ruff families it relies on"
    assert {c for c in cited if not any(c.startswith(e) for e in enabled)} == set()


def test_registered_adk_tools_return_dicts():
    from app.specialists import SPECIALISTS

    for fn in {tool for specialist in SPECIALISTS for tool in specialist.tools}:
        annotation = inspect.signature(fn, eval_str=True).return_annotation
        assert annotation is dict or getattr(annotation, "__origin__", None) is dict, (
            f"{fn.__name__} must return a dict (ADK tool contract)"
        )


# ------------------------------------------------------------- the error helper


def test_tool_error_redacts_and_caps_exception_text():
    from app.core.errors import MESSAGE_LIMIT, tool_error

    token = "ghp_" + "a" * 36
    payload = tool_error(
        RuntimeError(f"boom token={token} " + "x" * 1000), provider="gcp"
    )
    assert payload["status"] == "error"
    assert payload["error_type"] == "RuntimeError"
    assert payload["provider"] == "gcp"
    assert token not in payload["message"] and "boom" in payload["message"]
    assert len(payload["message"]) <= len("RuntimeError: ") + MESSAGE_LIMIT


def test_tool_error_message_can_be_replaced():
    from app.core.errors import tool_error

    payload = tool_error(ValueError("secret"), message="Collector failed")
    assert payload["message"] == "Collector failed"
    assert payload["error_type"] == "ValueError"
