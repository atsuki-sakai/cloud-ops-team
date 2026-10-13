"""GitHub issue / pull-request tools.

Writes are guarded by ``OPS_DRY_RUN`` (default on) and by the per-run issue
cap; closing an issue additionally requires ``OPS_AUTO_CLOSE_ISSUES``. Every
issue the team creates carries the ``ops-agent`` label and a hidden
``ops-meta`` block so that later runs can recognise and verify it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import quote

from google.adk.tools import ToolContext

from app.config import CONFIG
from app.core.fingerprint import build_meta_comment, parse_meta_comment
from app.core.redact import redact
from app.integrations.github import gh_graphql, gh_rest, repo_path

KIND_LABELS: dict[str, list[str]] = {
    "error": ["ops:error", "bug"],
    "security": ["ops:security", "security", "infrastructure"],
    "report": ["ops:report"],
}
SEVERITY_LABELS: dict[str, list[str]] = {
    "critical": ["priority: critical"],
    "high": ["priority: high"],
    "medium": ["major"],
    "low": ["minor"],
    "info": [],
}
LABEL_DEFS: dict[str, tuple[str, str]] = {
    "ops-agent": ("0052CC", "Created by the cloud-ops-team agents"),
    "ops:error": ("B60205", "Detected by the error watch"),
    "ops:security": ("5319E7", "Detected by the monthly security audit"),
    "ops:report": ("0E8A16", "Scheduled report from the ops team"),
    "ops:verified": ("0E8A16", "Fix verified by the ops team"),
    "ops:stale": ("FBCA04", "No progress for 14+ days"),
    "priority: critical": ("B60205", ""),
    "priority: high": ("D93F0B", ""),
    "major": ("EDEDED", ""),
    "minor": ("EDEDED", ""),
    "bug": ("D73A4A", "Something isn't working"),
    "security": ("EDEDED", ""),
    "infrastructure": ("0E8A16", "Infrastructure"),
}

# User-visible strings written to GitHub, selected by OPS_REPORT_LANGUAGE.
MESSAGES: dict[str, dict[str, str]] = {
    "ja": {
        "footer": "_この ISSUE は cloud-ops-team エージェントが自動起票しました（{when} / {provider}: {target}）。"
        "内容を確認のうえ対応方針を決めてください。対応 PR には `closes #<番号>` を含めると週次レビューで自動確認されます。_",
        "recurrence": "🔁 **再発を検知**（{when}）",
        "related_closed": "> 関連: 同種の ISSUE #{number}（{closed} クローズ）の再発の可能性があります。",
        "verified": "✅ **対応完了を確認**（{when}）",
        "auto_close_off": "_自動クローズは無効（OPS_AUTO_CLOSE_ISSUES=false）のため、確認のうえ人間がクローズしてください。_",
        "cooldown": "同じ fingerprint の open ISSUE が {hours}h 以内に更新済みのためコメントを省略",
        "cap": "OPS_MAX_ISSUES_PER_RUN ({cap}) に達したため起票をスキップ",
    },
    "en": {
        "footer": "_Filed automatically by the cloud-ops-team agents ({when} / {provider}: {target}). "
        "Please triage it. Include `closes #<number>` in the fixing PR so the weekly review can verify it._",
        "recurrence": "🔁 **Recurrence detected** ({when})",
        "related_closed": "> Related: this may be a recurrence of #{number} (closed {closed}).",
        "verified": "✅ **Fix verified** ({when})",
        "auto_close_off": "_Auto-close is disabled (OPS_AUTO_CLOSE_ISSUES=false); please close it after review._",
        "cooldown": "An open issue with the same fingerprint was updated within {hours}h; comment skipped",
        "cap": "Skipped: OPS_MAX_ISSUES_PER_RUN ({cap}) reached",
    },
}


def _msg(key: str, **kwargs: Any) -> str:
    table = MESSAGES.get(CONFIG.report_language, MESSAGES["en"])
    return table[key].format(**kwargs)


_ensured_labels: set[str] = set()
_COUNTER_KEY = "ops:issues_created"


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_local() -> str:
    return dt.datetime.now(CONFIG.tz).strftime("%Y-%m-%d %H:%M %Z")


def _ensure_labels(labels: list[str]) -> list[str]:
    created = []
    for label in labels:
        if label in _ensured_labels:
            continue
        ok, _ = gh_rest("GET", f"{repo_path()}/labels/{quote(label, safe='')}")
        if not ok:
            color, description = LABEL_DEFS.get(label, ("EDEDED", ""))
            ok2, _ = gh_rest(
                "POST",
                f"{repo_path()}/labels",
                json={"name": label, "color": color, "description": description},
            )
            if ok2:
                created.append(label)
        _ensured_labels.add(label)
    return created


def _title(kind: str, title: str) -> str:
    title = title.strip()
    return title if title.startswith("[ops/") else f"[ops/{kind}] {title}"


def _footer(meta: dict[str, Any]) -> str:
    footer = _msg(
        "footer", when=_now_local(), provider=CONFIG.provider, target=CONFIG.target
    )
    return f"\n\n---\n{footer}\n{build_meta_comment(meta)}"


def _meta_target(meta: dict[str, Any]) -> str | None:
    """Target recorded in an issue (``project`` is the pre-multi-cloud key)."""
    return meta.get("target") or meta.get("project")


def _compact_issue(issue: dict[str, Any]) -> dict[str, Any]:
    meta = parse_meta_comment(issue.get("body") or "")
    return {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "labels": [lbl.get("name") for lbl in issue.get("labels") or []],
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "closed_at": issue.get("closed_at"),
        "url": issue.get("html_url"),
        "comments": issue.get("comments"),
        "assignees": [a.get("login") for a in issue.get("assignees") or []],
        "fingerprint": meta.get("fingerprint"),
        "kind": meta.get("kind"),
        "service": meta.get("service"),
        "search_text": meta.get("search_text"),
        "severity": meta.get("severity"),
        "target": _meta_target(meta),
    }


def _list_issues(
    state: str, labels: list[str], per_page: int = 100
) -> tuple[bool, list[dict[str, Any]] | dict[str, Any]]:
    ok, issues = gh_rest(
        "GET",
        f"{repo_path()}/issues",
        params={
            "state": state,
            "labels": ",".join(labels),
            "per_page": per_page,
            "sort": "updated",
            "direction": "desc",
        },
    )
    if not ok:
        return False, issues
    return True, [i for i in issues if "pull_request" not in i]


# ---------------------------------------------------------------------------
# Issue de-duplication / creation
# ---------------------------------------------------------------------------


def find_existing_ops_issue(fingerprint: str) -> dict:
    """Find an issue previously opened by the team for this fingerprint.

    Looks at open issues first, then issues closed in the last 30 days.

    Args:
        fingerprint: The 16-char fingerprint from ``summarize_error_groups``
            or a security ``check_id:resource`` fingerprint.
    """
    if not fingerprint:
        return {"status": "not_found"}
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    for state in ("open", "closed"):
        ok, issues = _list_issues(state, [CONFIG.issue_label])
        if not ok:
            return {"status": issues.get("status", "error"), "error": issues}
        assert isinstance(issues, list)
        for issue in issues:
            if state == "closed" and (issue.get("closed_at") or "") < cutoff:
                continue
            compact = _compact_issue(issue)
            if compact["fingerprint"] != fingerprint:
                continue
            # Several targets (e.g. staging / prod) may share one repository.
            if (
                compact["target"]
                and CONFIG.target
                and compact["target"] != CONFIG.target
            ):
                continue
            return {"status": "found", **compact}
    return {"status": "not_found"}


def create_ops_issue(
    kind: str,
    title: str,
    body: str,
    severity: str,
    fingerprint: str,
    service: str,
    search_text: str,
    tool_context: ToolContext,
) -> dict:
    """Create a GitHub issue for a finding (de-duplicated by fingerprint).

    If an open issue with the same fingerprint exists, a "recurrence" comment
    is added instead of a new issue. Respects ``OPS_DRY_RUN`` and the
    per-run cap ``OPS_MAX_ISSUES_PER_RUN``.

    Args:
        kind: ``error`` or ``security``.
        title: Short Japanese title without prefix (prefix is added).
        body: Markdown body: 概要 / 影響範囲 / 証跡 / 原因分析 / 推奨対応 / 確認方法.
        severity: ``critical`` | ``high`` | ``medium`` | ``low`` | ``info``.
        fingerprint: Stable id for de-duplication (from the detection tool).
        service: Affected service / resource name (for later verification).
        search_text: Literal log text usable with ``count_matching_errors``
            (empty string for security findings).
    """
    kind = kind.strip().lower()
    severity = severity.strip().lower()
    if kind not in ("error", "security"):
        return {"status": "error", "message": "kind must be 'error' or 'security'"}
    if severity not in SEVERITY_LABELS:
        severity = "medium"
    labels = [CONFIG.issue_label, *KIND_LABELS[kind], *SEVERITY_LABELS[severity]]
    meta = {
        "fingerprint": fingerprint,
        "kind": kind,
        "service": service,
        "search_text": search_text,
        "severity": severity,
        "provider": CONFIG.provider,
        "target": CONFIG.target,
        "created_at": _now_iso(),
    }
    clean_body = redact(body)
    full_title = _title(kind, title)

    existing = (
        find_existing_ops_issue(fingerprint) if fingerprint else {"status": "not_found"}
    )
    if existing.get("status") not in ("found", "not_found"):
        return existing
    if existing["status"] == "found" and existing["state"] == "open":
        cooldown = (
            dt.datetime.now(dt.UTC)
            - dt.timedelta(hours=CONFIG.recurrence_cooldown_hours)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if (existing.get("updated_at") or "") >= cooldown:
            return {
                "status": "duplicate_recent",
                "number": existing["number"],
                "url": existing["url"],
                "reason": _msg("cooldown", hours=CONFIG.recurrence_cooldown_hours),
            }
        comment = f"{_msg('recurrence', when=_now_local())}\n\n{clean_body[:1500]}"
        if CONFIG.dry_run:
            return {
                "status": "dry_run",
                "action": "comment_existing",
                "number": existing["number"],
                "url": existing["url"],
                "comment_preview": comment[:600],
            }
        ok, _ = gh_rest(
            "POST",
            f"{repo_path()}/issues/{existing['number']}/comments",
            json={"body": comment},
        )
        return {
            "status": "updated_existing" if ok else "error",
            "number": existing["number"],
            "url": existing["url"],
        }
    if existing["status"] == "found":
        related = _msg(
            "related_closed",
            number=existing["number"],
            closed=str(existing.get("closed_at"))[:10],
        )
        clean_body += f"\n\n{related}"

    created_count = int(tool_context.state.get(_COUNTER_KEY, 0) or 0)
    if created_count >= CONFIG.max_issues_per_run:
        return {
            "status": "skipped",
            "reason": _msg("cap", cap=CONFIG.max_issues_per_run),
            "title": full_title,
        }
    full_body = clean_body + _footer(meta)
    if CONFIG.dry_run:
        tool_context.state[_COUNTER_KEY] = created_count + 1
        return {
            "status": "dry_run",
            "action": "create",
            "title": full_title,
            "labels": labels,
            "severity": severity,
            "body_preview": full_body[:900],
        }
    _ensure_labels(labels)
    ok, issue = gh_rest(
        "POST",
        f"{repo_path()}/issues",
        json={"title": full_title, "body": full_body, "labels": labels},
    )
    if not ok:
        return {
            "status": issue.get("status", "error"),
            "error": issue,
            "title": full_title,
        }
    tool_context.state[_COUNTER_KEY] = created_count + 1
    return {
        "status": "created",
        "number": issue.get("number"),
        "url": issue.get("html_url"),
        "title": full_title,
        "labels": labels,
    }


def publish_report(kind: str, title: str, body: str, tool_context: ToolContext) -> dict:
    """Publish a run report as a GitHub issue labelled ``ops:report``.

    Use for the monthly security report and the weekly PR/issue review so
    that headless runs leave a reviewable trace. Respects ``OPS_DRY_RUN``.

    Args:
        kind: ``security`` | ``error`` | ``weekly`` (adds matching label).
        title: Title without prefix, e.g. ``Monthly security audit 2026-10``.
        body: Markdown report.
    """
    kind = kind.strip().lower()
    labels = [CONFIG.issue_label, "ops:report"]
    if kind in ("security", "error"):
        labels.append(f"ops:{kind}")
    meta = {
        "kind": "report",
        "report_kind": kind,
        "provider": CONFIG.provider,
        "target": CONFIG.target,
        "created_at": _now_iso(),
    }
    full_title = _title("report", title)
    full_body = redact(body) + _footer(meta)
    if CONFIG.dry_run:
        return {
            "status": "dry_run",
            "action": "create_report",
            "title": full_title,
            "labels": labels,
            "body_preview": full_body[:1200],
        }
    _ensure_labels(labels)
    ok, issue = gh_rest(
        "POST",
        f"{repo_path()}/issues",
        json={"title": full_title, "body": full_body, "labels": labels},
    )
    if not ok:
        return {"status": issue.get("status", "error"), "error": issue}
    return {
        "status": "created",
        "number": issue.get("number"),
        "url": issue.get("html_url"),
        "title": full_title,
    }


# ---------------------------------------------------------------------------
# Issue updates
# ---------------------------------------------------------------------------


def comment_on_issue(issue_number: int, body: str) -> dict:
    """Add a Markdown comment to an issue (dry-run aware).

    Args:
        issue_number: Issue number.
        body: Comment body.
    """
    clean = redact(body)
    if CONFIG.dry_run:
        return {
            "status": "dry_run",
            "action": "comment",
            "number": issue_number,
            "comment_preview": clean[:600],
        }
    ok, payload = gh_rest(
        "POST",
        f"{repo_path()}/issues/{int(issue_number)}/comments",
        json={"body": clean},
    )
    return {
        "status": "commented" if ok else payload.get("status", "error"),
        "number": issue_number,
        "url": payload.get("html_url") if ok else None,
        "error": None if ok else payload,
    }


def add_issue_labels(issue_number: int, labels: list[str]) -> dict:
    """Add labels to an issue, creating missing ``ops:*`` labels (dry-run aware).

    Args:
        issue_number: Issue number.
        labels: Label names, e.g. ``["ops:verified"]`` or ``["ops:stale"]``.
    """
    if CONFIG.dry_run:
        return {
            "status": "dry_run",
            "action": "label",
            "number": issue_number,
            "labels": labels,
        }
    _ensure_labels(labels)
    ok, payload = gh_rest(
        "POST",
        f"{repo_path()}/issues/{int(issue_number)}/labels",
        json={"labels": labels},
    )
    return {
        "status": "labeled" if ok else payload.get("status", "error"),
        "number": issue_number,
        "labels": labels,
        "error": None if ok else payload,
    }


def close_issue(issue_number: int, reason_comment: str) -> dict:
    """Mark an issue as verified-complete.

    Posts ``reason_comment`` and adds ``ops:verified``. The issue is actually
    closed only when ``OPS_AUTO_CLOSE_ISSUES=true``; otherwise it is left for
    a human to close (status ``verified_not_closed``).

    Args:
        issue_number: Issue number.
        reason_comment: What was verified (merged PR, no recurrence, etc.).
    """
    comment = f"{_msg('verified', when=_now_local())}\n\n{redact(reason_comment)}"
    if not CONFIG.auto_close_issues:
        comment += f"\n\n{_msg('auto_close_off')}"
    if CONFIG.dry_run:
        return {
            "status": "dry_run",
            "action": "close" if CONFIG.auto_close_issues else "verify",
            "number": issue_number,
            "comment_preview": comment[:600],
        }
    _ensure_labels(["ops:verified"])
    gh_rest(
        "POST",
        f"{repo_path()}/issues/{int(issue_number)}/labels",
        json={"labels": ["ops:verified"]},
    )
    ok_c, _ = gh_rest(
        "POST",
        f"{repo_path()}/issues/{int(issue_number)}/comments",
        json={"body": comment},
    )
    if not CONFIG.auto_close_issues:
        return {
            "status": "verified_not_closed" if ok_c else "error",
            "number": issue_number,
        }
    ok, payload = gh_rest(
        "PATCH",
        f"{repo_path()}/issues/{int(issue_number)}",
        json={"state": "closed", "state_reason": "completed"},
    )
    return {
        "status": "closed" if ok else payload.get("status", "error"),
        "number": issue_number,
        "error": None if ok else payload,
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_ops_issues(state: str, kind: str) -> dict:
    """List issues created by the team.

    Args:
        state: ``open`` | ``closed`` | ``all``.
        kind: ``error`` | ``security`` | ``report`` | ``all``.
    """
    labels = [CONFIG.issue_label]
    if kind in ("error", "security", "report"):
        labels.append(f"ops:{kind}")
    ok, issues = _list_issues(
        state if state in ("open", "closed", "all") else "open", labels
    )
    if not ok:
        return {"status": issues.get("status", "error"), "error": issues}
    assert isinstance(issues, list)
    rows = [_compact_issue(i) for i in issues]
    return {"status": "ok", "count": len(rows), "issues": rows}


def get_issue(issue_number: int) -> dict:
    """Get one issue with its (truncated) body and metadata.

    Args:
        issue_number: Issue number.
    """
    ok, issue = gh_rest("GET", f"{repo_path()}/issues/{int(issue_number)}")
    if not ok:
        return {"status": issue.get("status", "error"), "error": issue}
    compact = _compact_issue(issue)
    compact["body"] = (issue.get("body") or "")[:3000]
    return {"status": "ok", **compact}


def search_issues(query: str) -> dict:
    """Search issues and PRs in the repository (GitHub search syntax).

    Args:
        query: e.g. ``"connection to client lost" in:title,body`` — the
            ``repo:`` qualifier is added automatically.
    """
    ok, payload = gh_rest(
        "GET",
        "/search/issues",
        params={"q": f"repo:{CONFIG.github_repo} {query}", "per_page": 20},
    )
    if not ok:
        return {"status": payload.get("status", "error"), "error": payload}
    items = [
        {
            "number": i.get("number"),
            "title": i.get("title"),
            "state": i.get("state"),
            "is_pr": "pull_request" in i,
            "url": i.get("html_url"),
            "updated_at": i.get("updated_at"),
        }
        for i in payload.get("items") or []
    ]
    return {"status": "ok", "total_count": payload.get("total_count"), "items": items}


_PR_FIELDS = """
  number title state merged mergedAt createdAt updatedAt url isDraft baseRefName headRefName
  author { login }
  reviewDecision
  labels(first: 10) { nodes { name } }
  closingIssuesReferences(first: 10) { nodes { number title state } }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
"""


def _compact_pr(pr: dict[str, Any]) -> dict[str, Any]:
    commits = ((pr.get("commits") or {}).get("nodes") or [{}])[0]
    rollup = ((commits.get("commit") or {}).get("statusCheckRollup") or {}).get("state")
    return {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "merged": pr.get("merged"),
        "merged_at": pr.get("mergedAt"),
        "created_at": pr.get("createdAt"),
        "updated_at": pr.get("updatedAt"),
        "url": pr.get("url"),
        "draft": pr.get("isDraft"),
        "author": (pr.get("author") or {}).get("login"),
        "base": pr.get("baseRefName"),
        "head": pr.get("headRefName"),
        "review_decision": pr.get("reviewDecision"),
        "checks": rollup,
        "labels": [n.get("name") for n in (pr.get("labels") or {}).get("nodes") or []],
        "closes_issues": [
            {
                "number": n.get("number"),
                "title": n.get("title"),
                "state": n.get("state"),
            }
            for n in (pr.get("closingIssuesReferences") or {}).get("nodes") or []
        ],
    }


def list_recent_pull_requests(days: int) -> dict:
    """List PRs updated in the last ``days`` days with linked issues, review
    decision and CI status.

    Args:
        days: Look-back window in days (1-90).
    """
    days = max(1, min(int(days), 90))
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    query = (
        "query($owner:String!,$name:String!){ repository(owner:$owner,name:$name){"
        " pullRequests(first:50, orderBy:{field:UPDATED_AT,direction:DESC}, states:[OPEN,MERGED,CLOSED]){"
        f" nodes {{ {_PR_FIELDS} }} }} }} }}"
    )
    ok, data = gh_graphql(query, {"owner": CONFIG.repo_owner, "name": CONFIG.repo_name})
    if not ok:
        return {"status": data.get("status", "error"), "error": data}
    nodes = ((data.get("repository") or {}).get("pullRequests") or {}).get(
        "nodes"
    ) or []
    rows = [_compact_pr(pr) for pr in nodes if (pr.get("updatedAt") or "") >= cutoff]
    return {
        "status": "ok",
        "since": cutoff,
        "count": len(rows),
        "merged": [r for r in rows if r["merged"]],
        "open": [r for r in rows if r["state"] == "OPEN"],
        "closed_unmerged": [
            r for r in rows if r["state"] == "CLOSED" and not r["merged"]
        ],
    }


def get_pull_request(pr_number: int) -> dict:
    """Get PR details: linked issues, review decision, CI rollup, changed files.

    Args:
        pr_number: Pull request number.
    """
    query = (
        "query($owner:String!,$name:String!,$number:Int!){ repository(owner:$owner,name:$name){"
        f" pullRequest(number:$number){{ {_PR_FIELDS} body changedFiles additions deletions"
        " mergeCommit { oid } files(first:50){ nodes { path additions deletions } }"
        " reviews(last:10){ nodes { state author { login } submittedAt } } } } }"
    )
    ok, data = gh_graphql(
        query,
        {
            "owner": CONFIG.repo_owner,
            "name": CONFIG.repo_name,
            "number": int(pr_number),
        },
    )
    if not ok:
        return {"status": data.get("status", "error"), "error": data}
    pr = (data.get("repository") or {}).get("pullRequest")
    if not pr:
        return {"status": "not_found", "number": pr_number}
    compact = _compact_pr(pr)
    compact.update(
        {
            "body": redact(pr.get("body") or "")[:2000],
            "changed_files": pr.get("changedFiles"),
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
            "merge_commit": (pr.get("mergeCommit") or {}).get("oid"),
            "files": [
                n.get("path") for n in (pr.get("files") or {}).get("nodes") or []
            ],
            "reviews": [
                {
                    "state": r.get("state"),
                    "author": (r.get("author") or {}).get("login"),
                    "at": r.get("submittedAt"),
                }
                for r in (pr.get("reviews") or {}).get("nodes") or []
            ],
        }
    )
    return {"status": "ok", **compact}


def get_issue_linked_prs(issue_number: int) -> dict:
    """List PRs linked to an issue (closing keywords and cross-references),
    with merge state — the primary signal for "is this issue done?".

    Args:
        issue_number: Issue number.
    """
    query = (
        "query($owner:String!,$name:String!,$number:Int!){ repository(owner:$owner,name:$name){"
        " issue(number:$number){ number title state closedAt"
        "  closedByPullRequestsReferences(first:10, includeClosedPrs:true){ nodes { number title state merged mergedAt url mergeCommit { oid } } }"
        "  timelineItems(first:50, itemTypes:[CROSS_REFERENCED_EVENT]){ nodes { ... on CrossReferencedEvent {"
        "   source { ... on PullRequest { number title state merged mergedAt url mergeCommit { oid } } } } } } } } }"
    )
    ok, data = gh_graphql(
        query,
        {
            "owner": CONFIG.repo_owner,
            "name": CONFIG.repo_name,
            "number": int(issue_number),
        },
    )
    if not ok:
        return {"status": data.get("status", "error"), "error": data}
    issue = (data.get("repository") or {}).get("issue")
    if not issue:
        return {"status": "not_found", "number": issue_number}
    prs: dict[int, dict[str, Any]] = {}
    for node in (issue.get("closedByPullRequestsReferences") or {}).get("nodes") or []:
        prs[node["number"]] = {
            **node,
            "merge_commit": (node.get("mergeCommit") or {}).get("oid"),
            "link": "closes",
        }
    for node in (issue.get("timelineItems") or {}).get("nodes") or []:
        src = node.get("source") or {}
        if src.get("number") and src["number"] not in prs:
            prs[src["number"]] = {
                **src,
                "merge_commit": (src.get("mergeCommit") or {}).get("oid"),
                "link": "reference",
            }
    rows = [{k: v for k, v in pr.items() if k != "mergeCommit"} for pr in prs.values()]
    merged = [r for r in rows if r.get("merged")]
    return {
        "status": "ok",
        "issue": {
            "number": issue.get("number"),
            "title": issue.get("title"),
            "state": issue.get("state"),
            "closed_at": issue.get("closedAt"),
        },
        "linked_prs": rows,
        "merged_prs": merged,
        "open_prs": [r for r in rows if r.get("state") == "OPEN"],
        "latest_merged_at": max(
            (r.get("mergedAt") or "" for r in merged), default=None
        ),
    }


def get_recent_workflow_runs(branch: str, limit: int) -> dict:
    """List recent GitHub Actions runs on a branch (CI / deploy health).

    Args:
        branch: Branch name, e.g. ``main``.
        limit: Max runs (1-30).
    """
    limit = max(1, min(int(limit), 30))
    ok, payload = gh_rest(
        "GET",
        f"{repo_path()}/actions/runs",
        params={"branch": branch, "per_page": limit},
    )
    if not ok:
        return {"status": payload.get("status", "error"), "error": payload}
    runs = [
        {
            "name": r.get("name") or r.get("display_title"),
            "workflow": str(r.get("path") or "").rsplit("/", 1)[-1],
            "event": r.get("event"),
            "status": r.get("status"),
            "conclusion": r.get("conclusion"),
            "head_sha": str(r.get("head_sha", ""))[:10],
            "created_at": r.get("created_at"),
            "url": r.get("html_url"),
        }
        for r in payload.get("workflow_runs") or []
    ]
    return {"status": "ok", "count": len(runs), "runs": runs}
