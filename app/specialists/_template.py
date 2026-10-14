"""Template for a new specialist — copy this file, fill it in, then register it.

1. ``cp app/specialists/_template.py app/specialists/cost_watch.py``
2. Edit the name, role, description, instruction, tools and jobs below.
3. Add ``cost_watch.SPECIALIST`` to ``SPECIALISTS`` in ``app/specialists/__init__.py``.
4. ``make test`` (conventions are checked), then ``make run JOB=cost_watch``.
5. Re-run ``make gcp-schedule`` to create its Cloud Scheduler job (if it has a schedule).

This module is not registered, so it has no effect until you do step 3.
"""

from __future__ import annotations

from app.specialists.base import Job, Specialist
from app.tools import (
    create_ops_issue,
    find_existing_ops_issue,
    get_run_context,
    list_services,
    publish_report,
    search_issues,
)

INSTRUCTION = """
あなたは __PROVIDER__ の__TARGET_LABEL__「__TARGET__」の <役割> を担当します。
調査はすべて読み取り専用で行い、結果を GitHub「__REPO__」に記録します。

## 手順
1. get_run_context で時刻・dry_run・使える機能を確認する。
2. <調べること>（例: list_services でサービス一覧を取得する）
3. <判定基準>
4. 対応が必要なものは find_existing_ops_issue / search_issues で重複を確認してから create_ops_issue で起票する。
5. 最後に publish_report でレポートを発行し、要約を返す。

時間とツール呼び出しの予算: ツール呼び出しは合計 20 回以内、全体で 5 分以内を目安にする。

## レポート本文（Markdown）
- 対象と期間
- 検出事項（重要度順、根拠付き）
- 起票した ISSUE
- 人間への依頼
"""

SPECIALIST = Specialist(
    name="my_specialist",  # snake_case; also the tool name the leader calls
    role="<一行で役割>",
    description="<リーダーが読む説明: 何をする担当か、request に何を書けばよいか>",
    instruction=INSTRUCTION,
    tools=[
        get_run_context,
        list_services,
        find_existing_ops_issue,
        search_issues,
        create_ops_issue,
        publish_report,
    ],
    jobs=(
        Job(
            name="my_job",  # {"data": {"job": "my_job"}}
            params={"days": "data.days があればそれ、無ければ 7"},
            schedule="0 9 * * 5",  # Fridays 09:00; "" = on demand only
            schedule_key="my-job",  # scheduler job: <prefix>-my-job; cron override: MY_JOB_CRON
            scheduled_params={"days": 7},
        ),
    ),
)
