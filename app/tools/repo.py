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


def get_repo_security_posture() -> dict:
    """Summarise the GitHub repository's security hygiene: security features,
    branch protection, latest runs of security workflows, Dockerfile base
    images and toolchain version pins (for EOL checks), Dependabot PRs."""
    ok, repo = gh_rest("GET", repo_path())
    if not ok:
        return error_result("repo_posture", repo)
    default_branch = repo.get("default_branch", "main")
    sec = repo.get("security_and_analysis") or {}
    ok_va, _ = gh_rest("GET", f"{repo_path()}/vulnerability-alerts")
    ok_bp, protection = gh_rest(
        "GET", f"{repo_path()}/branches/{default_branch}/protection"
    )
    ok_rs, rulesets = gh_rest("GET", f"{repo_path()}/rulesets", params={"per_page": 20})

    workflows_out = []
    ok_wf, workflows = gh_rest(
        "GET", f"{repo_path()}/actions/workflows", params={"per_page": 100}
    )
    if ok_wf:
        for wf in workflows.get("workflows") or []:
            if not _SECURITY_WORKFLOW.search(
                f"{wf.get('name', '')} {wf.get('path', '')}"
            ):
                continue
            ok_r, runs = gh_rest(
                "GET",
                f"{repo_path()}/actions/workflows/{wf['id']}/runs",
                params={"per_page": 1},
            )
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

    dockerfiles = []
    version_pins: dict[str, str] = {}
    ok_tree, tree = gh_rest(
        "GET", f"{repo_path()}/git/trees/{default_branch}", params={"recursive": "1"}
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
            ok_c, content = gh_rest(
                "GET",
                f"{repo_path()}/contents/{path}",
                params={"ref": default_branch},
                accept="application/vnd.github.raw+json",
            )
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
                ok_v, content = gh_rest(
                    "GET",
                    f"{repo_path()}/contents/{vf}",
                    params={"ref": default_branch},
                    accept="application/vnd.github.raw+json",
                )
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
    ok_dp, dependabot = gh_rest(
        "GET",
        "/search/issues",
        params={
            "q": f"repo:{CONFIG.github_repo} is:pr is:open author:app/dependabot",
            "per_page": 1,
        },
    )
    ok_rn, renovate = gh_rest(
        "GET",
        "/search/issues",
        params={
            "q": f"repo:{CONFIG.github_repo} is:pr is:open author:app/renovate",
            "per_page": 1,
        },
    )

    findings: list[dict[str, Any]] = []
    if not ok_va:
        findings.append(
            finding(
                "repo.dependabot_alerts",
                "medium",
                "Dependabot alerts が無効",
                CONFIG.github_repo,
                "vulnerability-alerts が有効化されていません。",
                "Settings > Code security で Dependabot alerts / security updates を有効化。",
            )
        )
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
    if not ok_bp and not (ok_rs and rulesets):
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
        "dependabot_alerts_enabled": ok_va,
        "branch_protection": bool(ok_bp and protection),
        "rulesets": [r.get("name") for r in rulesets]
        if ok_rs and isinstance(rulesets, list)
        else [],
        "security_workflows": workflows_out,
        "dockerfiles": dockerfiles,
        "version_pins": version_pins,
        "open_dependabot_prs": (dependabot or {}).get("total_count") if ok_dp else None,
        "open_renovate_prs": (renovate or {}).get("total_count") if ok_rn else None,
        "pushed_at": repo.get("pushed_at"),
    }
    return result("repo_posture", findings, summary)
