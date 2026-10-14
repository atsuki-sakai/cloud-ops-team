"""Offline regressions for safety flags, secret backups and deployment inputs."""

from __future__ import annotations

import json
import os
import runpy
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import _env_bool

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("YES", True),
        ("on", True),
        ("1", True),
        ("false", False),
        (" No ", False),
        ("off", False),
        ("0", False),
    ],
)
def test_safety_flags_accept_only_explicit_booleans(monkeypatch, raw, expected):
    monkeypatch.setenv("OPS_DRY_RUN", raw)
    assert _env_bool("OPS_DRY_RUN", True) is expected


@pytest.mark.parametrize(
    "raw",
    [
        "tru",  # codespell:ignore
        "falsee",
        "enabled",
        "true # comment",
    ],
)
def test_invalid_safety_flags_stop_configuration(monkeypatch, raw):
    monkeypatch.setenv("OPS_DRY_RUN", raw)
    with pytest.raises(ValueError, match="OPS_DRY_RUN must be"):
        _env_bool("OPS_DRY_RUN", True)


@pytest.mark.parametrize("default", [True, False])
def test_unset_or_empty_safety_flags_keep_defaults(monkeypatch, default):
    monkeypatch.delenv("OPS_DRY_RUN", raising=False)
    assert _env_bool("OPS_DRY_RUN", default) is default
    monkeypatch.setenv("OPS_DRY_RUN", "")
    assert _env_bool("OPS_DRY_RUN", default) is default


def test_setup_backup_preserves_content_but_is_private_and_ignored(
    tmp_path, monkeypatch
):
    setup = runpy.run_path(str(ROOT / "scripts/setup.py"))
    monkeypatch.setitem(
        setup["main"].__globals__,
        "detect",
        lambda: {
            "gcloud_project": "demo",
            "gcloud_account": "",
            "adc": True,
            "gh_user": "",
        },
    )
    env_file = tmp_path / ".env"
    env_file.write_text("GITHUB_TOKEN=synthetic-backup-value\n")
    assert (
        setup["main"](
            [
                "--yes",
                "--force",
                "--target",
                "demo",
                "--repo",
                "demo/app",
                "--env-file",
                str(env_file),
            ]
        )
        == 0
    )
    backup = next(tmp_path.glob(".env.backup-*"))
    assert backup.read_text() == "GITHUB_TOKEN=synthetic-backup-value\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", backup.name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0, ignored.stderr
    assert "#!include:.gitignore" in (ROOT / ".gcloudignore").read_text()


@pytest.mark.parametrize(
    ("first_deploy", "fail_deploy"), [(False, False), (True, False), (False, True)]
)
def test_deploy_preserves_label_values_and_removes_env_file(
    tmp_path, first_deploy, fail_deploy
):
    """Exercise the actual shell script with local CLI stubs only."""
    cli_dir = tmp_path / "bin"
    cli_dir.mkdir()
    capture = tmp_path / "captured.json"
    deployed = tmp_path / "deployed"
    if not first_deploy:
        deployed.touch()
    gcloud = cli_dir / "gcloud"
    gcloud.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "state = pathlib.Path(os.environ['REVIEW_DEPLOYED'])\n"
        "if args[:3] == ['run', 'services', 'describe']:\n"
        "    if not state.exists(): sys.exit(1)\n"
        "    print('https://demo.run.app')\n"
        "elif args[:2] == ['run', 'deploy']:\n"
        "    path = pathlib.Path(args[args.index('--env-vars-file') + 1])\n"
        "    data = {'values': json.loads(path.read_text()), 'path': str(path)}\n"
        "    pathlib.Path(os.environ['REVIEW_CAPTURE']).write_text(json.dumps(data))\n"
        "    if os.environ['REVIEW_FAIL'] == '1': sys.exit(2)\n"
        "    state.touch()\n"
    )
    gcloud.chmod(0o755)
    uv = cli_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert sys.argv[1:4] == ['run', '--quiet', 'python']\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[4:]])\n"
    )
    uv.chmod(0o755)
    env = {
        "PATH": str(cli_dir) + os.pathsep + os.defpath,
        "TMPDIR": str(tmp_path),
        "PROJECT_ID": "demo",
        "GITHUB_REPO": "demo/app",
        "OPS_SEVERITY_LABELS": "critical=priority: critical,high=priority: high|urgent",
        "OPS_STACK_KEYWORDS": "Cloud Run,PostgreSQL\n日本語",
        "OPS_ERROR_LABELS": "",
        "GITHUB_TOKEN": "synthetic-local-token",
        "GOOGLE_APPLICATION_CREDENTIALS": "/synthetic/local/path",
        "REVIEW_CAPTURE": str(capture),
        "REVIEW_DEPLOYED": str(deployed),
        "REVIEW_FAIL": "1" if fail_deploy else "0",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/gcp/deploy_cloud_run.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode != 0) is fail_deploy, result.stderr
    data = json.loads(capture.read_text())
    assert data["values"]["OPS_SEVERITY_LABELS"] == env["OPS_SEVERITY_LABELS"]
    assert data["values"]["OPS_STACK_KEYWORDS"] == env["OPS_STACK_KEYWORDS"]
    assert data["values"]["OPS_ERROR_LABELS"] == ""
    assert data["values"]["OPS_DRY_RUN"] == "true"
    assert "GITHUB_TOKEN" not in data["values"]
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in data["values"]
    assert not Path(data["path"]).exists()
