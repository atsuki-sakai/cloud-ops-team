"""Pure-logic unit tests (no network, no LLM) for the ops team helpers."""

from __future__ import annotations

from app.config import load_config
from app.tools._fingerprint import (
    build_meta_comment,
    fingerprint,
    message_template,
    parse_meta_comment,
    search_text,
)
from app.tools._redact import redact, redact_obj
from app.tools.github_tools import KIND_LABELS, SEVERITY_LABELS, _title


def test_message_template_collapses_variable_parts() -> None:
    a = message_template(
        "db=app,user=app,host=[local] FATAL: connection to client lost pid 3437463 at 2026-10-08T11:30:05.682Z"
    )
    b = message_template(
        "db=app,user=app,host=[local] FATAL: connection to client lost pid 99 at 2026-10-09T01:00:00Z"
    )
    assert a == b
    assert "<n>" in a and "<ts>" in a
    assert "3437463" not in a


def test_fingerprint_is_stable_and_discriminates() -> None:
    t = message_template("HTTP 500 GET /internal/jobs/run id=abc123def456")
    f1 = fingerprint("cloud_run_revision", "demo-worker", t)
    f2 = fingerprint("cloud_run_revision", "demo-worker", t)
    f3 = fingerprint("cloud_run_revision", "demo-api", t)
    assert f1 == f2
    assert f1 != f3
    assert len(f1) == 16


def test_search_text_prefers_alphabetic_run() -> None:
    text = search_text(
        "job=worker-reconcile status=UNAVAILABLE Request failed 503 {json}"
    )
    assert "Request failed" in text
    assert "{" not in text
    assert len(text) <= 60


def test_meta_comment_roundtrip() -> None:
    meta = {
        "fingerprint": "deadbeefdeadbeef",
        "kind": "error",
        "service": "demo-worker",
        "search_text": "x y",
    }
    body = "## 概要\nなにか\n" + build_meta_comment(meta)
    assert parse_meta_comment(body) == meta
    assert parse_meta_comment("no meta here") == {}
    assert parse_meta_comment("") == {}


def test_redact_masks_secrets_and_pii() -> None:
    raw = (
        "user john.doe@example.com Authorization: Bearer abc.def-ghi sk_live_ABCDEFGH12345678 "
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 from 203.0.113.42 password=hunter2secret "
        "postgres://app:S3cret@10.1.2.3:5432/db card 4111 1111 1111 1111"
    )
    out = redact(raw)
    assert "john.doe@" not in out and "***@example.com" in out
    assert "Bearer [REDACTED]" in out
    assert "sk_live_" not in out
    assert "ghp_" not in out
    assert "203.0.113.42" not in out and "203.0.113.x" in out
    assert "hunter2secret" not in out
    assert "S3cret" not in out
    assert "4111 1111 1111 1111" not in out


def test_redact_leaves_ordinary_text_alone() -> None:
    text = "demo-worker: HTTP 500 on /internal/reconcile after 3 retries (revision demo-worker-00042-abc)"
    assert redact(text) == text


def test_redact_obj_recurses() -> None:
    data = {"a": ["mail me@x.io"], "b": {"c": "Bearer zzz"}}
    out = redact_obj(data)
    assert out["a"][0] == "mail ***@x.io"
    assert out["b"]["c"] == "Bearer [REDACTED]"


def test_issue_labels_and_title_prefix() -> None:
    assert "ops:error" in KIND_LABELS["error"] and "bug" in KIND_LABELS["error"]
    assert "security" in KIND_LABELS["security"]
    assert SEVERITY_LABELS["critical"] == ["priority: critical"]
    assert SEVERITY_LABELS["info"] == []
    assert _title("error", "x が落ちている") == "[ops/error] x が落ちている"
    assert _title("error", "[ops/error] already") == "[ops/error] already"


def test_config_env_parsing(monkeypatch) -> None:
    monkeypatch.setenv("GCP_TARGET_PROJECT", "proj-x")
    monkeypatch.setenv("GITHUB_REPO", "octo/repo")
    monkeypatch.setenv("OPS_DRY_RUN", "false")
    monkeypatch.setenv("OPS_AUTO_CLOSE_ISSUES", "yes")
    monkeypatch.setenv("OPS_MAX_ISSUES_PER_RUN", "3")
    monkeypatch.setenv("OPS_STACK_KEYWORDS", "A, B ,C")
    cfg = load_config()
    assert cfg.target_project == "proj-x"
    assert cfg.repo_owner == "octo" and cfg.repo_name == "repo"
    assert cfg.dry_run is False
    assert cfg.auto_close_issues is True
    assert cfg.max_issues_per_run == 3
    assert cfg.stack_keywords == ("A", "B", "C")
    assert cfg.tz.key == "Asia/Tokyo"


def test_config_defaults_are_safe(monkeypatch) -> None:
    for key in ("OPS_DRY_RUN", "OPS_AUTO_CLOSE_ISSUES", "OPS_MAX_ISSUES_PER_RUN"):
        monkeypatch.delenv(key, raising=False)
    cfg = load_config()
    assert cfg.dry_run is True
    assert cfg.auto_close_issues is False
    assert cfg.max_issues_per_run == 10


def test_request_log_search_text_uses_stable_url_prefix() -> None:
    from app.tools.observability_tools import _search_text_for

    entry = {
        "httpRequest": {
            "requestUrl": "https://demo-worker-x.a.run.app/api/v1/worker/sns-plans/01a11afa-0620-76b3-9f3a-1c3521841a9d/steps/plan_strategy?attempt=1"
        }
    }
    assert _search_text_for(entry, "HTTP 503 POST ...") == "/api/v1/worker/sns-plans"
    assert (
        _search_text_for({}, "job=x status=UNAVAILABLE Request failed")
        == "UNAVAILABLE Request failed"
    )


def test_secret_env_heuristic_ignores_ttl_and_mode_names() -> None:
    from app.tools.security_tools import _looks_secret

    assert _looks_secret("STRIPE_SECRET_KEY")
    assert _looks_secret("DATABASE_PASSWORD")
    assert not _looks_secret("SESSION_TOKEN_TTL_DAYS")
    assert not _looks_secret("LLM_AUTH_MODE")
    assert not _looks_secret("EMAIL_VERIFICATION_TOKEN_TTL_HOURS")
    assert not _looks_secret("API_AUDIENCE")


def test_scheduler_log_message_extraction() -> None:
    from app.tools.observability_tools import _message_of, _service_of

    entry = {
        "resource": {
            "type": "cloud_scheduler_job",
            "labels": {"job_id": "reconcile-reservations"},
        },
        "jsonPayload": {
            "@type": "type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished",
            "jobName": "projects/p/locations/r/jobs/reconcile-reservations",
            "status": "INTERNAL",
            "debugInfo": "URL_UNREACHABLE-UNREACHABLE_5xx. Original HTTP response code number = 500",
        },
    }
    assert _service_of(entry) == "reconcile-reservations"
    assert _message_of(entry).startswith("job=reconcile-reservations status=INTERNAL")
