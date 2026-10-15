"""Extension points: specialists, jobs, schedules, project context, setup and doctor (offline)."""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pytest

from app.specialists import SPECIALISTS, all_jobs, scheduled_jobs
from app.specialists.base import Job, Specialist

ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    module_name = f"ops_scripts_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[module_name] = module  # dataclasses look their module up in sys.modules
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- specialists


def test_specialists_are_well_formed() -> None:
    names = [s.name for s in SPECIALISTS]
    assert len(names) == len(set(names))
    for specialist in SPECIALISTS:
        assert specialist.name.isidentifier() and specialist.name.islower()
        assert (
            specialist.role
            and specialist.description
            and specialist.instruction.strip()
        )
        assert specialist.tools, f"{specialist.name} has no tools"


def test_job_ids_and_schedule_keys_are_unique() -> None:
    ids = [job_id for _, job in all_jobs() for job_id in job.ids]
    assert len(ids) == len(set(ids))
    keys = [job.schedule_key for _, job in all_jobs() if job.schedule_key]
    assert len(keys) == len(set(keys))


def test_template_specialist_is_valid_but_not_registered() -> None:
    from app.specialists import _template

    assert isinstance(_template.SPECIALIST, Specialist)
    assert _template.SPECIALIST.name not in {s.name for s in SPECIALISTS}


def test_default_schedules_match_the_documented_ones() -> None:
    schedules = {job["key"]: job for job in scheduled_jobs({})}
    assert schedules["error-watch"]["cron"] == "0 8 * * *"
    assert json.loads(schedules["error-watch"]["payload"]) == {
        "job": "error_watch",
        "hours": 24,
    }
    assert json.loads(schedules["weekly-pr-review"]["payload"]) == {
        "job": "weekly_pr_review",
        "days": 7,
    }
    assert json.loads(schedules["monthly-security-audit"]["payload"]) == {
        "job": "monthly_security_audit",
        "kev_days": 30,
    }
    assert schedules["access-watch"]["cron"] == "30 8 * * *"
    assert json.loads(schedules["access-watch"]["payload"]) == {
        "job": "access_watch",
        "hours": 24,
    }
    assert schedules["infrastructure-review"]["cron"] == "0 10 * * 1"
    assert json.loads(schedules["infrastructure-review"]["payload"]) == {
        "job": "infrastructure_review",
    }


def test_schedule_overrides_from_environment() -> None:
    env = {
        "ERROR_WATCH_CRON": "0 * * * *",
        "ERROR_WATCH_HOURS": "1",
        "WEEKLY_DAYS": "14",
        "ACCESS_WATCH_CRON": "0 * * * *",
        "ACCESS_WATCH_HOURS": "1",
        "INFRASTRUCTURE_REVIEW_CRON": "0 10 1 * *",
    }
    schedules = {job["key"]: job for job in scheduled_jobs(env)}
    assert schedules["error-watch"]["cron"] == "0 * * * *"
    assert json.loads(schedules["error-watch"]["payload"])["hours"] == 1
    assert json.loads(schedules["weekly-pr-review"]["payload"])["days"] == 14
    assert schedules["access-watch"]["cron"] == "0 * * * *"
    assert json.loads(schedules["access-watch"]["payload"])["hours"] == 1
    assert schedules["infrastructure-review"]["cron"] == "0 10 1 * *"


def test_new_job_gets_a_generic_cron_variable() -> None:
    job = Job(
        name="cost_report",
        schedule="0 9 * * 5",
        schedule_key="cost-report",
        scheduled_params={"days": 7},
    )
    assert job.cron({}) == "0 9 * * 5"
    assert job.cron({"COST_REPORT_CRON": "0 6 * * *"}) == "0 6 * * *"
    assert json.loads(job.payload({})) == {"job": "cost_report", "days": 7}


def test_registering_a_specialist_updates_the_leader(monkeypatch) -> None:
    from app import prompts
    from app.specialists import _template

    monkeypatch.setattr(prompts, "SPECIALISTS", [*SPECIALISTS, _template.SPECIALIST])
    text = prompts.root_instruction(None)
    assert "my_specialist" in text and "| my_job | my_specialist |" in text
    assert f"{len(SPECIALISTS) + 1} 人の専門家" in text


# --------------------------------------------------------------------------- project context


def test_project_context_is_appended_when_present(tmp_path, monkeypatch) -> None:
    from app import prompts

    context = tmp_path / "ctx.md"
    context.write_text("# サービス構成\n- api: 決済 API")
    monkeypatch.setattr(
        prompts,
        "CONFIG",
        dataclasses.replace(prompts.CONFIG, context_file=str(context)),
    )
    text = prompts.instruction_for(SPECIALISTS[0])(None)
    assert "プロジェクト固有の情報" in text and "決済 API" in text

    monkeypatch.setattr(
        prompts,
        "CONFIG",
        dataclasses.replace(prompts.CONFIG, context_file=str(tmp_path / "none.md")),
    )
    assert "プロジェクト固有の情報" not in prompts.instruction_for(SPECIALISTS[0])(None)


def test_context_example_ships_with_the_image() -> None:
    assert (ROOT / "config" / "ops-context.example.md").exists()
    assert "COPY ./config ./config" in (ROOT / "Dockerfile").read_text()


# --------------------------------------------------------------------------- setup / doctor scripts


def test_setup_render_env_keeps_comments_and_sets_values() -> None:
    setup = _load_script("setup")
    template = "# header\nOPS_TARGET=your-target    # project to monitor\n# OPS_CONTEXT_FILE=config/x.md\nKEEP=1\n"
    out = setup.render_env(
        template,
        {"OPS_TARGET": "prod-1", "OPS_CONTEXT_FILE": "config/ops.md", "NEW": "v"},
    )
    lines = out.splitlines()
    assert lines[0] == "# header"
    assert lines[1].startswith("OPS_TARGET=prod-1") and lines[1].endswith(
        "# project to monitor"
    )
    assert lines[2] == "OPS_CONTEXT_FILE=config/ops.md"
    assert "KEEP=1" in lines and "NEW=v" in lines


def test_setup_writes_env_non_interactively(tmp_path, monkeypatch) -> None:
    setup = _load_script("setup")
    monkeypatch.setattr(
        setup,
        "detect",
        lambda: {
            "gcloud_project": "p1",
            "gcloud_account": "",
            "adc": True,
            "gh_user": "",
        },
    )
    env = tmp_path / ".env"
    code = setup.main(
        [
            "--yes",
            "--target",
            "p1",
            "--repo",
            "me/app",
            "--language",
            "en",
            "--env-file",
            str(env),
        ]
    )
    assert code == 0
    from dotenv import dotenv_values

    values = dotenv_values(env)
    assert values["OPS_TARGET"] == "p1" and values["GITHUB_REPO"] == "me/app"
    assert values["OPS_DRY_RUN"] == "true" and values["OPS_REPORT_LANGUAGE"] == "en"
    with pytest.raises(SystemExit):  # existing file without --force
        setup.main(
            ["--yes", "--target", "p1", "--repo", "me/app", "--env-file", str(env)]
        )


@pytest.mark.parametrize(
    ("result", "level", "hint_part"),
    [
        ({"status": "ok"}, "ok", ""),
        ({"status": "not_supported"}, "info", ""),
        ({"status": "partial"}, "fail", "check the source errors"),
        (
            {
                "status": "api_disabled",
                "error": {"api": "securitycenter.googleapis.com", "message": "x"},
            },
            "fail",
            "gcloud services enable securitycenter.googleapis.com --project p",
        ),
        (
            {
                "status": "permission_denied",
                "error": {"message": "Permission 'run.services.list' denied"},
            },
            "fail",
            "make gcp-grant",
        ),
    ],
)
def test_doctor_classifies_results_with_fixes(result, level, hint_part) -> None:
    doctor = _load_script("doctor")
    check = doctor.classify("audit_iam", result, "p")
    assert check.level == level
    assert hint_part in check.hint


def test_doctor_treats_optional_security_services_as_warnings() -> None:
    doctor = _load_script("doctor")
    check = doctor.classify(
        "list_security_findings",
        {"status": "api_disabled", "error": {"message": "x"}},
        "p",
    )
    assert check.level == "warn"


def test_scripts_have_docstrings() -> None:
    for name in ("setup", "doctor", "jobs", "envrun", "run_job"):
        module = _load_script(name)
        assert inspect.getdoc(module), name


@pytest.mark.parametrize("partial", [False, True])
def test_doctor_checks_new_capabilities_and_reports_partial_evidence(
    monkeypatch, partial
):
    import app.providers as providers
    from app.providers.base import CloudProvider

    doctor = _load_script("doctor")
    calls = []

    class Fake(CloudProvider):
        profile = providers.load_provider_class("gcp").profile

        def investigate_access(self, hours: int) -> dict:
            calls.append(("access", hours))
            return {"status": "partial" if partial else "ok"}

        def list_optimization_recommendations(self) -> dict:
            calls.append(("optimization",))
            return {"status": "ok"}

    monkeypatch.setattr(providers, "get_provider", lambda: Fake("test", "test-region"))
    monkeypatch.setattr(doctor, "gcp_identity", lambda: "offline test identity")
    checks = {check.title: check for check in doctor.check_cloud(quick=False)}
    assert ("access", 1) in calls
    assert ("optimization",) in calls
    assert checks["investigate_access"].level == ("fail" if partial else "ok")
    assert checks["list_optimization_recommendations"].level == "ok"
    calls.clear()
    doctor.check_cloud(quick=True)
    assert calls == []
