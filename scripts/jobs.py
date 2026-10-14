"""List the team's jobs and their schedules (generated from app/specialists).

    uv run python scripts/jobs.py              # human-readable table
    uv run python scripts/jobs.py schedules    # TSV used by scripts/gcp/setup_schedules.sh

Cron expressions and scheduled parameters honour the environment overrides
declared on each job (e.g. ERROR_WATCH_CRON, ERROR_WATCH_HOURS).
"""

from __future__ import annotations

import sys

from app.specialists import all_jobs, scheduled_jobs


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "schedules":
        for job in scheduled_jobs():
            print(f"{job['key']}\t{job['cron']}\t{job['payload']}")
        return 0
    print(f"{'job':<40} {'specialist':<16} {'schedule':<14} payload")
    schedules = {job["key"]: job for job in scheduled_jobs()}
    for specialist, job in all_jobs():
        scheduled = schedules.get(job.schedule_key, {})
        print(
            f"{' / '.join(job.ids):<40} {specialist.name:<16} "
            f"{scheduled.get('cron', 'on demand'):<14} {scheduled.get('payload', '')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
