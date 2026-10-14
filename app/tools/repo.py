"""Repository security posture (GitHub): security features, branch protection,
security workflow health, Dockerfile base images and toolchain pins."""

from __future__ import annotations

import re
from typing import Any

from app.config import CONFIG
from app.core.findings import error_result, finding, result
from app.integrations.github import gh_rest, repo_path

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


def _get(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    accept: str | None = None,
    list_key: str | None = None,
    expect_list: bool = False,
) -> tuple[bool, Any]:
    """Reject malformed successful responses before treating them as observations."""
    ok, payload = gh_rest("GET", path, params=params, accept=accept)
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
    elif valid and accept:
        valid = isinstance(payload.get("raw"), str)
    if not valid:
        return False, {
            "status": "error",
            "message": f"Unexpected response shape for {path}",
        }
    return True, payload


def get_repo_security_posture() -> dict:
    """Summarise the GitHub repository's security hygiene: security features,
    branch protection, latest runs of security workflows, Dockerfile base
    images and toolchain version pins (for EOL checks), Dependabot PRs."""
    ok, repo = _get(repo_path())
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

    ok_va, alerts = _get(f"{repo_path()}/vulnerability-alerts")
    alerts_enabled = True if ok_va else None
    if not ok_va:
        # Repository admin rights do not prove the token's endpoint permissions.
        # GitHub may mask missing token permissions with the same 404 as disabled alerts.
        record_error("dependabot_alerts", alerts)
    ok_bp, protection = _get(f"{repo_path()}/branches/{default_branch}/protection")
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
    ok_rs, rulesets = _get(
        f"{repo_path()}/rulesets", params={"per_page": 20}, expect_list=True
    )
    if not ok_rs:
        record_error("rulesets", rulesets)

    workflows_out = []
    ok_wf, workflows = _get(
        f"{repo_path()}/actions/workflows",
        params={"per_page": 100},
        list_key="workflows",
    )
    if ok_wf:
        for wf in workflows.get("workflows") or []:
            if not _SECURITY_WORKFLOW.search(
                f"{wf.get('name', '')} {wf.get('path', '')}"
            ):
                continue
            if "id" not in wf:
                record_error(
                    "security_workflows",
                    {"status": "error", "message": "Workflow is missing id"},
                )
                continue
            ok_r, runs = _get(
                f"{repo_path()}/actions/workflows/{wf['id']}/runs",
                params={"per_page": 1},
                list_key="workflow_runs",
            )
            if not ok_r:
                record_error(f"workflow_runs:{wf['id']}", runs)
            latest = ((runs or {}).get("workflow_runs") or [{}])[0] if ok_r else {}
            workflows_out.append(
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
    else:
        record_error("security_workflows", workflows)

    dockerfiles = []
    version_pins: dict[str, str] = {}
    ok_tree, tree = _get(
        f"{repo_path()}/git/trees/{default_branch}",
        params={"recursive": "1"},
        list_key="tree",
    )
    if ok_tree:
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
        for path in docker_paths:
            ok_c, content = _get(
                f"{repo_path()}/contents/{path}",
                params={"ref": default_branch},
                accept="application/vnd.github.raw+json",
            )
            if not ok_c:
                record_error(f"content:{path}", content)
            text = content.get("raw", "") if isinstance(content, dict) else ""
            if ok_c and text:
                from_lines = [
                    line.strip()
                    for line in text.splitlines()
                    if line.strip().upper().startswith("FROM ")
                ]
                dockerfiles.append({"path": path, "from": from_lines})
        for vf in _VERSION_FILES:
            if vf in paths:
                ok_v, content = _get(
                    f"{repo_path()}/contents/{vf}",
                    params={"ref": default_branch},
                    accept="application/vnd.github.raw+json",
                )
                if not ok_v:
                    record_error(f"content:{vf}", content)
                text = content.get("raw", "") if isinstance(content, dict) else ""
                if ok_v and text:
                    if vf == "package.json":
                        m = re.search(r'"engines"\s*:\s*\{[^}]*\}', text, re.S)
                        version_pins[vf] = (
                            m.group(0)[:200] if m else "(no engines field)"
                        )
                    elif vf.endswith(".yml") or vf.endswith(".json"):
                        version_pins[vf] = "(present)"
                    else:
                        version_pins[vf] = text.strip()[:120]
    else:
        record_error("repository_tree", tree)
    ok_dp, dependabot = _get(
        "/search/issues",
        params={
            "q": f"repo:{CONFIG.github_repo} is:pr is:open author:app/dependabot",
            "per_page": 1,
        },
    )
    if not ok_dp:
        record_error("dependabot_prs", dependabot)
    ok_rn, renovate = _get(
        "/search/issues",
        params={
            "q": f"repo:{CONFIG.github_repo} is:pr is:open author:app/renovate",
            "per_page": 1,
        },
    )
    if not ok_rn:
        record_error("renovate_prs", renovate)

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
    for wf in workflows_out:
        if wf["latest_conclusion"] in ("failure", "timed_out", "cancelled"):
            findings.append(
                finding(
                    "repo.security_workflow_failing",
                    "medium",
                    f"セキュリティ系ワークフロー失敗: {wf['name']}",
                    wf["path"],
                    f"最新実行 {wf['latest_run_at']} → {wf['latest_conclusion']}",
                    "失敗原因を確認し、スキャン結果が継続的に得られる状態に戻す。",
                )
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
        "open_dependabot_prs": (dependabot or {}).get("total_count") if ok_dp else None,
        "open_renovate_prs": (renovate or {}).get("total_count") if ok_rn else None,
        "pushed_at": repo.get("pushed_at"),
    }
    out = result("repo_posture", findings, summary)
    if errors:
        out.update(status="partial", errors=errors)
    return out
