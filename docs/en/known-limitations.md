# Known limitations and improvement plan

日本語: [docs/known-limitations.md](../known-limitations.md)

This page records the additional review of `main` at `629abaf27b610b9c3a8d1c850c8e03359633cb7c` on 2026-10-14.
The review inspected source and pinned SDK dependencies and reproduced cases with synthetic responses and mocks.
It did not confirm incidents in production or inspect users' cloud permissions or deployed services.

These are 11 unresolved defects and two conditional risks, separate from the 14 fixes in [PR #4](https://github.com/atsuki-sakai/cloud-ops-team/pull/4).
Adding this document does not fix them. The proposed fixes below have not been implemented.
P1 items directly affect operational decisions and should be addressed first. P2 items produce incorrect results or behavior under the stated conditions.

See [Configuration](configuration.md) for settings and [Deploying to Google Cloud](deployment-gcp.md) for deployment instructions.

## Confirmed defects

### A01 — P1: A reference PR from another repository becomes fix evidence

Source: [Issue and PR tools](../../app/tools/issues.py#L819)

- **Current behavior:** PRs mentioning an Issue are identified only by number. A merged PR in another repository contributes a candidate merge time, while detail retrieval reads the same PR number from the configured repository.
- **Impact:** Unrelated changes, CI, or merge times can lead to an incorrect verified verdict. An incorrect closure by a live model was not reproduced.
- **Temporary measure:** Run fix verification with `OPS_DRY_RUN=true` and have an operator check the repository, change, CI, deployment, and recurrence evidence. Setting only `OPS_AUTO_CLOSE_ISSUES=false` still allows the `ops:verified` label and comments.
- **Proposed fix:** Identify PRs by repository and a unique ID, distinguish mentions from closing relationships, and retrieve cross-repository details from their actual repository. [GitHub reference event specification](https://docs.github.com/en/graphql/reference/issues#crossreferencedevent)

### A02 — P2: Overlapping runs can create duplicate Issues and recurrence comments

Source: [Issue creation and recurrence reporting](../../app/tools/issues.py#L304)

- **Current behavior:** There is no shared exclusion between searching and creating an Issue, or between checking the cooldown and posting a comment.
- **Impact:** Separate instances, for example, can read the same result before writing and create duplicate Issues or comments within the same period. This is not a claim that synchronous tools run in parallel within one ADK invocation.
- **Temporary measure:** Avoid overlapping runs across all services and manual callers writing for the same target, and check for duplicates. Reducing the instance count to one does not guarantee exclusion across other writers.
- **Proposed fix:** Add shared exclusion and operation records keyed by repository, provider, target, and fingerprint. Handle retries after a write succeeds but its response is lost without writing twice.

### A03 — P2: The per-run Issue limit carries over to the next request

Source: [Issue counter](../../app/tools/issues.py#L86)

- **Current behavior:** The creation counter is stored in session state and remains for the next request in that session.
- **Impact:** After reaching `OPS_MAX_ISSUES_PER_RUN`, new findings in later requests can also be skipped. Runs that create a fresh session each time have different exposure.
- **Temporary measure:** Start a new investigation in a new session.
- **Proposed fix:** Store the counter in invocation state or a budget keyed by invocation ID. [ADK state scopes](https://github.com/google/adk-docs/blob/main/docs/sessions/state.md)

### A04 — P2: Report creation bypasses the cap and within-run deduplication

Source: [Report publishing](../../app/tools/issues.py#L391)

- **Current behavior:** `publish_report` neither checks nor updates `OPS_MAX_ISSUES_PER_RUN`. Repeated calls can create the same report multiple times in one run.
- **Impact:** The total number of new Issues can exceed the configured limit.
- **Temporary measure:** Do not treat this setting as a strict limit including reports; check the number created. Use dry-run when writes need to be restricted.
- **Proposed fix:** Share the run budget across all creation paths and record report operations to prevent duplicates of the same kind within a run. If reports have a separate allowance, document its configuration and limit.

### A05 — P2: Service account key auditing checks only the first 60 accounts

Source: [Service account key auditing](../../app/providers/gcp/security.py#L226)

- **Current behavior:** Although accounts are listed, key inspection stops after the first 60. The result does not report unchecked accounts or incomplete coverage.
- **Impact:** Projects with 61 or more accounts can receive no findings despite long-lived keys on later accounts.
- **Temporary measure:** Inspect the complete account list and keys separately. No findings are not evidence that all accounts were checked.
- **Proposed fix:** Inspect every account, or return unchecked identities, counts, and an incomplete status when a limit is necessary.

### A06 — P2: Failed Cloud Run IAM reads are reported as non-public

Source: [Cloud Run public access auditing](../../app/providers/gcp/security.py#L288)

- **Current behavior:** A failed public access policy read retains the initial `public:false` value and omits the read error.
- **Impact:** Services that could not be checked due to permissions or timeouts can appear confirmed non-public.
- **Temporary measure:** Check access policies separately with permission to read them. Do not infer non-public access from a result whose API read success cannot be confirmed.
- **Proposed fix:** Distinguish unknown from public and non-public, and return read errors, checked counts, and incomplete coverage.

### A07 — P2: Enabled MySQL IAM authentication is reported as disabled

Source: [Cloud SQL IAM authentication check](../../app/providers/gcp/security.py#L597)

- **Current behavior:** MySQL is checked using the PostgreSQL flag `cloudsql.iam_authentication`. The MySQL flag is `cloudsql_iam_authentication`.
- **Impact:** Enabled MySQL IAM authentication can produce a disabled-authentication finding. This is separate from the MySQL PITR fix in PR #4.
- **Temporary measure:** Check the actual engine and flag before acting on a MySQL finding. [Google Cloud MySQL setup instructions](https://docs.cloud.google.com/sql/docs/mysql/create-edit-iam-instances)
- **Proposed fix:** Normalize engine-specific settings into a common authentication state before checking them, and report unsupported engines as unknown.

### A08 — P2: IAM expiry and conditions are lost from audit results

Source: [IAM retrieval and checks](../../app/providers/gcp/security.py#L108)

- **Current behavior:** Binding conditions are not retained in results; checks use only the role and member.
- **Impact:** Expired permissions, or permissions restricted to resources or other conditions, can be reported as currently available unconditional powerful permissions.
- **Temporary measure:** Review each finding alongside conditions in the original IAM policy. Arbitrary expressions need to be checked against the conditions of the relevant request.
- **Proposed fix:** Retain conditions, recognize simple expired time conditions, and mark conditions that cannot be evaluated for review. [IAM Binding condition specification](https://docs.cloud.google.com/iam/docs/reference/rpc/google.iam.v1)

### A09 — P2: Expired SQL authorized networks are reported as world-open

Source: [Cloud SQL authorized network checks](../../app/providers/gcp/security.py#L450)

- **Current behavior:** Only network values are retained, losing `expirationTime`.
- **Impact:** An expired `0.0.0.0/0` entry on an instance with a public IP can produce a critical finding of current world-open access.
- **Temporary measure:** Check the expiry of networks in findings. Separate cleanup of expired settings from current connection authorization.
- **Proposed fix:** Retain expiry and evaluate currently active entries. Treat invalid expiry values as unknown. [Cloud SQL ACL specification](https://docs.cloud.google.com/sql/docs/postgres/admin-api/rest/v1/instances)

### A10 — P2: The standard deployment advertises an unusable A2A URL

Source: [A2A card generation](../../app/app_utils/a2a.py#L104), [Deployment environment variables](../../scripts/gcp/deploy_env.py#L14)

- **Current behavior:** Without `APP_URL`, the card advertises `http://0.0.0.0:8000/a2a/app`. The standard deployment does not forward `APP_URL`, and the container listens on port 8080.
- **Impact:** A2A clients discovering their endpoint from the card cannot connect to the deployed service.
- **Temporary measure:** When using A2A, set Cloud Run's `APP_URL` to the actual service URL after deployment and inspect the card URL. Check again after every redeployment because environment variables are replaced.
- **Proposed fix:** Set `APP_URL` from the service URL once it is known during initial deployment, and verify that the card matches the actual endpoint.

### A11 — P2: A custom project context file setting is lost during deployment

Source: [Deployment environment variables](../../scripts/gcp/deploy_env.py#L39)

- **Current behavior:** The standard deployment does not forward `OPS_CONTEXT_FILE`, even when set. The deployed service uses the default `config/ops-context.md`.
- **Impact:** Project knowledge and exclusion rules used locally are omitted from deployed instructions, even if the custom file exists in the image.
- **Temporary measure:** Use the default path included in the image, or set Cloud Run's `OPS_CONTEXT_FILE` after deployment. Check it again after redeployment because environment variables are replaced. Files present only on the local machine are unavailable to the deployed service.
- **Proposed fix:** Forward the setting, validate that the file exists and can be read, and share the configuration definition between local and deployed execution.

## Conditional operational risks

### R01: The reason an investigation could not complete may not survive the run

Source: [Trigger handling](../../app/fast_api_app.py#L87), [Session storage](../../app/app_utils/services.py#L57)

The pinned SDK returns HTTP success when execution finishes without an exception. Its response does not include the final explanation that investigation was blocked by missing permissions or similar problems, and sessions are in memory by default.
If no Issue was created and the instance terminates, that explanation may no longer be available. HTTP success is not evidence of audit completion. The SDK's success response itself is expected behavior.

**Temporary measure:** Provide a way for operators to inspect and save the final result. **Proposed improvement:** Persist the run ID, coverage, unknown items, errors, and a redacted final result. Distinguish a run being executed from an audit being completed.

### R02: A2A session ownership is not separated between multiple callers

Source: [A2A request handling](../../app/app_utils/a2a.py#L118)

The current setup does not propagate the caller authenticated by Cloud Run into SDK session ownership. The default converter derives the owner ID from `context_id`.
**When multiple callers have invocation permission and another caller's `context_id` is known or shared**, they can be treated as the same session owner. This was checked with different synthetic authorization headers and the same ID, not by retrieving another user's production data.

**Temporary measure:** Limit usage to one trusted operational principal instead of using it as a shared A2A service requiring isolation between users. **Proposed improvement:** Bind verified caller identity to sessions and enforce ownership when retrieving or resuming sessions and tasks. Do not trust self-declared headers as authentication evidence.

## Improvement order and structure

| Order | Items | Direction |
|---|---|---|
| 1 | A01, A05, A06 | Correct PR identity and coverage; report insufficient evidence as unknown |
| 2 | A02, A03, A04 | Share write budgets, exclusion, and retry handling across GitHub operations |
| 3 | A07, A08, A09 | Retain engine differences, expiry, and conditions in retrieved results |
| 4 | A10, A11 | Share configuration definitions across local execution, deployment, and doctor |
| 5 | R01, R02, permission scope | Match persistent results and caller boundaries to the operating model; allow access only to needed secrets |

The proposed split for [GCP auditing](../../app/providers/gcp/security.py) is IAM, keys, Cloud Run, and Cloud SQL, separating retrieval from checks.
Retrieved results should retain coverage, read errors, timestamps, and conditions. [GitHub tools](../../app/tools/issues.py) should separate Issue operations, PR evidence, and report publishing while sharing write controls.
Consider checking PR, SHA, CI, deployment, and observation-period evidence in code for verified verdicts, with the model gathering evidence and explaining results.
Tool-call and runtime guidelines in prompts also need code-enforced budgets.

[Bootstrap](../../scripts/gcp/bootstrap.sh#L28) grants secret read access at project scope.
If only the GitHub token is required, narrow access to that secret. This is a recommendation about the script, not a confirmation of current production grants. [Secret Manager least privilege](https://docs.cloud.google.com/secret-manager/docs/access-control)

Changes should add regression cases for the conditions above and verify concurrency, read failures, unknown reporting, and persistent results across complete workflows.
Unit tests, live-model evaluations, authenticated deployment checks, and scheduled-run completion are separate validations.
