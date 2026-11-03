"""Exercise traffic guards with local CLI/API fixtures, never live cloud writes."""

from __future__ import annotations

import copy
import io
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.gcp import promote_build as promotion

OLD = "gcp-ops-team-old"
NEW = "gcp-ops-team-new"
OLD_DIGEST = "sha256:" + "1" * 64
NEW_DIGEST = "sha256:" + "2" * 64
BUILD = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SOURCE = "a" * 40
UID = "service-uid"
FILES = {
    "Dockerfile": b"FROM python",
    "uv.lock": b"frozen",
    "scripts/gcp/deploy_cloud_run.sh": b"source at recorded SHA",
}


def zipped(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return output.getvalue()


def service():
    return {
        "metadata": {
            "name": promotion.SERVICE,
            "uid": UID,
            "generation": 7,
            "labels": {"app": "ops"},
            "annotations": {"run.googleapis.com/ingress": "all"},
        },
        "spec": {
            "template": {
                "metadata": {
                    "labels": {"app": "ops"},
                    "annotations": {"autoscaling.knative.dev/maxScale": "2"},
                },
                "spec": {
                    "serviceAccountName": promotion.RUNTIME,
                    "containerConcurrency": 4,
                    "timeoutSeconds": 1800,
                    "containers": [
                        {
                            "image": f"{promotion.IMAGE}@{OLD_DIGEST}",
                            "env": [{"name": "OPS_MODEL", "value": "unchanged"}],
                        }
                    ],
                },
            },
            "traffic": [{"revisionName": OLD, "percent": 100}],
        },
        "status": {
            "latestCreatedRevisionName": OLD,
            "traffic": [{"revisionName": OLD, "percent": 100}],
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def revision(name, digest, spec):
    return {
        "metadata": {
            "name": name,
            "labels": {
                "app": "ops",
                "serving.knative.dev/service": promotion.SERVICE,
                "serving.knative.dev/serviceUid": UID,
            },
            "annotations": {
                "autoscaling.knative.dev/maxScale": "2",
                "run.googleapis.com/operation-id": "observed",
            },
        },
        "spec": copy.deepcopy(spec),
        "status": {
            "imageDigest": f"{promotion.IMAGE}@{digest}",
            "conditions": [{"type": "Ready", "status": "True", "reason": "Retired"}],
        },
    }


class FakeCloud:
    def __init__(self):
        self.before = service()
        self.current = copy.deepcopy(self.before)
        self.current["metadata"]["generation"] = 8
        self.current["metadata"]["annotations"].update(
            {
                "run.googleapis.com/build-id": BUILD,
                "run.googleapis.com/build-service-account": promotion.BUILDER,
            }
        )
        self.current["spec"]["template"]["spec"]["containers"][0]["image"] = (
            f"{promotion.IMAGE}@{NEW_DIGEST}"
        )
        self.current["status"]["latestCreatedRevisionName"] = NEW
        self.policy_value = {
            "bindings": [
                {
                    "role": "roles/run.invoker",
                    "members": ["serviceAccount:scheduler@example.invalid"],
                }
            ]
        }
        self.baseline_policy = copy.deepcopy(self.policy_value)
        self.revisions = {
            OLD: revision(OLD, OLD_DIGEST, self.before["spec"]["template"]["spec"]),
            NEW: revision(NEW, NEW_DIGEST, self.current["spec"]["template"]["spec"]),
        }
        self.build_value = {
            "status": "SUCCESS",
            "serviceAccount": promotion.BUILDER,
            "source": {
                "storageSource": {
                    "bucket": promotion.BUCKET,
                    "object": f"services/{promotion.SERVICE}/source.zip",
                    "generation": "123",
                }
            },
            "results": {"images": [{"name": promotion.IMAGE, "digest": NEW_DIGEST}]},
        }
        self.archive = zipped(FILES)
        self.writes = []
        self.deploys = 0
        self.service_reads = 0
        self.failure = ""
        self.project_role = "roles/logging.logWriter"

    def service(self):
        self.service_reads += 1
        if self.failure == "read":
            raise RuntimeError("CLI get failed")
        if self.failure == "concurrent" and self.service_reads == 2:
            changed = copy.deepcopy(self.current)
            changed["metadata"]["generation"] += 1
            return changed
        return copy.deepcopy(self.current)

    def policy(self):
        return copy.deepcopy(self.policy_value)

    def revision(self, name):
        return copy.deepcopy(self.revisions[name])

    def build(self, identifier):
        assert identifier == BUILD
        return copy.deepcopy(self.build_value)

    def source_archive(self, source):
        assert source is not None
        promotion.require(
            source.get("bucket") == promotion.BUCKET
            and source.get("object", "").startswith(f"services/{promotion.SERVICE}/"),
            "Unexpected source",
        )
        return self.archive

    def source_files(self, source_sha):
        promotion.require(source_sha == SOURCE, "Recorded source SHA differs")
        return FILES

    def cli(self, *arguments):
        member = "serviceAccount:" + promotion.BUILDER.rsplit("/", 1)[-1]
        if arguments[:3] == ("artifacts", "docker", "images"):
            return {"image_summary": {"digest": NEW_DIGEST}}
        if arguments[:3] == ("iam", "service-accounts", "describe"):
            return {"email": promotion.BUILDER.rsplit("/", 1)[-1], "disabled": False}
        if arguments[:2] == ("projects", "get-iam-policy"):
            return {"bindings": [{"role": self.project_role, "members": [member]}]}
        if arguments[:2] == ("artifacts", "repositories"):
            return {
                "bindings": [
                    {"role": "roles/artifactregistry.writer", "members": [member]}
                ]
            }
        if arguments[:2] == ("storage", "buckets"):
            return {
                "bindings": [
                    {"role": "roles/storage.objectViewer", "members": [member]}
                ]
            }
        raise AssertionError(arguments)

    def api(self, method, path, body=None):
        assert path == promotion.RESOURCE
        if method == "GET":
            desired = self.current["spec"]["traffic"][0]
            traffic = {
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
                if desired.get("latestRevision")
                else "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                "percent": 100,
            }
            if not desired.get("latestRevision"):
                traffic["revision"] = desired["revisionName"]
            return {
                "name": promotion.RESOURCE,
                "uid": UID,
                "generation": str(
                    self.current["metadata"]["generation"]
                    + (self.failure == "generation")
                ),
                "etag": "fresh-etag",
                "observedGeneration": str(self.current["metadata"]["generation"]),
                "template": {
                    "containers": [{"image": f"{promotion.IMAGE}@{NEW_DIGEST}"}]
                },
                "traffic": [traffic],
            }
        assert method == "PATCH"
        self.writes.append(copy.deepcopy(body))
        if self.failure == "conflict":
            raise RuntimeError("HTTP 412; write not retried")
        if self.failure == "operation":
            return {"done": True, "error": {"code": 13}}
        self.current["spec"]["traffic"] = [{"revisionName": NEW, "percent": 100}]
        self.current["status"]["traffic"] = [{"revisionName": NEW, "percent": 100}]
        return {"done": True}

    def deploy_source(self, source_sha):
        assert source_sha == SOURCE
        self.deploys += 1
        self.current = copy.deepcopy(self.deployed)


def promote(cloud, *, apply=True, **overrides):
    arguments = {
        "before": cloud.before,
        "before_policy": cloud.baseline_policy,
        "previous": OLD,
        "previous_digest": OLD_DIGEST,
        "candidate": NEW,
        "digest": NEW_DIGEST,
        "build_id": BUILD,
        "source_sha": SOURCE,
        "apply": apply,
        **overrides,
    }
    return promotion.promote(cloud, **arguments)


def test_explicit_pin_promotes_once_without_build_or_latest():
    cloud = FakeCloud()
    assert promote(cloud)["status"] == "promoted"
    assert cloud.deploys == 0
    assert cloud.writes == [
        {
            "name": promotion.RESOURCE,
            "etag": "fresh-etag",
            "traffic": [
                {
                    "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                    "revision": NEW,
                    "percent": 100,
                }
            ],
        }
    ]
    assert promote(cloud)["status"] == "already_promoted"
    assert len(cloud.writes) == 1


def test_plan_reads_without_build_or_patch():
    cloud = FakeCloud()
    result = promote(cloud, apply=False)
    assert result["status"] == "plan_only" and result["image_source_sha"] == SOURCE
    assert cloud.writes == [] and cloud.deploys == 0


def test_revision_materializes_only_the_single_container_default_name():
    cloud = FakeCloud()
    cloud.revisions[NEW]["spec"]["containers"][0]["name"] = promotion.SERVICE + "-1"
    assert promote(cloud, apply=False)["status"] == "plan_only"
    cloud.revisions[NEW]["spec"]["containers"][0]["name"] = "renamed-container"
    with pytest.raises(RuntimeError, match="runtime differs"):
        promote(cloud, apply=False)
    assert cloud.writes == []


@pytest.mark.parametrize("latest", [False, True])
def test_normal_source_deploy_stages_then_promotes_explicit_revision(latest):
    cloud = FakeCloud()
    if latest:
        cloud.before["spec"]["traffic"] = [{"latestRevision": True, "percent": 100}]
    cloud.deployed = copy.deepcopy(cloud.current)
    cloud.current = copy.deepcopy(cloud.before)
    result = promotion.deploy(cloud, SOURCE)
    assert result["status"] == "promoted" and cloud.deploys == 1
    assert len(cloud.writes) == 1


@pytest.mark.parametrize("failure", ["read", "concurrent", "generation"])
def test_read_and_concurrent_changes_fail_before_patch(failure):
    cloud = FakeCloud()
    cloud.failure = failure
    with pytest.raises(RuntimeError):
        promote(cloud)
    assert cloud.writes == [] and cloud.deploys == 0


@pytest.mark.parametrize("failure", ["conflict", "operation"])
def test_failed_conditional_write_is_not_retried(failure):
    cloud = FakeCloud()
    cloud.failure = failure
    with pytest.raises(RuntimeError):
        promote(cloud)
    assert len(cloud.writes) == 1


@pytest.mark.parametrize(
    "case",
    [
        "ready",
        "service",
        "uid",
        "latest",
        "template-digest",
        "revision-digest",
        "runtime",
        "ingress",
        "labels",
        "iam",
        "audit",
        "traffic",
        "tag",
        "external-route",
        "build-sa",
        "build-status",
        "build-image",
        "source-bucket",
        "source-service",
        "source-content",
        "source-extra",
        "project-editor",
    ],
)
def test_invalid_state_never_promotes(case):
    cloud = FakeCloud()
    if case == "ready":
        cloud.revisions[NEW]["status"]["conditions"][0]["status"] = "False"
    elif case == "service":
        cloud.revisions[NEW]["metadata"]["labels"]["serving.knative.dev/service"] = (
            "other"
        )
    elif case == "uid":
        cloud.current["metadata"]["uid"] = "recreated"
    elif case == "latest":
        cloud.current["status"]["latestCreatedRevisionName"] = "gcp-ops-team-concurrent"
    elif case == "template-digest":
        cloud.current["spec"]["template"]["spec"]["containers"][0]["image"] = (
            f"{promotion.IMAGE}@{OLD_DIGEST}"
        )
    elif case == "revision-digest":
        cloud.revisions[NEW]["status"]["imageDigest"] = (
            f"{promotion.IMAGE}@{OLD_DIGEST}"
        )
    elif case == "runtime":
        cloud.revisions[NEW]["spec"]["containers"][0]["env"][0]["value"] = "changed"
    elif case == "ingress":
        cloud.current["metadata"]["annotations"]["run.googleapis.com/ingress"] = (
            "changed"
        )
    elif case == "labels":
        cloud.current["spec"]["template"]["metadata"]["labels"]["app"] = "changed"
    elif case == "iam":
        cloud.policy_value["bindings"].append(
            {"role": "roles/run.invoker", "members": ["allUsers"]}
        )
    elif case == "audit":
        cloud.policy_value["auditConfigs"] = [{"service": "allServices"}]
    elif case == "traffic":
        cloud.current["status"]["traffic"].append({"revisionName": NEW, "percent": 0})
    elif case == "tag":
        cloud.current["spec"]["traffic"][0]["tag"] = "keep-me"
    elif case == "external-route":
        cloud.current["spec"]["traffic"][0]["revisionName"] = cloud.current["status"][
            "traffic"
        ][0]["revisionName"] = "gcp-ops-team-external"
    elif case == "build-sa":
        cloud.build_value["serviceAccount"] = "default-compute"
    elif case == "build-status":
        cloud.build_value["status"] = "FAILURE"
    elif case == "build-image":
        cloud.build_value["results"]["images"][0]["digest"] = OLD_DIGEST
    elif case == "source-bucket":
        cloud.build_value["source"]["storageSource"]["bucket"] = "other"
    elif case == "source-service":
        cloud.build_value["source"]["storageSource"]["object"] = (
            "services/other/source.zip"
        )
    elif case == "source-content":
        cloud.archive = zipped({**FILES, "Dockerfile": b"other source"})
    elif case == "source-extra":
        cloud.archive = zipped({**FILES, "new-helper.py": b"not in original source"})
    elif case == "project-editor":
        cloud.project_role = "roles/editor"
    with pytest.raises(RuntimeError):
        promote(cloud)
    assert cloud.writes == [] and cloud.deploys == 0


def test_new_helper_sha_cannot_replace_recorded_image_source():
    cloud = FakeCloud()
    with pytest.raises(RuntimeError, match="source SHA"):
        promote(cloud, source_sha="b" * 40)
    assert cloud.writes == []


def test_old_serving_runtime_must_match_the_baseline():
    cloud = FakeCloud()
    cloud.revisions[OLD]["spec"]["containers"][0]["env"][0]["value"] = (
        "old-live-setting"
    )
    with pytest.raises(RuntimeError, match="Serving revision runtime differs"):
        promote(cloud)
    assert cloud.writes == []


@pytest.mark.parametrize("current_change", ["deleted-app", "changed-ignore"])
def test_recorded_manifest_is_independent_of_current_checkout(
    tmp_path, monkeypatch, current_change
):
    historical_files = {
        "Dockerfile": b"original docker",
        "uv.lock": b"lock",
        "app/agent.py": b"historical required app",
        ".gcloudignore": b"historical ignore",
        ".gitignore": b"historical git ignore",
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for path, content in historical_files.items():
            entry = tarfile.TarInfo(path)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    current = tmp_path / "current"
    current.mkdir()
    (current / "Dockerfile").write_bytes(historical_files["Dockerfile"])
    (current / "uv.lock").write_bytes(historical_files["uv.lock"])
    if current_change == "changed-ignore":
        (current / ".gcloudignore").write_text("app/**\n")
    calls = []

    def invoke(arguments, *, cwd=None):
        calls.append((arguments, cwd))
        if arguments[:2] == ["git", "archive"]:
            return output.getvalue()
        assert arguments == ["gcloud", "meta", "list-files-for-upload"]
        assert cwd is not None and cwd != current
        assert (cwd / ".gcloudignore").read_bytes() == b"historical ignore"
        assert (cwd / "app/agent.py").read_bytes() == historical_files["app/agent.py"]
        assert cwd.stat().st_mode & 0o777 == 0o700
        return b"Dockerfile\nuv.lock\napp/agent.py\n"

    monkeypatch.setattr(promotion, "command", invoke)
    expected = promotion.Cloud().source_files(SOURCE)
    assert expected["app/agent.py"] == historical_files["app/agent.py"]
    with pytest.raises(RuntimeError, match="file set differs"):
        promotion.verify_archive(
            zipped(
                {
                    "Dockerfile": historical_files["Dockerfile"],
                    "uv.lock": historical_files["uv.lock"],
                }
            ),
            expected,
        )
    assert not calls[1][1].exists()


def test_resume_rejects_current_dirty_tree_before_any_cloud_read(monkeypatch):
    monkeypatch.setenv("OPS_BUILD_PROFILE", "yadora-prod-gcp-ops")
    monkeypatch.setattr(promotion.sys, "argv", ["promote_build.py", "resume"])
    calls = []

    def invoke(arguments, **kwargs):
        calls.append(arguments)
        assert arguments == ["git", "status", "--porcelain"]
        return b" D app/agent.py\n"

    monkeypatch.setattr(promotion, "command", invoke)
    with pytest.raises(RuntimeError, match="worktree must be clean"):
        promotion.main()
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", ["success", "failed", "timeout"])
def test_source_deploy_cli_stages_only_recorded_source_without_runtime_flags(
    monkeypatch, outcome
):
    monkeypatch.setenv("CLOUDSDK_GCLOUDIGNORE_ENABLED", "false")
    calls = []
    directories = []
    base = b".env\nartifacts/\n"
    historical_files = {
        **FILES,
        ".gcloudignore": base,
        promotion.PROFILE_IGNORE: base + b"\n.ruff_cache/\n.gcloudignore.yadora\n",
    }
    archived = io.BytesIO()
    with tarfile.open(fileobj=archived, mode="w") as archive:
        for name, data in historical_files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))

    def historical(arguments, **kwargs):
        assert arguments == ["git", "archive", "--format=tar", SOURCE]
        return archived.getvalue()

    def invoke(arguments, **kwargs):
        calls.append(arguments)
        assert kwargs["cwd"] == promotion.ROOT
        assert kwargs["env"]["CLOUDSDK_GCLOUDIGNORE_ENABLED"] == "true"
        assert os.environ["CLOUDSDK_GCLOUDIGNORE_ENABLED"] == "false"
        directory = Path(arguments[arguments.index("--source") + 1])
        directories.append(directory)
        assert directory != promotion.ROOT
        assert directory.stat().st_mode & 0o777 == 0o700
        assert (directory / ".gcloudignore").read_bytes() == historical_files[
            promotion.PROFILE_IGNORE
        ]
        assert (directory / "uv.lock").read_bytes() == FILES["uv.lock"]
        assert (directory / "uv.lock").stat().st_mode & 0o777 == 0o600
        assert not (directory / ".ruff_cache").exists()
        assert not (directory / "artifacts").exists()
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(arguments, 2700)
        return SimpleNamespace(returncode=9 if outcome == "failed" else 0)

    monkeypatch.setattr(promotion, "command", historical)
    monkeypatch.setattr(promotion.subprocess, "run", invoke)
    if outcome == "success":
        promotion.Cloud().deploy_source(SOURCE)
    else:
        with pytest.raises(RuntimeError, match=r"Source deploy (failed|timed out)"):
            promotion.Cloud().deploy_source(SOURCE)
    assert len(directories) == 1 and not directories[0].exists()
    assert calls == [
        [
            "gcloud",
            "run",
            "deploy",
            promotion.SERVICE,
            "--project",
            promotion.PROJECT,
            "--region",
            promotion.REGION,
            "--source",
            str(directories[0]),
            "--build-service-account",
            promotion.BUILDER,
            "--no-traffic",
            "--quiet",
        ]
    ]


@pytest.mark.parametrize("profile", [None, b".env\n!.env\n"])
def test_source_deploy_rejects_missing_or_invalid_historical_profile(
    monkeypatch, profile
):
    files = {**FILES, ".gcloudignore": b".env\n"}
    if profile is not None:
        files[promotion.PROFILE_IGNORE] = profile
    archived = io.BytesIO()
    with tarfile.open(fileobj=archived, mode="w") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))

    def historical(arguments, **kwargs):
        assert arguments == ["git", "archive", "--format=tar", SOURCE]
        return archived.getvalue()

    def reject(*args, **kwargs):
        raise AssertionError("No source cloud operation may be started")

    monkeypatch.setattr(promotion, "command", historical)
    monkeypatch.setattr(promotion.subprocess, "run", reject)
    with pytest.raises(
        RuntimeError, match=r"(profile ignore missing|ignore must preserve)"
    ):
        promotion.Cloud().deploy_source(SOURCE)


def test_manifest_child_enables_ignores_without_mutating_inherited_setting(monkeypatch):
    monkeypatch.setenv("CLOUDSDK_GCLOUDIGNORE_ENABLED", "false")
    calls = []

    def invoke(arguments, **kwargs):
        calls.append(arguments)
        assert kwargs["env"]["CLOUDSDK_GCLOUDIGNORE_ENABLED"] == "true"
        assert os.environ["CLOUDSDK_GCLOUDIGNORE_ENABLED"] == "false"
        return SimpleNamespace(returncode=0, stdout=b"Dockerfile\n")

    monkeypatch.setattr(promotion.subprocess, "run", invoke)
    assert (
        promotion.command(["gcloud", "meta", "list-files-for-upload"])
        == b"Dockerfile\n"
    )
    assert len(calls) == 1


def test_cli_failure_does_not_print_response_or_token(monkeypatch, capsys):
    monkeypatch.setattr(
        promotion.subprocess,
        "run",
        lambda *args, **kw: SimpleNamespace(
            returncode=9, stdout=b"secret sentinel", stderr=b"private@example.invalid"
        ),
    )
    with pytest.raises(RuntimeError, match="CLI read/operation failed"):
        promotion.command(["gcloud", "run", "services", "describe"])
    assert capsys.readouterr().out == ""


def test_fixed_profile_ignore_preserves_every_ordered_base_rule():
    assert (
        promotion.profile_ignore_contents(promotion.ROOT)
        == (promotion.ROOT / promotion.PROFILE_IGNORE).read_bytes()
    )


@pytest.mark.parametrize(
    "change",
    [
        "missing-cache",
        "missing-private-rule",
        "extra-negation",
        "leading-include-space",
        "leading-pattern-space",
    ],
)
def test_invalid_fixed_profile_ignore_stops_before_source_build(
    tmp_path, monkeypatch, change
):
    base = "#!include:.gitignore\n.env\nartifacts/\n"
    selected = base + "\n.ruff_cache/\n" + promotion.PROFILE_IGNORE + "\n"
    if change == "missing-cache":
        selected = selected.replace(".ruff_cache/\n", "")
    elif change == "missing-private-rule":
        selected = selected.replace("artifacts/\n", "")
    elif change == "extra-negation":
        selected += "!.env\n"
    elif change == "leading-include-space":
        selected = selected.replace("#!include:.gitignore", " #!include:.gitignore")
    else:
        selected = selected.replace(".env\n", " .env\n")
    (tmp_path / ".gcloudignore").write_text(base)
    (tmp_path / promotion.PROFILE_IGNORE).write_text(selected)
    monkeypatch.setattr(promotion, "ROOT", tmp_path)

    def reject(*args, **kwargs):
        raise AssertionError("Source CLI must not be called")

    monkeypatch.setattr(promotion.subprocess, "run", reject)
    with pytest.raises(RuntimeError, match="ignore must preserve"):
        promotion.Cloud().deploy_source(SOURCE)


def test_historical_profile_ignore_drives_manifest_and_cache_is_rejected(monkeypatch):
    files = {
        "Dockerfile": b"docker",
        "uv.lock": b"lock",
        "app/agent.py": b"app",
        ".gitignore": b"",
        ".gcloudignore": b".env\n",
        promotion.PROFILE_IGNORE: b".env\n\n.ruff_cache/\n.gcloudignore.yadora\n",
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for path, content in files.items():
            item = tarfile.TarInfo(path)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))

    def invoke(arguments, *, cwd=None):
        if arguments[:2] == ["git", "archive"]:
            return output.getvalue()
        assert arguments == ["gcloud", "meta", "list-files-for-upload"]
        assert cwd is not None
        assert (cwd / ".gcloudignore").read_bytes() == files[promotion.PROFILE_IGNORE]
        return b"Dockerfile\nuv.lock\napp/agent.py\n"

    monkeypatch.setattr(promotion, "command", invoke)
    expected = promotion.Cloud().source_files(SOURCE)
    promotion.verify_archive(zipped(expected), expected)
    with pytest.raises(RuntimeError, match="file set differs"):
        promotion.verify_archive(
            zipped({**expected, ".ruff_cache/0.17.0/123": b"cache"}), expected
        )
