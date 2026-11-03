# Production record: Yadora build-SA migration

日本語版（メイン）: [production-yadora-build-identity-2026-11-03.md](../production-yadora-build-identity-2026-11-03.md)

At 2026-11-03 08:24:44 UTC, the existing `gcp-ops-team` service in `yadora-prod` / `asia-northeast1` completed a real build/push with a dedicated non-Editor Build SA, promotion of the new revision to 100%, and authenticated read-path checks. See the [migration runbook](build-identity-yadora.md) and [Yadora #1652](https://github.com/atsuki-sakai/Yadora/issues/1652). The default Compute SA retains Editor; this migration alone does not close that issue.

## Serving image and actual source

| Item | Actual observation |
|---|---|
| Main SHA that built this image | `2c97825a5feb37050a0d488d13931afe60193966` ([PR #29](https://github.com/atsuki-sakai/cloud-ops-team/pull/29)) |
| Cloud Build ID | `b2f8f004-2bee-4317-b2b3-7083f64547c9` |
| Build status | `SUCCESS` (08:21:25–08:22:17 UTC) |
| Build SA | `gcp-ops-build@yadora-prod.iam.gserviceaccount.com` |
| Source bucket | `run-sources-yadora-prod-asia-northeast1` |
| Image | `asia-northeast1-docker.pkg.dev/yadora-prod/cloud-run-source-deploy/gcp-ops-team@sha256:874a23f5de3f219c5ffe1d76d450869c087fb99a9a127067aca22da09bb079e9` |
| Serving revision | `gcp-ops-team-00010-hqr`, Ready, sole explicitly named revision at 100% |
| Previous 100% revision | `gcp-ops-team-pr23-2a16824` |
| Previous image digest | `sha256:4011722df35db78d918e62155c33fa7477d30f280730a3469dd4db90dab4bf60` |

The later commit adding this record did not build the image. The retained runtime `source-commit` label is not source proof: the actual generation-pinned GCS zip referenced by the Build matched the complete expected upload from the SHA above, with 129 files / 1,727,613 bytes and zero extra, missing or changed files. Archive SHA-256: `34f1d5f297fe7ee44779eddb38f4bab7bf8244ca10a1ce9581d6275d7b9c557d`. No secret files, private artifacts or Ruff caches were included.

## Permissions and preserved settings

The fixed profile from [PR #27](https://github.com/atsuki-sakai/cloud-ops-team/pull/27) bootstrapped only the dedicated SA and the following three bindings at 06:09:44 UTC. It did not bootstrap APIs, buckets, repositories, runtime or Secrets.

| Scope | Dedicated Build SA role |
|---|---|
| Project `yadora-prod` | Only `roles/logging.logWriter` |
| Repository `cloud-run-source-deploy` | Only `roles/artifactregistry.writer` |
| Source bucket | Only `roles/storage.objectViewer` |

The SA was enabled, its own IAM bindings were empty, and it had no Editor. Source reads and image pushes succeeded under this SA; the Build used `CLOUD_LOGGING_ONLY`, and ten actual build-log entries were observed. This is not success attributed to the old Editor SA. Initial bucket IAM application changed only metageneration/update time; retention, encryption, public-access prevention and other settings stayed unchanged.

The final deployment invoked `OPS_BUILD_PROFILE=yadora-prod-gcp-ops make gcp-deploy` once from the clean main SHA above. After creating a `--no-traffic` candidate, it checked old/new runtime, actual Build/source/SA/digest/Ready and service UID/generation/etag, then sent one traffic-only PATCH pinning 100% to the new revision name. Actual v1/v2 reads confirmed that route and completed reconciliation. LATEST following was not enabled.

Private before/after snapshots confirmed unchanged runtime SA, model, environment, secret references, scaling, timeout, labels, ingress/authentication, project/repository/bucket/service IAM, and source/repository configuration. Full snapshots of the other four Yadora services were unchanged. Raw policies, environment values, personal data and tokens are not published here.

## Real reads and validation

- After verifying that the new revision was the sole 100% route, authenticated `GET /list-apps` returned HTTP 200 and the JSON array `["app"]`.
- `GET /a2a/app/.well-known/agent-card.json` returned HTTP 200, `name=cloud_ops_team`, and two nonempty `supportedInterfaces` entries.
- No job/agent execution or GitHub posting was triggered. These reads do not establish audit-job completion, successful posting or vulnerability remediation.
- Final Python 3.13/3.11 unit matrices each passed 622 tests, run sequentially. Ruff/format/codespell/ty and independent review passed with no concrete P1/P2 findings. Six earlier real integration successes apply to the unchanged app tree.
- Earlier high-load matrices retained 620 successes plus one routing child-process timeout each. At the final hashes, the isolated case passed before both full matrices completed sequentially; timeouts were not relabeled as successes. The latest GitHub Actions run stopped before jobs began due to a platform account condition, with zero steps. Automated CI is not reported as successful.

## Retained candidates and remaining work

Earlier Build `dc66ced2-8926-47de-95d2-a7796a4fbb83` actually built/pushed `ee4cd770…` from source `e1788e0c6f5f1995e8710a7c7e25e80936732cb8` with the dedicated SA, but the existing explicit traffic pin did not move, leaving candidate `gcp-ops-team-00009-rlx` unpromoted. Its source zip included 16 extra Ruff-cache files. The candidate/evidence were retained without accepting an exception. [PR #28](https://github.com/atsuki-sakai/cloud-ops-team/pull/28) added guarded revision promotion; PR #29 added supported source staging and child-only forced ignore parsing.

An intermediate attempt from main `e1680afd3fe868b0e48fb392a2435ebb7317adad` stopped during argument parsing because SDK568.0.0 rejected `run deploy --ignore-file`, before any build. Actual post-failure reads showed an unchanged full service snapshot and zero new builds. This was not a retry of a write with unknown outcome.

Old images, revisions and tags remain intact. Restoring traffic to a saved revision requires reviewing current state and etag separately. Editor removal is explicitly on hold. Other retained paths' migration/continuation/retirement decisions and validation, and remediation of existing vulnerabilities, remain separate work.
