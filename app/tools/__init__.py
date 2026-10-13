"""Agent-facing tool registry. Import the per-specialist tool lists from here.

- ``cloud``: cloud-neutral wrappers over the selected provider's capabilities
- ``threat_intel``: CISA KEV, endoflife.date, Google Search-grounded research
- ``repo``: repository security posture (GitHub)
- ``issues``: issue / PR tools (GitHub), dry-run aware
- ``context``: run context (time, provider, flags)
"""

from app.tools.cloud import (
    audit_compute,
    audit_credentials,
    audit_databases,
    audit_iam,
    audit_network,
    audit_security_services,
    count_matching_errors,
    get_alerting_setup,
    get_log_context,
    get_request_error_rates,
    list_databases,
    list_deployments,
    list_error_logs,
    list_scheduled_jobs,
    list_security_findings,
    list_services,
    scan_deployed_images,
    summarize_error_groups,
)
from app.tools.context import get_run_context
from app.tools.issues import (
    add_issue_labels,
    close_issue,
    comment_on_issue,
    create_ops_issue,
    find_existing_ops_issue,
    get_issue,
    get_issue_linked_prs,
    get_pull_request,
    get_recent_workflow_runs,
    list_ops_issues,
    list_recent_pull_requests,
    publish_report,
    search_issues,
)
from app.tools.repo import get_repo_security_posture
from app.tools.threat_intel import check_eol_status, fetch_cisa_kev, web_threat_research

ERROR_WATCH_TOOLS = [
    get_run_context,
    summarize_error_groups,
    list_error_logs,
    get_log_context,
    count_matching_errors,
    list_services,
    list_deployments,
    list_scheduled_jobs,
    list_databases,
    get_request_error_rates,
    get_alerting_setup,
    find_existing_ops_issue,
    search_issues,
    create_ops_issue,
    comment_on_issue,
]

SECURITY_AUDIT_TOOLS = [
    get_run_context,
    audit_iam,
    audit_credentials,
    audit_compute,
    audit_databases,
    audit_network,
    audit_security_services,
    list_security_findings,
    scan_deployed_images,
    fetch_cisa_kev,
    check_eol_status,
    get_repo_security_posture,
    web_threat_research,
    get_alerting_setup,
    find_existing_ops_issue,
    search_issues,
    create_ops_issue,
    comment_on_issue,
    publish_report,
]

PR_REVIEW_TOOLS = [
    get_run_context,
    list_ops_issues,
    get_issue,
    get_issue_linked_prs,
    list_recent_pull_requests,
    get_pull_request,
    get_recent_workflow_runs,
    count_matching_errors,
    search_issues,
    comment_on_issue,
    add_issue_labels,
    close_issue,
    publish_report,
]

__all__ = [
    "ERROR_WATCH_TOOLS",
    "PR_REVIEW_TOOLS",
    "SECURITY_AUDIT_TOOLS",
    "get_run_context",
]
