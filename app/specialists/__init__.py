"""The team: every specialist the leader can delegate to.

To add a specialist, copy ``_template.py`` to a new module, fill it in and
append its ``SPECIALIST`` to ``SPECIALISTS`` below. The leader's routing
table, its tool list and the scheduler jobs (``scripts/gcp/setup_schedules.sh``)
follow automatically. See ``docs/customization.md``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from app.specialists import (
    access_watch,
    error_watch,
    infrastructure_review,
    pr_review,
    security_audit,
)
from app.specialists.base import Job, Specialist

SPECIALISTS: list[Specialist] = [
    error_watch.SPECIALIST,
    security_audit.SPECIALIST,
    pr_review.SPECIALIST,
    access_watch.SPECIALIST,
    infrastructure_review.SPECIALIST,
]

__all__ = ["SPECIALISTS", "Job", "Specialist", "all_jobs", "scheduled_jobs"]


def all_jobs() -> list[tuple[Specialist, Job]]:
    return [(specialist, job) for specialist in SPECIALISTS for job in specialist.jobs]


def scheduled_jobs(env: Mapping[str, str] = os.environ) -> list[dict[str, str]]:
    """Jobs that have a schedule, with environment overrides applied."""
    out = []
    for specialist, job in all_jobs():
        cron = job.cron(env)
        if job.schedule_key and cron:
            out.append(
                {
                    "key": job.schedule_key,
                    "cron": cron,
                    "payload": job.payload(env),
                    "specialist": specialist.name,
                }
            )
    return out
