"""Exercise the real build-profile shell paths using local CLI fixtures only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EMAIL = "gcp-ops-build@yadora-prod.iam.gserviceaccount.com"
RESOURCE = f"projects/yadora-prod/serviceAccounts/{EMAIL}"


@pytest.fixture
def cli(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    calls = tmp_path / "calls.jsonl"
    gcloud = binary / "gcloud"
    gcloud.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "a = sys.argv[1:]\n"
        "with open(os.environ['CAPTURE'], 'a') as f: f.write(json.dumps(a) + '\\n')\n"
        "failure = os.environ.get('FAILURE', '')\n"
        "email = 'gcp-ops-build@yadora-prod.iam.gserviceaccount.com'\n"
        "if a[:3] == ['run', 'services', 'describe']:\n"
        "    if failure == 'missing-service': sys.exit(9)\n"
        "    print('other@demo.iam.gserviceaccount.com' if failure == 'runtime' else 'gcp-ops-team@yadora-prod.iam.gserviceaccount.com')\n"
        "elif a[:3] == ['artifacts', 'repositories', 'describe']:\n"
        "    print('wrong' if failure == 'repository' else 'projects/yadora-prod/locations/asia-northeast1/repositories/cloud-run-source-deploy\\tDOCKER')\n"
        "elif a[:3] == ['storage', 'buckets', 'describe']:\n"
        "    if failure == 'bucket-denied': sys.exit(9)\n"
        "    print('wrong' if failure == 'bucket' else 'run-sources-yadora-prod-asia-northeast1')\n"
        "elif a[:3] == ['iam', 'service-accounts', 'describe']:\n"
        "    if failure == 'identity-denied': sys.exit(9)\n"
        "    if failure == 'identity-json': print('{'); sys.exit(0)\n"
        "    data = {'email': email}\n"
        "    if failure == 'identity-email': data['email'] = 'private-sentinel@example.invalid'\n"
        "    if failure == 'identity-disabled': data['disabled'] = True\n"
        "    if failure == 'identity-null': data['disabled'] = None\n"
        "    if failure == 'identity-string': data['disabled'] = 'false'\n"
        "    if os.environ.get('EXPLICIT_ENABLED'): data['disabled'] = False\n"
        "    print(json.dumps(data))\n"
        "elif a[:2] == ['projects', 'get-iam-policy']:\n"
        "    if failure == 'policy-denied': sys.exit(9)\n"
        "    if failure == 'editor': print('roles/editor')\n"
        "    elif os.environ.get('EXISTING'): print('roles/logging.logWriter')\n"
        "elif a[:3] == ['iam', 'service-accounts', 'list']:\n"
        "    if failure == 'list-denied': sys.exit(9)\n"
        "    if os.environ.get('EXISTING'): print(email)\n"
        "elif a[:3] == ['iam', 'service-accounts', 'create']: pass\n"
        "elif a[:2] == ['projects', 'add-iam-policy-binding']: pass\n"
        "elif a[:3] in [['artifacts', 'repositories', 'add-iam-policy-binding'], ['storage', 'buckets', 'add-iam-policy-binding']]: pass\n"
        "elif a[:2] == ['run', 'deploy']:\n"
        "    if failure == 'deploy': sys.exit(9)\n"
        "else: raise SystemExit('unexpected fixture command')\n"
    )
    gcloud.chmod(0o755)
    uv = binary / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert sys.argv[1:4] == ['run', '--quiet', 'python']\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[4:]])\n"
    )
    uv.chmod(0o755)

    def run(script="deploy_cloud_run.sh", arguments=(), **overrides):
        calls.write_text("")
        env = {
            "PATH": str(binary) + os.pathsep + os.defpath,
            "CAPTURE": str(calls),
            "OPS_BUILD_PROFILE": "yadora-prod-gcp-ops",
            "GITHUB_REPO": "atsuki-sakai/Yadora",
            **overrides,
        }
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/gcp" / script), *arguments],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return result, [json.loads(line) for line in calls.read_text().splitlines()]

    return run


def mutations(calls):
    return [
        call
        for call in calls
        if "create" in call or "add-iam-policy-binding" in call or "deploy" in call
    ]


@pytest.mark.parametrize("explicit_enabled", ["", "1"])
def test_profile_passes_build_identity_and_preserves_live_runtime(
    cli, explicit_enabled
):
    result, calls = cli(
        EXPLICIT_ENABLED=explicit_enabled,
        OPS_DRY_RUN="false",
        MEMORY="9Gi",
        OPS_MODEL="must-not-reconfigure-runtime",
    )
    assert result.returncode == 0, result.stderr
    deploy = mutations(calls)
    assert deploy == [
        [
            "run",
            "deploy",
            "gcp-ops-team",
            "--project",
            "yadora-prod",
            "--region",
            "asia-northeast1",
            "--source",
            ".",
            "--build-service-account",
            RESOURCE,
            "--quiet",
        ]
    ]
    assert not any(
        "secrets" in call or call[:2] == ["services", "enable"] for call in calls
    )
    assert "runtime settings" in result.stdout


@pytest.mark.parametrize(
    "override",
    [
        {"OPS_BUILD_PROFILE": "unknown"},
        {"PROJECT_ID": "other-project"},
        {"GOOGLE_CLOUD_PROJECT": "other-project"},
        {"REGION": "us-central1"},
        {"SERVICE": "other-service"},
        {"RUNTIME_SA": "other@demo.iam.gserviceaccount.com"},
        {"BUILD_SA_EMAIL": "other@demo.iam.gserviceaccount.com"},
        {
            "BUILD_SERVICE_ACCOUNT": "projects/other/serviceAccounts/other@demo.iam.gserviceaccount.com"
        },
        {"BUILD_SOURCE_BUCKET": "other-bucket"},
        {"BUILD_ARTIFACT_REPOSITORY": "other-repository"},
    ],
)
def test_profile_rejects_target_override_before_cloud_access(cli, override):
    result, calls = cli(**override)
    assert result.returncode != 0
    assert calls == []


def test_migrated_service_cannot_fall_back_to_default_builder(cli):
    result, calls = cli(
        OPS_BUILD_PROFILE="", PROJECT_ID="yadora-prod", SERVICE="gcp-ops-team"
    )
    assert result.returncode != 0
    assert calls == []
    assert "requires OPS_BUILD_PROFILE" in result.stderr


@pytest.mark.parametrize(
    "failure",
    [
        "missing-service",
        "runtime",
        "repository",
        "bucket-denied",
        "bucket",
        "identity-denied",
        "identity-json",
        "identity-email",
        "identity-disabled",
        "identity-null",
        "identity-string",
        "policy-denied",
        "editor",
    ],
)
def test_profile_fails_closed_before_deploy(cli, failure):
    result, calls = cli(FAILURE=failure)
    assert result.returncode != 0
    assert mutations(calls) == []
    assert "private-sentinel" not in result.stdout + result.stderr


def test_source_deploy_failure_is_not_retried_or_replaced(cli):
    result, calls = cli(FAILURE="deploy")
    assert result.returncode != 0
    assert len(mutations(calls)) == 1


def test_build_bootstrap_defaults_to_read_only_plan(cli):
    result, calls = cli("bootstrap_build.sh", TARGET_PROJECT="other-monitored-project")
    assert result.returncode == 0, result.stderr
    assert mutations(calls) == []
    assert "Plan only" in result.stdout
    assert not any("other-monitored-project" in call for call in calls)


@pytest.mark.parametrize("existing", ["", "1"])
def test_build_bootstrap_applies_only_scoped_builder_grants(cli, existing):
    result, calls = cli("bootstrap_build.sh", ("--apply",), EXISTING=existing)
    assert result.returncode == 0, result.stderr
    changes = mutations(calls)
    assert len(changes) == (3 if existing else 4)
    roles = {
        argument.removeprefix("--role=")
        for call in changes
        for argument in call
        if argument.startswith("--role=")
    }
    assert roles == {
        "roles/logging.logWriter",
        "roles/artifactregistry.writer",
        "roles/storage.objectViewer",
    }
    project_grants = [
        call for call in changes if call[:2] == ["projects", "add-iam-policy-binding"]
    ]
    assert len(project_grants) == 1
    assert "--role=roles/logging.logWriter" in project_grants[0]
    assert any(
        call[3] == "cloud-run-source-deploy"
        for call in changes
        if call[:2] == ["artifacts", "repositories"]
    )
    assert any(
        call[3] == "gs://run-sources-yadora-prod-asia-northeast1"
        for call in changes
        if call[:2] == ["storage", "buckets"]
    )
    assert all(
        f"--member=serviceAccount:{EMAIL}" in call
        for call in changes
        if "add-iam-policy-binding" in call
    )
    assert not any(
        "enable" in call or "secrets" in call or "scheduler" in call for call in calls
    )


@pytest.mark.parametrize("failure", ["list-denied", "policy-denied", "editor"])
def test_bootstrap_does_not_treat_permission_failure_as_absent_account(cli, failure):
    result, calls = cli("bootstrap_build.sh", ("--apply",), FAILURE=failure)
    assert result.returncode != 0
    assert mutations(calls) == []


def test_build_bootstrap_requires_explicit_profile(cli):
    result, calls = cli("bootstrap_build.sh", ("--apply",), OPS_BUILD_PROFILE="")
    assert result.returncode != 0
    assert calls == []
