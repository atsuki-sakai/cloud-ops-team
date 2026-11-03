# Migrating the build identity for Yadora's existing service

日本語版（メイン）: [docs/build-identity-yadora.md](../build-identity-yadora.md)

Refs [atsuki-sakai/Yadora#1652](https://github.com/atsuki-sakai/Yadora/issues/1652). This is a migration contract and runbook, not a record of live IAM application, builds or Editor removal.

## Fixed target

Only the explicit `OPS_BUILD_PROFILE=yadora-prod-gcp-ops` profile selects this combination. `scripts/gcp/build_profile.sh` owns the values; see [configuration](configuration.md#deployment-make-gcp-deploy).

| Target | Value |
|---|---|
| Deployment project / region | `yadora-prod` / `asia-northeast1` |
| Existing Cloud Run service | `gcp-ops-team` |
| Runtime SA to preserve | `gcp-ops-team@yadora-prod.iam.gserviceaccount.com` |
| Dedicated build SA | `gcp-ops-build@yadora-prod.iam.gserviceaccount.com` |
| Existing source bucket | `run-sources-yadora-prod-asia-northeast1` |
| Existing Docker repository | `cloud-run-source-deploy` |

Conflicting target, identity, bucket or repository inputs are rejected. A missing or mismatched existing service/runtime identity, source bucket or Docker repository also stops the operation. Other projects/services keep their existing generic deployment behavior. Selecting `yadora-prod/gcp-ops-team` without this profile fails rather than falling back to the default Compute SA.

## Before making changes

Use a reviewed clean checkout, not an unrelated feature branch in an existing Desktop checkout. The deployed PR #23 merge `2a16824210e50c02fc48a24c4544f8575d854e5f` and migration base main `3ca4e6d57c8f46486e6ca351f1d98faf3a4ef754` differ in five changelog/evaluation-recording script/documentation/unit-test files, with no app, dependency, Dockerfile or deployment-script differences. Recheck current main and the intended release immediately before execution; do not mix in undeployed application work.

Privately save the image, serving revision/traffic, runtime SA, environment/secret references, scaling, timeout, service IAM and the policies whose bindings will change. Do not publish personal data, tokens, cookies or secret values. `artifacts/` is excluded from Git and source uploads.

## Build-only bootstrap

Do not run the generic `make gcp-bootstrap` for this migration: it also configures runtime, secrets and monitoring permissions. The build-only target checks existing resources and manages only the dedicated SA and scoped bindings. It does not enable APIs or create buckets, repositories or runtime resources.

```bash
OPS_BUILD_PROFILE=yadora-prod-gcp-ops make gcp-build-bootstrap
# Only after reviewing the read-only checks and displayed target, apply separately:
OPS_BUILD_PROFILE=yadora-prod-gcp-ops make gcp-build-bootstrap ARGS=--apply
```

The default is plan-only with no IAM changes. Initial application creates the dedicated SA plus three bindings; an existing SA needs only the bindings.

| Grant level | Role |
|---|---|
| Project `yadora-prod` | `roles/logging.logWriter` |
| Repository `cloud-run-source-deploy` | `roles/artifactregistry.writer` |
| Source bucket `run-sources-yadora-prod-asia-northeast1` | `roles/storage.objectViewer` |

Use an approved operator such as the Owner. If delegating execution to a non-Owner, separately scope `iam.serviceAccounts.actAs` to the dedicated SA; the script does not grant project-wide operator authority. Permission errors are not interpreted as an absent account. Disabled/mismatched accounts or unexpected project roles stop execution. No Editor, runtime, Secret, KMS or DB authority is added.

After application, inspect all grant levels, verify that the new SA has no Editor and record the exact bindings. The [official source-build identity guide](https://docs.cloud.google.com/run/docs/configuring/services/build-service-account) documents `--build-service-account`. The candidate role set is proven sufficient only after a real build/push succeeds using that non-Editor SA alone. Do not automatically add `roles/run.builder` or Editor after a failure.

## Source deployment preserving existing runtime settings

```bash
OPS_BUILD_PROFILE=yadora-prod-gcp-ops make gcp-deploy
```

The profile supplies a temporary source from the recorded SHA and the dedicated `--build-service-account` to the existing service. It does not resend generic deployment flags for environment files, runtime SA, sizing/scaling/timeout, secrets, labels or authentication/IAM. Existing runtime settings are retained instead of reapplying `.env` values such as dry-run mode, model or monitored target. The profile itself is not a runtime environment variable.

Only the fixed profile extracts clean HEAD's `git archive` into a private temporary directory (directories 0700, files 0600 / executables 0700). It verifies that `.gcloudignore.yadora` exactly preserves the base `.gcloudignore` bytes plus only the two fixed exclusions for `.ruff_cache/` and the profile file itself, then writes those bytes as the temporary source's `.gcloudignore`. Deployment uses the supported `--source TEMP_DIRECTORY` argument and cleans the staging directory on success, failure and timeout. Deploy and manifest child processes force `CLOUDSDK_GCLOUDIGNORE_ENABLED=true` so caller settings cannot disable exclusions. The original checkout/base ignore, global configuration/parent environment and other profiles remain unchanged. Whitespace/include changes, missing/reordered base content and re-inclusions fail before building.

On 2026-11-03, SDK 568.0.0 did not register `gcloud run deploy --ignore-file` as an actual flag. The invocation from main `e1680afd…` stopped during argument parsing; actual reads confirmed an unchanged service snapshot and zero new dedicated-SA builds. Help prose alone does not establish support: inspect the actual parser and upload manifest. Follow the [official run deploy arguments](https://docs.cloud.google.com/sdk/gcloud/reference/run/deploy) and [upload-root `.gcloudignore` behavior](https://docs.cloud.google.com/sdk/gcloud/reference/topic/gcloudignore), without unsupported flags or global configuration changes.

The fixed profile builds and creates a revision with `--no-traffic`, validates the saved single 100% route, then assigns 100% to this revision **by name**. An existing explicit revision pin does not move merely because source deployment succeeds. Exit 0 or HTTP 200 from the old revision is not evidence that the new image serves traffic.

Before promotion, it reads latest-created revision, immutable digest, Ready, service UID, runtime settings and IAM, plus the actual successful Build's identity, source bucket and pushed digest. After comparing generations it sends exactly one [traffic-only PATCH](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.services/patch) with the freshly read [Service etag](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.services#Service). It does not enable LATEST following or update runtime/IAM. Conflicts, failed reads, mismatches and unknown operations stop without write retries. An already verified 100% pin to the same revision returns without another write.

Verify actual Cloud Build serviceAccount, source bucket, destination repository, successful status, pushed digest, log output and the new SA's permissions. Check Cloud Run Ready/traffic/digest and an authenticated read path such as `/list-apps`. HTTP success does not establish audit-job completion or successful GitHub writes. Do not trigger jobs that publish externally without authorization.

## Resume an existing candidate without rebuilding

`make gcp-promote-existing` is resume-only: it performs no build, source deployment or IAM operation. Supply the private service/service-IAM JSON saved before source deployment, expected old/new revisions and digests, actual Build ID and **the source SHA that built that image**. The default only reads and plans; `--apply` enables the same conditional traffic-only PATCH.

```bash
PROMOTION_ARGS="--before-service artifacts/before-service.json --before-iam artifacts/before-service-iam.json --previous-revision PREVIOUS_REVISION --previous-digest sha256:PREVIOUS_DIGEST --revision CANDIDATE_REVISION --digest sha256:CANDIDATE_DIGEST --build-id ACTUAL_BUILD_ID --image-source-sha ACTUAL_40_CHAR_SOURCE_SHA"
OPS_BUILD_PROFILE=yadora-prod-gcp-ops make gcp-promote-existing ARGS="$PROMOTION_ARGS"
# Only after reviewing the plan/evidence and authorizing the live change:
OPS_BUILD_PROFILE=yadora-prod-gcp-ops make gcp-promote-existing ARGS="$PROMOTION_ARGS --apply"
```

Replace uppercase values with actual observations. The helper extracts the recorded SHA's `git archive` into a private temporary directory and computes its entire upload set using that revision's profile ignore file (or base ignore for older SHAs), then compares the generation-pinned actual GCS source zip. Deletions or changed ignore rules in the current checkout cannot shrink that expected set. Dirty checkouts, another source, changed content and extra/missing files stop execution. The SHA is an operator-recorded source, not a fabricated Cloud Build Git-provenance field.

The older 2026-11-03 candidate `gcp-ops-team-00009-rlx` / `ee4cd770…` was actually built/pushed from source SHA `e1788e0…` by Build `dc66ced2-8926-47de-95d2-a7796a4fbb83`, but its source zip also included 16 untracked Ruff-cache files. All 127 tracked upload files matched, yet the complete set differed, so strict resume rejects it. Retain that candidate/evidence without exceptions and run normal `make gcp-deploy` once from new main containing the profile ignore file. Do not claim that old candidate was built from the new helper SHA.

Multiple routes, tags, another service/revision, non-Ready state, runtime/IAM differences and concurrent changes stop rather than bypass checks. If an operation outcome is unknown, inspect actual state without rebuilding or repeating PATCH; resume verification with the same expected values. Separately verify the new 100% revision/digest and authenticated read-only health afterwards.

## Recovery and completion

Investigate build failures without adding a default-Compute-SA fallback. Review restoration of traffic to the saved revision if needed after deployment; do not delete existing images/tags or overwrite entire IAM policies.

This repository's migration alone does not close Yadora #1652. Resolve retained Yadora build, historical Rust-validation and Trivy-cache paths through migration or an explicit retirement decision. Only after real validation should the default Compute SA's Editor binding be removed and required operations rechecked. Success using the old Editor SA is not evidence for replacement permissions.

Local CLI fixtures check target isolation, rejection paths and omission of runtime-setting flags. They do not prove live IAM sufficiency or propagation. Remediation of scanner findings is tracked separately from this build-identity change.
