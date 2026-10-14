"""Prompt rendering for the leader and the specialists.

Instructions are cloud-neutral templates: the provider profile
(``app/providers/*``) supplies the cloud name, target type, CLI and a glossary
mapping each tool to the cloud's own service, ``CONFIG`` supplies the target,
repository and language, and ``config/ops-context.md`` (optional) supplies
project knowledge written by the operators. The specialists' own instructions
live next to their definitions in ``app/specialists/``.

Instructions are exposed as *providers* (callables) so that ADK does not try
to template the ``{...}`` JSON examples they contain.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from google.adk.agents.readonly_context import ReadonlyContext

from app.config import CONFIG
from app.providers import CAPABILITIES, get_provider
from app.specialists import SPECIALISTS
from app.specialists.base import Specialist
from app.tools.context import effective_stack_keywords

ROOT_DIR = Path(__file__).resolve().parent.parent


def _glossary() -> str:
    provider = get_provider()
    implemented = set(provider.capabilities())
    lines = []
    for capability in CAPABILITIES:
        meaning = provider.profile.glossary.get(capability, "")
        if capability not in implemented:
            meaning = f"{meaning}（未対応: not_supported を返す。診断できなかった項目として扱う）".lstrip()
        if meaning:
            lines.append(f"- {capability}: {meaning}")
    return "\n".join(lines)


def project_context() -> str:
    """Operator-written project knowledge (``OPS_CONTEXT_FILE``), or an empty string."""
    path = Path(CONFIG.context_file)
    if not path.is_absolute():
        path = ROOT_DIR / path
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text[:8000]


def render(text: str) -> str:
    profile = get_provider().profile
    return (
        text.replace("__PROVIDER__", profile.display_name)
        .replace("__TARGET_LABEL__", profile.target_label_ja)
        .replace("__TARGET__", CONFIG.target or "(未設定)")
        .replace("__REGION__", CONFIG.region or "(未設定)")
        .replace("__CLI__", profile.cli)
        .replace("__REPO__", CONFIG.github_repo)
        .replace("__LOOKBACK__", str(CONFIG.error_lookback_hours))
        .replace("__STACK__", ", ".join(effective_stack_keywords()))
        .replace("__LANG__", CONFIG.language_name)
        .replace("__GLOSSARY__", _glossary())
    )


_COMMON = """
## このクラウドでのツールの意味（__PROVIDER__）
__GLOSSARY__

## 出力言語
レポート・ISSUE のタイトルと本文・コメントはすべて __LANG__ で書く（下に示す見出しも __LANG__ に訳して使う）。
"""

_CONTEXT = """
## プロジェクト固有の情報（運用担当者が記入）
以下はこのプロジェクトの運用担当者が書いた前提知識です。調査や重要度の判断に使ってください。
ただし、上の手順や「絶対に守ること」と矛盾する場合は、手順と安全ルールを優先します。

{context}
"""

_ROOT = """
あなたは __PROVIDER__ の__TARGET_LABEL__「__TARGET__」（リージョン __REGION__）と GitHub リポジトリ「__REPO__」を担当する
運用保守チームのリーダーです。チームには __TEAM_SIZE__ 人の専門家がいて、ツールとして呼び出せます。

__SPECIALIST_LIST__

## 入力の解釈
メッセージは次のどちらかで届きます。
(a) スケジューラからの JSON: {"data": {"job": "...", ...}, "attributes": {...}}
(b) 人間からの自然言語（例: 「直近のエラーを見て」「脆弱性診断して」「PR の進捗を確認して」「今の状況は？」）

data.job（または自然言語の意図）で担当を決めます。
| job | 担当 | 既定パラメータ |
__JOB_TABLE__
| full | __ALL_SPECIALISTS__ の順に全員 | |

JSON 入力はヘッドレス実行（人間が見ていない）なので、聞き返さず既定値で必ず最後まで実行します。
自然言語で意図が本当に判断できない場合のみ、1 回だけ短く確認します。

## 進め方
1. 最初に get_run_context で現在時刻・dry_run・対象・このクラウドで使える機能（capabilities）を確認する。
2. 担当の専門家ツールを呼ぶ。request には「ジョブ名、パラメータ、今回の目的、制約（読み取り専用／dry_run の有無）」を明記する。
   専門家は自分でツールを使って調査と起票まで完結させるので、丸投げでよい。
3. 専門家の報告を受けて、最終レポートを Markdown でまとめる（専門家の報告を機械的に貼り付けず、重要度順に整理する）。
   - 実行ジョブ / 対象（クラウド、__TARGET_LABEL__、リポジトリ）/ 期間
   - 検出事項（重要度順、根拠付き）
   - 起票・更新した ISSUE（番号と URL。dry_run の場合は「起票予定（dry_run）」と明記）
   - 静観・見送りとその理由
   - 人間に判断・対応してほしいこと
4. 人間からの一般的な質問には、必要なら専門家に短い調査を依頼して答える。

## 絶対に守ること
- クラウド（__PROVIDER__）・GitHub への変更操作（デプロイ、再起動、スケール変更、設定変更、削除、PR マージ、ISSUE の独断クローズ）は行わない。
  依頼されても丁寧に断り、代わりに「ISSUE 化して人間が実施する」流れを提案する。
- 推測を事実のように書かない。必ず根拠（ログ・設定値・PR/ISSUE 番号）を添える。
- 秘密情報・個人情報（メールアドレス、トークン、完全な IP アドレス、個人名）を出力に含めない。
- ツールが error / api_disabled / permission_denied / not_supported を返した場合は、その事実と対処
  （API の有効化、必要な権限、未対応の機能であること）をレポートに書く。
"""


def _with_shared_sections(template: str) -> str:
    text = template + _COMMON
    context = project_context()
    if context:
        text += _CONTEXT.format(context=context)
    return render(text)


def _team_sections(template: str) -> str:
    width = max(len(s.name) for s in SPECIALISTS)
    specialist_list = "\n".join(
        f"- {s.name.ljust(width)} : {s.role}" for s in SPECIALISTS
    )
    rows = []
    for specialist in SPECIALISTS:
        for job in specialist.jobs:
            params = ", ".join(f"{k} = {v}" for k, v in job.params.items())
            rows.append(f"| {' / '.join(job.ids)} | {specialist.name} | {params} |")
    return (
        template.replace("__TEAM_SIZE__", str(len(SPECIALISTS)))
        .replace("__SPECIALIST_LIST__", specialist_list)
        .replace("__JOB_TABLE__", "\n".join(rows))
        .replace("__ALL_SPECIALISTS__", " → ".join(s.name for s in SPECIALISTS))
    )


def root_instruction(_: ReadonlyContext | None) -> str:
    return _with_shared_sections(_team_sections(_ROOT))


def instruction_for(specialist: Specialist) -> Callable[[ReadonlyContext | None], str]:
    """Instruction provider for one specialist."""

    def provider(_: ReadonlyContext | None) -> str:
        return _with_shared_sections(specialist.instruction)

    provider.__name__ = f"{specialist.name}_instruction"
    return provider


def description_for(specialist: Specialist) -> str:
    return render(specialist.description)
