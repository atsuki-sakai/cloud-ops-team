"""Fixed-profile source deployment and guarded, explicit-revision promotion.

The only traffic write is a v2 traffic-only PATCH with the freshly read etag.
Errors, conflicts and unknown outcomes are never retried as writes.
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path

PROJECT = "yadora-prod"
REGION = "asia-northeast1"
SERVICE = "gcp-ops-team"
RESOURCE = f"projects/{PROJECT}/locations/{REGION}/services/{SERVICE}"
BUILDER = f"projects/{PROJECT}/serviceAccounts/gcp-ops-build@{PROJECT}.iam.gserviceaccount.com"
RUNTIME = f"gcp-ops-team@{PROJECT}.iam.gserviceaccount.com"
BUCKET = "run-sources-yadora-prod-asia-northeast1"
IMAGE = f"{REGION}-docker.pkg.dev/{PROJECT}/cloud-run-source-deploy/{SERVICE}"
ROOT = Path(__file__).resolve().parents[2]
PROFILE_IGNORE = ".gcloudignore.yadora"
PROVENANCE = {
    "run.googleapis.com/client-name",
    "run.googleapis.com/client-version",
    "run.googleapis.com/operation-id",
    "serving.knative.dev/creator",
    "serving.knative.dev/lastModifier",
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def command(arguments, *, cwd=ROOT):
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            capture_output=True,
            check=False,
            timeout=120,
            env=source_environment()
            if arguments[:3] == ["gcloud", "meta", "list-files-for-upload"]
            else None,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("CLI read timed out; no automatic retry") from None
    require(
        result.returncode == 0, f"CLI read/operation failed: {' '.join(arguments[:3])}"
    )
    return result.stdout


def decode(raw):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise RuntimeError("Invalid JSON response") from None


def profile_ignore_contents(directory):
    base = (directory / ".gcloudignore").read_bytes()
    selected = (directory / PROFILE_IGNORE).read_bytes()
    require(
        selected == base + b"\n.ruff_cache/\n" + PROFILE_IGNORE.encode() + b"\n",
        "Fixed-profile ignore must preserve the exact base bytes and only append its two exclusions",
    )
    return selected


def source_environment():
    # Ignore exclusions cannot be disabled by caller config or inherited env.
    return {**os.environ, "CLOUDSDK_GCLOUDIGNORE_ENABLED": "true"}


@contextmanager
def recorded_source(source_sha, *, require_profile=False):
    require(
        re.fullmatch(r"[0-9a-f]{40}", source_sha),
        "Expected a complete image source SHA",
    )
    archived = command(["git", "archive", "--format=tar", source_sha])
    with tempfile.TemporaryDirectory(prefix="cloudops-recorded-source-") as temporary:
        directory = Path(temporary).resolve()
        with tarfile.open(fileobj=io.BytesIO(archived), mode="r:") as historical:
            historical.extractall(directory, filter="data")
        for selected in directory.rglob("*"):
            require(not selected.is_symlink(), "Recorded source symlink is unsupported")
            selected.chmod(
                0o700 if selected.is_dir() or selected.stat().st_mode & 0o111 else 0o600
            )
        has_profile = (directory / PROFILE_IGNORE).exists()
        require(
            not require_profile or has_profile, "Recorded source profile ignore missing"
        )
        if has_profile:
            # Run deploy has no --ignore-file flag in supported installed SDKs.
            # Only this private archive receives the exact checked profile bytes.
            selected_ignore = profile_ignore_contents(directory)
            (directory / ".gcloudignore").write_bytes(selected_ignore)
        yield directory


def policy_members(policy):
    require(
        isinstance(policy, dict) and isinstance(policy.get("bindings"), list),
        "IAM response has no bindings",
    )
    return sorted(
        (binding["role"], member, json.dumps(binding.get("condition"), sort_keys=True))
        for binding in policy["bindings"]
        for member in binding.get("members", [])
    )


def policy_config(policy):
    return policy_members(policy), json.dumps(
        policy.get("auditConfigs", []), sort_keys=True
    )


def builder_guard(cloud):
    email = BUILDER.rsplit("/", 1)[-1]
    identity = cloud.cli("iam", "service-accounts", "describe", email)
    require(
        identity.get("email") == email and identity.get("disabled", False) is False,
        "Builder is disabled or mismatched",
    )
    project_policy = cloud.cli("projects", "get-iam-policy", PROJECT)
    roles = {
        role
        for role, member, _ in policy_members(project_policy)
        if member == "serviceAccount:" + email
    }
    require(roles == {"roles/logging.logWriter"}, "Builder project roles differ")
    for arguments, expected in (
        (
            (
                "artifacts",
                "repositories",
                "get-iam-policy",
                "cloud-run-source-deploy",
                "--location",
                REGION,
            ),
            "roles/artifactregistry.writer",
        ),
        (
            ("storage", "buckets", "get-iam-policy", f"gs://{BUCKET}"),
            "roles/storage.objectViewer",
        ),
    ):
        bindings = {
            (role, condition)
            for role, member, condition in policy_members(cloud.cli(*arguments))
            if member == "serviceAccount:" + email
        }
        require(bindings == {(expected, "null")}, "Builder resource roles differ")


def metadata_config(metadata):
    return {
        "labels": {
            key: value
            for key, value in metadata.get("labels", {}).items()
            if not key.startswith("serving.knative.dev/")
            and key not in ("cloud.googleapis.com/location", "client.knative.dev/nonce")
        },
        "annotations": {
            key: value
            for key, value in metadata.get("annotations", {}).items()
            if key not in PROVENANCE and not key.startswith("run.googleapis.com/build-")
        },
    }


def template_config(template):
    spec = copy.deepcopy(template["spec"])
    require(
        spec.get("serviceAccountName") == RUNTIME, "Runtime service account differs"
    )
    require(len(spec.get("containers", [])) == 1, "Expected one existing container")
    # Cloud Run omits the default name in Service templates and materializes it
    # in Revisions. Preserve explicit names and reject an actual rename.
    spec["containers"][0].setdefault("name", f"{SERVICE}-1")
    spec["containers"][0].pop("image", None)
    return {"metadata": metadata_config(template.get("metadata", {})), "spec": spec}


def service_config(service):
    require(service.get("metadata", {}).get("name") == SERVICE, "Wrong service")
    return {
        "metadata": metadata_config(service["metadata"]),
        "template": template_config(service["spec"]["template"]),
    }


def route(service):
    desired = service.get("spec", {}).get("traffic", [])
    observed = service.get("status", {}).get("traffic", [])
    require(len(desired) == len(observed) == 1, "Expected exactly one traffic target")
    require(
        all(
            item.get("percent") == 100 and not item.get("tag")
            for item in (*desired, *observed)
        ),
        "Expected untagged 100% traffic",
    )
    revision = observed[0].get("revisionName")
    require(
        isinstance(revision, str) and revision.startswith(SERVICE + "-"),
        "Wrong serving revision",
    )
    require(
        desired[0].get("revisionName") == revision
        or desired[0].get("latestRevision") is True,
        "Desired/observed traffic differs",
    )
    return revision


def ready(revision, name, uid, digest):
    metadata = revision.get("metadata", {})
    require(
        metadata.get("name") == name
        and metadata.get("labels", {}).get("serving.knative.dev/service") == SERVICE,
        "Revision belongs to another service",
    )
    require(
        metadata.get("labels", {}).get("serving.knative.dev/serviceUid") == uid,
        "Revision service UID differs",
    )
    require(
        any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in revision.get("status", {}).get("conditions", [])
        ),
        "Revision is not Ready",
    )
    require(
        revision.get("status", {}).get("imageDigest") == f"{IMAGE}@{digest}",
        "Revision digest differs",
    )
    require(
        revision.get("spec", {}).get("containers", [{}])[0].get("image")
        == f"{IMAGE}@{digest}",
        "Revision image is not the expected immutable image",
    )


class Cloud:
    def cli(self, *arguments):
        return decode(
            command(["gcloud", *arguments, "--project", PROJECT, "--format=json"])
        )

    def service(self):
        return self.cli("run", "services", "describe", SERVICE, "--region", REGION)

    def policy(self):
        return self.cli(
            "run", "services", "get-iam-policy", SERVICE, "--region", REGION
        )

    def revision(self, name):
        return self.cli("run", "revisions", "describe", name, "--region", REGION)

    def build(self, identifier):
        return self.cli("builds", "describe", identifier, "--region", REGION)

    def deploy_source(self, source_sha):
        profile_ignore_contents(ROOT)
        with recorded_source(source_sha, require_profile=True) as directory:
            arguments = [
                "gcloud",
                "run",
                "deploy",
                SERVICE,
                "--project",
                PROJECT,
                "--region",
                REGION,
                "--source",
                str(directory),
                "--build-service-account",
                BUILDER,
                "--no-traffic",
                "--quiet",
            ]
            try:
                result = subprocess.run(
                    arguments,
                    cwd=ROOT,
                    check=False,
                    timeout=2700,
                    env=source_environment(),
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    "Source deploy timed out; inspect the existing operation, do not resubmit"
                ) from None
            require(
                result.returncode == 0,
                "Source deploy failed; no automatic retry or promotion",
            )

    def source_files(self, source_sha):
        # Compare the complete uploaded tracked tree, including deployment scripts,
        # so the newer helper commit cannot be called the old image's source SHA.
        with recorded_source(source_sha) as directory:
            paths = (
                command(["gcloud", "meta", "list-files-for-upload"], cwd=directory)
                .decode()
                .splitlines()
            )
            require(
                paths and "Dockerfile" in paths and "uv.lock" in paths,
                "Source manifest is incomplete",
            )
            expected = {}
            for path in paths:
                selected = directory / path
                require(
                    selected.resolve().is_relative_to(directory) and selected.is_file(),
                    "Historical upload path is invalid",
                )
                expected[path] = selected.read_bytes()
            return expected

    def source_archive(self, source):
        require(
            source.get("bucket") == BUCKET
            and str(source.get("object", "")).startswith(f"services/{SERVICE}/"),
            "Unexpected source bucket/object",
        )
        require(
            re.fullmatch(r"[0-9]+", str(source.get("generation", ""))),
            "Source object generation missing",
        )
        return command(
            [
                "gcloud",
                "storage",
                "cat",
                f"gs://{BUCKET}/{source['object']}#{source['generation']}",
            ]
        )

    def api(self, method, path, body=None):
        require(
            path == RESOURCE
            or path.startswith(f"projects/{PROJECT}/locations/{REGION}/operations/"),
            "Unexpected API target",
        )
        token = command(["gcloud", "auth", "print-access-token"]).decode().strip()
        require(token, "Short access token missing")
        url = f"https://run.googleapis.com/v2/{path}" + (
            "?updateMask=traffic" if method == "PATCH" else ""
        )
        request = urllib.request.Request(
            url,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return decode(response.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Cloud Run {method} failed (HTTP {exc.code}); write not retried"
            ) from None
        except OSError:
            raise RuntimeError(
                f"Cloud Run {method} failed; outcome unknown, write not retried"
            ) from None


def verify_archive(raw, expected):
    require(len(raw) <= 32 * 1024 * 1024, "Source archive exceeds bound")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            files = [entry for entry in archive.infolist() if not entry.is_dir()]
            require(
                sum(entry.file_size for entry in files) <= 128 * 1024 * 1024,
                "Expanded source exceeds bound",
            )
            names = [entry.filename.removeprefix("./") for entry in files]
            require(
                len(names) == len(set(names)) and set(names) == set(expected),
                "Source archive file set differs from recorded SHA",
            )
            for entry, name in zip(files, names, strict=True):
                require(
                    archive.read(entry) == expected[name],
                    "Source archive content differs from recorded SHA",
                )
    except zipfile.BadZipFile:
        raise RuntimeError("Invalid source archive") from None


def validate(
    cloud,
    before,
    before_policy,
    previous,
    previous_digest,
    candidate,
    digest,
    build_id,
    source_sha,
):
    require(
        re.fullmatch(r"gcp-ops-team-[a-z0-9-]+", candidate),
        "Invalid candidate revision",
    )
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "Invalid candidate digest")
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", previous_digest), "Invalid previous digest"
    )
    require(re.fullmatch(r"[0-9a-f-]{36}", build_id), "Invalid build ID")
    require(route(before) == previous, "Baseline previous revision differs")
    current = cloud.service()
    require(
        current["metadata"].get("uid") == before["metadata"].get("uid")
        and bool(current["metadata"].get("uid")),
        "Service UID changed",
    )
    require(
        service_config(current) == service_config(before),
        "Runtime/service settings changed",
    )
    require(
        policy_config(cloud.policy()) == policy_config(before_policy),
        "Service IAM changed",
    )
    require(route(current) in (previous, candidate), "Traffic changed externally")
    require(
        current.get("status", {}).get("latestCreatedRevisionName") == candidate,
        "Candidate is not the latest created revision",
    )
    require(
        current["spec"]["template"]["spec"]["containers"][0].get("image")
        == f"{IMAGE}@{digest}",
        "Service template digest differs",
    )
    uid = current["metadata"]["uid"]
    old = cloud.revision(previous)
    ready(old, previous, uid, previous_digest)
    require(
        template_config({"spec": old["spec"], "metadata": old["metadata"]})
        == template_config(before["spec"]["template"]),
        "Serving revision runtime differs from baseline",
    )
    new = cloud.revision(candidate)
    ready(new, candidate, uid, digest)
    require(
        template_config({"spec": new["spec"], "metadata": new["metadata"]})
        == template_config(before["spec"]["template"]),
        "Candidate runtime differs from baseline",
    )
    build = cloud.build(build_id)
    require(
        build.get("status") == "SUCCESS" and build.get("serviceAccount") == BUILDER,
        "Build did not succeed using the dedicated SA",
    )
    require(
        any(
            image.get("digest") == digest
            and image.get("name") in (IMAGE, IMAGE + ":latest")
            for image in build.get("results", {}).get("images", [])
        ),
        "Build did not push the candidate image",
    )
    annotations = current["metadata"].get("annotations", {})
    require(
        annotations.get("run.googleapis.com/build-id") == build_id
        and annotations.get("run.googleapis.com/build-service-account") == BUILDER,
        "Service build provenance differs",
    )
    source = build.get("source", {}).get("storageSource", {})
    verify_archive(cloud.source_archive(source), cloud.source_files(source_sha))
    registry = cloud.cli(
        "artifacts", "docker", "images", "describe", f"{IMAGE}@{digest}"
    )
    require(
        registry.get("image_summary", {}).get("digest") == digest,
        "Registry digest readback differs",
    )
    builder_guard(cloud)
    # The final v1 snapshot and v2 generation/etag close read-to-write races.
    latest = cloud.service()
    require(latest == current, "Service changed during validation")
    v2 = cloud.api("GET", RESOURCE)
    require(
        v2.get("name") == RESOURCE
        and v2.get("uid") == uid
        and str(v2.get("generation")) == str(latest["metadata"].get("generation")),
        "v1/v2 service identity or generation differs",
    )
    require(isinstance(v2.get("etag"), str) and v2["etag"], "Service etag missing")
    require(
        v2.get("reconciling", False) is False
        and str(v2.get("observedGeneration")) == str(v2.get("generation")),
        "Service reconciliation is not complete",
    )
    require(
        v2.get("template", {}).get("containers", [{}])[0].get("image")
        == f"{IMAGE}@{digest}",
        "v2 template digest differs",
    )
    traffic = v2.get("traffic", [])
    require(
        len(traffic) == 1
        and traffic[0].get("percent") == 100
        and not traffic[0].get("tag"),
        "v2 traffic differs",
    )
    if current["spec"]["traffic"][0].get("latestRevision") is True:
        require(
            traffic[0].get("type") == "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST",
            "v1/v2 allocation differs",
        )
    else:
        require(
            traffic[0].get("type") == "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION"
            and traffic[0].get("revision") == route(current),
            "v1/v2 allocation differs",
        )
    return current, v2


def promote(
    cloud,
    before,
    before_policy,
    previous,
    previous_digest,
    candidate,
    digest,
    build_id,
    source_sha,
    apply=False,
):
    current, v2 = validate(
        cloud,
        before,
        before_policy,
        previous,
        previous_digest,
        candidate,
        digest,
        build_id,
        source_sha,
    )
    result = {
        "service": SERVICE,
        "revision": candidate,
        "digest": digest,
        "build_id": build_id,
        "image_source_sha": source_sha,
        "write_count": 0,
    }
    if route(current) == candidate and current["spec"]["traffic"] == [
        {"percent": 100, "revisionName": candidate}
    ]:
        return {**result, "status": "already_promoted"}
    if not apply:
        return {**result, "status": "plan_only", "expected_previous_revision": previous}
    body = {
        "name": RESOURCE,
        "etag": v2["etag"],
        "traffic": [
            {
                "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
                "revision": candidate,
                "percent": 100,
            }
        ],
    }
    operation = cloud.api("PATCH", RESOURCE, body)
    for _ in range(60):
        if operation.get("done"):
            break
        require(
            isinstance(operation.get("name"), str),
            "Operation name missing; outcome unknown",
        )
        time.sleep(2)
        operation = cloud.api("GET", operation["name"])
    require(
        operation.get("done") is True and "error" not in operation,
        "Traffic operation failed or exceeded bounded reads; do not retry write",
    )
    final = cloud.service()
    require(
        final.get("status", {}).get("latestCreatedRevisionName") == candidate
        and final["spec"]["template"]["spec"]["containers"][0].get("image")
        == f"{IMAGE}@{digest}",
        "Candidate changed after traffic write",
    )
    require(
        any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in final.get("status", {}).get("conditions", [])
        ),
        "Service is not Ready after traffic write",
    )
    require(
        route(final) == candidate
        and final["spec"]["traffic"] == [{"percent": 100, "revisionName": candidate}],
        "Final traffic is not the explicit candidate 100%",
    )
    require(
        service_config(final) == service_config(before)
        and final["metadata"].get("uid") == before["metadata"].get("uid"),
        "Settings changed after traffic write",
    )
    require(
        policy_config(cloud.policy()) == policy_config(before_policy),
        "IAM changed after traffic write",
    )
    builder_guard(cloud)
    ready(cloud.revision(candidate), candidate, final["metadata"]["uid"], digest)
    return {**result, "status": "promoted", "write_count": 1}


def deploy(cloud, source_sha):
    before, policy = cloud.service(), cloud.policy()
    previous = route(before)
    previous_digest = cloud.revision(previous)["status"]["imageDigest"].rsplit("@", 1)[
        -1
    ]
    cloud.deploy_source(source_sha)
    after = cloud.service()
    candidate = after["status"]["latestCreatedRevisionName"]
    digest = after["spec"]["template"]["spec"]["containers"][0]["image"].rsplit("@", 1)[
        -1
    ]
    build_id = after["metadata"]["annotations"]["run.googleapis.com/build-id"]
    return promote(
        cloud,
        before,
        policy,
        previous,
        previous_digest,
        candidate,
        digest,
        build_id,
        source_sha,
        apply=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("deploy", "resume"))
    parser.add_argument("--before-service", type=Path)
    parser.add_argument("--before-iam", type=Path)
    parser.add_argument("--previous-revision")
    parser.add_argument("--previous-digest")
    parser.add_argument("--revision")
    parser.add_argument("--digest")
    parser.add_argument("--build-id")
    parser.add_argument("--image-source-sha")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    require(
        os.environ.get("OPS_BUILD_PROFILE") == "yadora-prod-gcp-ops",
        "Explicit fixed build profile required",
    )
    for name, expected in {
        "PROJECT_ID": PROJECT,
        "GOOGLE_CLOUD_PROJECT": PROJECT,
        "REGION": REGION,
        "SERVICE": SERVICE,
        "RUNTIME_SA": RUNTIME,
        "BUILD_SERVICE_ACCOUNT": BUILDER,
        "BUILD_SA_EMAIL": BUILDER.rsplit("/", 1)[-1],
        "BUILD_SOURCE_BUCKET": BUCKET,
        "BUILD_ARTIFACT_REPOSITORY": "cloud-run-source-deploy",
    }.items():
        require(
            not os.environ.get(name) or os.environ[name] == expected,
            f"{name} conflicts with the fixed profile",
        )
    cloud = Cloud()
    require(
        not command(["git", "status", "--porcelain"]), "Source worktree must be clean"
    )
    if args.mode == "deploy":
        require(
            not any(
                (
                    args.before_service,
                    args.before_iam,
                    args.previous_revision,
                    args.previous_digest,
                    args.revision,
                    args.digest,
                    args.build_id,
                    args.image_source_sha,
                    args.apply,
                )
            ),
            "Deploy does not accept resume inputs",
        )
        source_sha = command(["git", "rev-parse", "HEAD"]).decode().strip()
        result = deploy(cloud, source_sha)
    else:
        require(
            all(
                (
                    args.before_service,
                    args.before_iam,
                    args.previous_revision,
                    args.previous_digest,
                    args.revision,
                    args.digest,
                    args.build_id,
                    args.image_source_sha,
                )
            ),
            "Resume requires baseline files and all explicit expected identities",
        )
        result = promote(
            cloud,
            decode(args.before_service.read_bytes()),
            decode(args.before_iam.read_bytes()),
            args.previous_revision,
            args.previous_digest,
            args.revision,
            args.digest,
            args.build_id,
            args.image_source_sha,
            apply=args.apply,
        )
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError, TypeError, ValueError, OSError) as exc:
        message = (
            str(exc)
            if isinstance(exc, RuntimeError)
            else "Invalid input/response; no automatic retry"
        )
        print(f"Promotion: {message}", file=sys.stderr)
        raise SystemExit(1) from None
