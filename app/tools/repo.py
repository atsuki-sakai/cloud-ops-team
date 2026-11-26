"""Repository security posture (GitHub): security features, branch protection,
security workflow health, Dockerfile base images and toolchain pins."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.config import CONFIG
from app.core.findings import error_result, finding, result
from app.integrations.github import gh_graphql, gh_rest, repo_path

_SECURITY_WORKFLOW = re.compile(
    r"(?i)security|scan|audit|drift|trivy|codeql|osv|depend"
)
_VERSION_FILES = (
    ".nvmrc",
    ".node-version",
    "rust-toolchain.toml",
    "rust-toolchain",
    ".python-version",
    "package.json",
    ".github/dependabot.yml",
    "renovate.json",
)


def _gh_get(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    list_key: str | None = None,
    expect_list: bool = False,
) -> tuple[bool, Any]:
    """Reject malformed successful responses before treating them as observations."""
    ok, payload = gh_rest("GET", path, params=params)
    if not ok:
        return ok, payload
    valid = isinstance(payload, list if expect_list else dict)
    if valid and expect_list:
        valid = all(isinstance(item, dict) for item in payload)
    elif valid and list_key:
        items = payload.get(list_key)
        valid = isinstance(items, list) and all(
            isinstance(item, dict) for item in items
        )
        if valid and list_key == "tree":
            valid = all(isinstance(item.get("path"), str) for item in items)
    if not valid:
        return False, {
            "status": "error",
            "message": f"Unexpected response shape for {path}",
        }
    return True, payload


RecordError = Callable[[str, dict[str, Any]], None]


def _raw_files(paths: list[str], ref: str) -> tuple[bool, dict[str, Any]]:
    """Fetch several files at ``ref`` with one GraphQL query instead of a GET each.

    Returns ``(True, {path: text or None})`` (``None`` when the path is not a
    text blob) or ``(False, error)`` when the query itself failed.
    """
    if not paths:
        return True, {}
    owner, _, name = CONFIG.github_repo.partition("/")
    declarations = "".join(f", $e{i}: String!" for i in range(len(paths)))
    fields = " ".join(
        f"f{i}: object(expression: $e{i}) {{ ... on Blob {{ text }} }}"
        for i in range(len(paths))
    )
    query = (
        f"query($owner: String!, $name: String!{declarations}) "
        f"{{ repository(owner: $owner, name: $name) {{ {fields} }} }}"
    )
    variables: dict[str, Any] = {"owner": owner, "name": name}
    variables.update({f"e{i}": f"{ref}:{path}" for i, path in enumerate(paths)})
    ok, data = gh_graphql(query, variables)
    if not ok:
        return False, data
    repository = data.get("repository")
    if not isinstance(repository, dict):
        return False, {
            "status": "error",
            "message": "Unexpected response shape for file contents",
        }
    texts: dict[str, Any] = {}
    for i, path in enumerate(paths):
        blob = repository.get(f"f{i}")
        text = blob.get("text") if isinstance(blob, dict) else None
        texts[path] = text if isinstance(text, str) else None
    return True, texts


def _security_workflows(record_error: RecordError) -> list[dict[str, Any]]:
    """Latest run of every workflow whose name or path looks security related."""
    ok_wf, workflows = _gh_get(
        f"{repo_path()}/actions/workflows",
        params={"per_page": 100},
        list_key="workflows",
    )
    if not ok_wf:
        record_error("security_workflows", workflows)
        return []
    out = []
    for wf in workflows.get("workflows") or []:
        if not _SECURITY_WORKFLOW.search(f"{wf.get('name', '')} {wf.get('path', '')}"):
            continue
        if "id" not in wf:
            record_error(
                "security_workflows",
                {"status": "error", "message": "Workflow is missing id"},
            )
            continue
        # ast-grep-ignore: api-call-in-loop -- one latest run per security workflow; GitHub has no per-workflow latest-run batch endpoint
        ok_r, runs = _gh_get(
            f"{repo_path()}/actions/workflows/{wf['id']}/runs",
            params={"per_page": 1},
            list_key="workflow_runs",
        )
        if not ok_r:
            record_error(f"workflow_runs:{wf['id']}", runs)
        latest = ((runs or {}).get("workflow_runs") or [{}])[0] if ok_r else {}
        out.append(
            {
                "name": wf.get("name"),
                "path": wf.get("path"),
                "state": wf.get("state"),
                "latest_status": latest.get("status"),
                "latest_conclusion": latest.get("conclusion"),
                "latest_run_at": latest.get("created_at"),
                "latest_event": latest.get("event"),
                "url": latest.get("html_url"),
            }
        )
    return out


def _version_pin(path: str, text: str) -> str:
    if path == "package.json":
        m = re.search(r'"engines"\s*:\s*\{[^}]*\}', text, re.DOTALL)
        return m.group(0)[:200] if m else "(no engines field)"
    if path.endswith((".yml", ".json")):
        return "(present)"
    return text.strip()[:120]


def _tree_scan(
    ref: str, record_error: RecordError
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Dockerfile base images (first 6 Dockerfiles) and toolchain version pins."""
    dockerfiles: list[dict[str, Any]] = []
    version_pins: dict[str, str] = {}
    ok_tree, tree = _gh_get(
        f"{repo_path()}/git/trees/{ref}",
        params={"recursive": "1"},
        list_key="tree",
    )
    if not ok_tree:
        record_error("repository_tree", tree)
        return dockerfiles, version_pins
    paths = [
        t.get("path", "") for t in tree.get("tree") or [] if t.get("type") == "blob"
    ]
    docker_paths = [
        p
        for p in paths
        if "node_modules" not in p
        and (
            p.endswith("Dockerfile")
            or "/Dockerfile." in p
            or p.startswith("Dockerfile.")
        )
    ][:6]
    present = set(paths)
    version_paths = [vf for vf in _VERSION_FILES if vf in present]
    ok, texts = _raw_files([*docker_paths, *version_paths], ref)
    if not ok:
        for path in [*docker_paths, *version_paths]:
            record_error(f"content:{path}", texts)
        return dockerfiles, version_pins
    unreadable = {"status": "error", "message": "File is not a readable text blob"}
    for path in docker_paths:
        text = texts[path]
        if text is None:
            record_error(f"content:{path}", unreadable)
        elif text:
            from_lines = [
                line.strip()
                for line in text.splitlines()
                if line.strip().upper().startswith("FROM ")
            ]
            dockerfiles.append({"path": path, "from": from_lines})
    for vf in version_paths:
        text = texts[vf]
        if text is None:
            record_error(f"content:{vf}", unreadable)
        elif text:
            version_pins[vf] = _version_pin(vf, text)
    return dockerfiles, version_pins


def _open_bot_prs(record_error: RecordError) -> dict[str, int | None]:
    """Open dependency-update PRs per bot (``None`` when the search failed)."""
    counts: dict[str, int | None] = {}
    for bot in ("dependabot", "renovate"):
        ok, found = _gh_get(
            "/search/issues",
            params={
                "q": f"repo:{CONFIG.github_repo} is:pr is:open author:app/{bot}",
                "per_page": 1,
            },
        )
        if not ok:
            record_error(f"{bot}_prs", found)
        counts[bot] = (found or {}).get("total_count") if ok else None
    return counts


def get_repo_security_posture() -> dict:
    """Summarise the GitHub repository's security hygiene: security features,
    branch protection, latest runs of security workflows, Dockerfile base
    images and toolchain version pins (for EOL checks), Dependabot PRs."""
    ok, repo = _gh_get(repo_path())
    if not ok:
        return error_result("repo_posture", repo)
    default_branch = repo.get("default_branch", "main")
    sec = repo.get("security_and_analysis")
    sec = {} if sec is None else sec
    if (
        not isinstance(default_branch, str)
        or not isinstance(sec, dict)
        or any(not isinstance(value, dict) for value in sec.values())
    ):
        return error_result(
            "repo_posture",
            {"status": "error", "message": "Unexpected repository metadata shape"},
        )
    errors = []

    def record_error(check: str, payload: dict[str, Any]) -> None:
        errors.append({"check": check, **payload})

    ok_va, alerts = _gh_get(f"{repo_path()}/vulnerability-alerts")
    alerts_enabled = True if ok_va else None
    if not ok_va:
        # Repository admin rights do not prove the token's endpoint permissions.
        # GitHub may mask missing token permissions with the same 404 as disabled alerts.
        record_error("dependabot_alerts", alerts)
    ok_bp, protection = _gh_get(f"{repo_path()}/branches/{default_branch}/protection")
    branch_protection = bool(protection) if ok_bp else None
    if not ok_bp:
        if (
            protection.get("http_status") == 404
            and str(protection.get("message", "")).strip().lower()
            == "branch not protected"
        ):
            branch_protection = False
        else:
            record_error("branch_protection", protection)
    ok_rs, rulesets = _gh_get(
        f"{repo_path()}/rulesets", params={"per_page": 20}, expect_list=True
    )
    if not ok_rs:
        record_error("rulesets", rulesets)

    workflows_out = _security_workflows(record_error)
    dockerfiles, version_pins = _tree_scan(default_branch, record_error)
    bot_prs = _open_bot_prs(record_error)

    findings: list[dict[str, Any]] = []
    if (sec.get("secret_scanning") or {}).get("status") not in (None, "enabled"):
        findings.append(
            finding(
                "repo.secret_scanning",
                "medium",
                "Secret scanning が無効",
                CONFIG.github_repo,
                "secret_scanning=disabled",
                "Secret scanning + push protection を有効化。",
            )
        )
    if branch_protection is False and ok_rs and not rulesets:
        findings.append(
            finding(
                "repo.branch_protection",
                "high",
                f"{default_branch} にブランチ保護/ルールセットがない",
                CONFIG.github_repo,
                "保護なしで直接 push 可能。",
                "PR 必須 + 必須チェック + レビュー 1 名以上を設定。",
            )
        )
    findings.extend(
        finding(
            "repo.security_workflow_failing",
            "medium",
            f"セキュリティ系ワークフロー失敗: {wf['name']}",
            str(wf["path"] or ""),
            f"最新実行 {wf['latest_run_at']} → {wf['latest_conclusion']}",
            "失敗原因を確認し、スキャン結果が継続的に得られる状態に戻す。",
        )
        for wf in workflows_out
        if wf["latest_conclusion"] in ("failure", "timed_out", "cancelled")
    )
    summary = {
        "default_branch": default_branch,
        "visibility": repo.get("visibility"),
        "security_and_analysis": sec,
        "dependabot_alerts_enabled": alerts_enabled,
        "branch_protection": branch_protection,
        "rulesets": [r.get("name") for r in rulesets]
        if ok_rs and isinstance(rulesets, list)
        else None,
        "security_workflows": workflows_out,
        "dockerfiles": dockerfiles,
        "version_pins": version_pins,
        "open_dependabot_prs": bot_prs["dependabot"],
        "open_renovate_prs": bot_prs["renovate"],
        "pushed_at": repo.get("pushed_at"),
    }
    out = result("repo_posture", findings, summary)
    if errors:
        out.update(status="partial", errors=errors)
    return out
