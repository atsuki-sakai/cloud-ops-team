"""Tool registry for the GCP ops team. Import tool lists from here."""

from app.tools.common_tools import get_run_context
from app.tools.github_tools import (
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
from app.tools.observability_tools import (
    count_matching_errors,
    get_alerting_setup,
    get_cloud_run_revisions,
    get_cloud_run_services,
    get_log_context,
    get_request_error_rates,
    get_scheduler_jobs,
    list_error_logs,
    summarize_error_groups,
)
from app.tools.security_tools import (
    audit_cloud_run_security,
    audit_cloud_sql,
    audit_firewall_rules,
    audit_iam_policy,
    audit_security_services,
    audit_service_account_keys,
    check_eol_status,
    fetch_cisa_kev,
    get_cloud_sql_instances,
    get_repo_security_posture,
    list_scc_findings,
    scan_deployed_images,
    web_threat_research,
)

ERROR_WATCH_TOOLS = [
    get_run_context,
    summarize_error_groups,
    list_error_logs,
    get_log_context,
    count_matching_errors,
    get_cloud_run_services,
    get_cloud_run_revisions,
    get_scheduler_jobs,
    get_cloud_sql_instances,
    get_request_error_rates,
    get_alerting_setup,
    find_existing_ops_issue,
    search_issues,
    create_ops_issue,
    comment_on_issue,
]

SECURITY_AUDIT_TOOLS = [
    get_run_context,
    audit_iam_policy,
    audit_service_account_keys,
    audit_cloud_run_security,
    audit_cloud_sql,
    audit_firewall_rules,
    audit_security_services,
    list_scc_findings,
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
