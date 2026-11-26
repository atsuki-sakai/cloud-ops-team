"""Claude Code quality gate for Python files (ruff, ruff format, ty, ast-grep).

Modes
-----
``post-edit``  PostToolUse hook for Edit/Write/MultiEdit. Checks only the edited
               ``.py`` file. Errors exit 2 so the agent must fix them; warnings
               are passed back as context without blocking.
``stop``       Stop hook. Re-checks every changed ``.py`` file (``git diff`` plus
               untracked) and blocks finishing at most ``STOP_RETRY_LIMIT`` times.
``check``      For humans and ``make check-py``: ``hook.py check FILE...``.

Rules are not defined here. ruff and ty read ``pyproject.toml`` and ast-grep
reads ``sgconfig.yml``, the same configuration CI uses. ``CLOUD_OPS_PYCHECK``
selects ``enforce`` (default), ``warn`` (never blocks) or ``off``.

Only the standard library is used so the hook starts fast and cannot break on
an application dependency. A failing tool never blocks the agent: it is shown
to the user as a ``[py-quality] SETUP`` message instead (fail open, visibly).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TAG = "[py-quality]"
TOOL_TIMEOUT_S = 10
MAX_REPORTED = 20
STOP_RETRY_LIMIT = 2
BLOCK = 2

# Project-specific guidance for ruff codes whose generic message would mislead
# (for example "do not catch Exception" at an approved tool boundary).
RUFF_HINTS: dict[str, str] = {
    "BLE001": "境界（ツール/プロバイダ/通信）なら `return tool_error(exc, ...)` とし、"
    "`# noqa: BLE001 -- 境界の理由` を付ける。境界でなければ具体的な例外だけを受ける。",
    "B904": "except の中で投げ直すときは `raise NewError(...) from exc` とする。",
    "TRY300": "成功時の return は try の外（else 節）へ移す。",
    "TRY400": "except の中のログは logger.exception(...) を使い、例外の文字列は埋め込まない。",
    "S101": "assert は `python -O` で消える。明示的な if と例外で検証する。",
    "S113": "httpx / requests には必ず timeout= を渡す。",
    "S603": "subprocess には固定の引数リストだけを渡す。外部入力を混ぜない。",
    "S607": "実行ファイルはフルパスで指定するか、理由付きで抑制する。",
    "S324": "セキュリティ用途の hash は sha256 以上を使う（fingerprint.py は対象外）。",
    "ANN001": "引数に型注釈を付ける。",
    "ANN201": "戻り値に型注釈を付ける（ADK ツールは `-> dict[str, Any]`）。",
    "ANN202": "private 関数にも戻り値の型注釈を付ける。",
    "ASYNC210": "async 関数の中で同期 HTTP を呼ばない。httpx.AsyncClient か asyncio.to_thread を使う。",
    "ASYNC240": "async 関数の中で同期のファイル操作をしない。asyncio.to_thread を使う。",
    "PERF401": "ループで append せず、内包表記か list.extend を使う。",
    "DTZ005": "datetime.now(tz=dt.UTC) のようにタイムゾーンを付ける。",
    "T201": "print ではなく logging を使う。",
    "PGH004": "ファイル全体や理由のない noqa は使わず、`# noqa: CODE -- 理由` にする。",
    "TID251": "禁止された import です。メッセージの代替を使う。",
}
TY_HINT = "型を直す（isinstance で絞る・戻り値の型を正確に書く）。stub の誤りに限り `# ty: ignore[rule] -- 理由`。"
FORMAT_HINT = (
    "`.venv/bin/ruff format <file>` を実行してから、ファイルを読み直して続ける。"
)

_TY_LINE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?P<col>\d+): (?P<sev>error|warning)\[(?P<rule>[\w-]+)\] (?P<msg>.*)$"
)


@dataclass(frozen=True)
class Finding:
    tool: str
    rule: str
    severity: str  # "error" blocks, "warning" informs
    path: str
    line: int
    col: int
    message: str
    fix: str

    def render(self) -> str:
        return (
            f"- {self.path}:{self.line}:{self.col} [{self.tool} {self.rule}] "
            f"{self.message}\n  直し方: " + self.fix.strip().replace("\n", "\n    ")
        )


class ToolFailure(Exception):
    """A checker could not run; reported to the user, never to block the agent."""


# --------------------------------------------------------------------------- tools


def _bin(name: str) -> Path:
    path = Path(sys.executable).parent / name
    if not path.exists():
        raise ToolFailure(
            f"{name} が見つかりません（{path}）。`uv sync --extra lint` を実行してください。"
        )
    return path


def _run(args: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolFailure(
            f"{Path(args[0]).name} が {TOOL_TIMEOUT_S} 秒以内に終わりませんでした。"
        ) from exc


def ruff_check(files: list[str], root: Path) -> list[Finding]:
    proc = _run(
        [str(_bin("ruff")), "check", "--no-fix", "--output-format", "json", *files],
        root,
    )
    if proc.returncode not in (0, 1):
        raise ToolFailure(f"ruff check が失敗しました: {proc.stderr.strip()[:300]}")
    findings = []
    for item in json.loads(proc.stdout or "[]"):
        code = item.get("code") or "syntax"
        hint = RUFF_HINTS.get(
            code, f"`.venv/bin/ruff rule {code}` で説明を確認して直す。"
        )
        if item.get("fix"):
            hint += (
                "（自動修正あり: `.venv/bin/ruff check --fix <file>` の後、読み直す）"
            )
        loc = item.get("location") or {}
        findings.append(
            Finding(
                "ruff",
                code,
                "error",
                _rel(item.get("filename", ""), root),
                int(loc.get("row", 0)),
                int(loc.get("column", 0)),
                item.get("message", ""),
                hint,
            )
        )
    return findings


def ruff_format(files: list[str], root: Path) -> list[Finding]:
    proc = _run([str(_bin("ruff")), "format", "--check", *files], root)
    if proc.returncode == 0:
        return []
    if proc.returncode != 1:
        raise ToolFailure(f"ruff format が失敗しました: {proc.stderr.strip()[:300]}")
    out = []
    for line in proc.stdout.splitlines():
        if line.startswith("Would reformat: "):
            path = line.removeprefix("Would reformat: ").strip()
            out.append(
                Finding(
                    "ruff",
                    "format",
                    "error",
                    _rel(path, root),
                    1,
                    1,
                    "フォーマットが崩れています。",
                    FORMAT_HINT,
                )
            )
    return out


def ast_grep(files: list[str], root: Path) -> list[Finding]:
    proc = _run(
        [
            str(_bin("ast-grep")),
            "scan",
            "--config",
            "sgconfig.yml",
            "--json=compact",
            *files,
        ],
        root,
    )
    try:
        matches = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ToolFailure(
            f"ast-grep の出力を読めません: {proc.stderr.strip()[:300]}"
        ) from exc
    return [
        Finding(
            "ast-grep",
            m["ruleId"],
            "error" if m.get("severity") == "error" else "warning",
            _rel(m["file"], root),
            m["range"]["start"]["line"] + 1,
            m["range"]["start"]["column"] + 1,
            m.get("message", ""),
            m.get("note") or "",
        )
        for m in matches
    ]


def ty_check(files: list[str], root: Path) -> list[Finding]:
    # --python pins ty to the hook's own virtualenv so imports resolve even when
    # VIRTUAL_ENV is unset (hooks run outside an activated shell).
    venv = str(Path(sys.executable).parent.parent)
    proc = _run(
        [
            str(_bin("ty")),
            "check",
            "--python",
            venv,
            "--output-format",
            "concise",
            *files,
        ],
        root,
    )
    if proc.returncode not in (0, 1):
        raise ToolFailure(
            f"ty が失敗しました: {(proc.stderr or proc.stdout).strip()[:300]}"
        )
    matches = (_TY_LINE.match(line.strip()) for line in proc.stdout.splitlines())
    return [
        Finding(
            "ty",
            m["rule"],
            m["sev"],
            _rel(m["path"], root),
            int(m["line"]),
            int(m["col"]),
            m["msg"],
            TY_HINT,
        )
        for m in matches
        if m
    ]


CHECKS = (ruff_check, ruff_format, ty_check, ast_grep)


def run_checks(files: list[str], root: Path) -> tuple[list[Finding], dict[str, float]]:
    """Run every checker in parallel; total time is the slowest one, not the sum."""
    timings: dict[str, float] = {}

    def timed(check: Any) -> list[Finding]:
        started = time.monotonic()
        try:
            return check(files, root)
        finally:
            timings[check.__name__] = round(time.monotonic() - started, 3)

    with ThreadPoolExecutor(max_workers=len(CHECKS)) as pool:
        results = list(pool.map(timed, CHECKS))
    findings = [f for result in results for f in result]
    findings.sort(key=lambda f: (f.severity != "error", f.path, f.line, f.col))
    return findings, timings


# --------------------------------------------------------------------------- helpers


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root))
    except ValueError:
        return path


def project_root(payload: dict[str, Any]) -> Path:
    raw = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or Path.cwd()
    return Path(raw).resolve()


def in_scope(path: Path, root: Path) -> bool:
    """Python files inside the project, outside the virtualenv and VCS dirs."""
    if path.suffix != ".py" or not path.is_file():
        return False
    try:
        rel = path.resolve().relative_to(root)
    except ValueError:
        return False
    return not {".venv", ".git", "node_modules"} & set(rel.parts)


def changed_files(root: Path) -> list[str]:
    tracked = _run(["git", "diff", "--name-only", "HEAD", "--", "*.py"], root)
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"], root
    )
    names = {n for n in (tracked.stdout + untracked.stdout).splitlines() if n}
    return sorted(n for n in names if in_scope(root / n, root))


def report(findings: list[Finding], header: str) -> str:
    errors = sum(f.severity == "error" for f in findings)
    warnings = len(findings) - errors
    lines = [f"{TAG} {header}: エラー {errors} 件 / 警告 {warnings} 件"]
    lines += [f.render() for f in findings[:MAX_REPORTED]]
    if len(findings) > MAX_REPORTED:
        lines.append(
            f"... ほか {len(findings) - MAX_REPORTED} 件。直したあと再実行されます。"
        )
    lines.append(
        "規約: .claude/skills/python-quality/SKILL.md。抑制は `# noqa: CODE -- 理由` / "
        "`# ast-grep-ignore: rule-id -- 理由` だけ。同じ指摘が2回直らなければ、理由付きで抑制するか人に確認する。"
    )
    return "\n".join(lines)


def log(root: Path, entry: dict[str, Any]) -> None:
    """Append one JSON line; logging must never affect the hook's verdict."""
    try:
        log_dir = root / ".claude" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "py-quality.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"ts": time.time(), **entry}, ensure_ascii=False) + "\n"
            )
    except OSError:
        pass


def emit_json(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False))


# --------------------------------------------------------------------------- modes


def post_edit(payload: dict[str, Any], mode: str) -> int:
    root = project_root(payload)
    tool_input = payload.get("tool_input") or {}
    raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
    if not raw_path:
        return 0
    path = Path(raw_path)
    path = path if path.is_absolute() else root / path
    if not in_scope(path, root):
        return 0
    rel = str(path.resolve().relative_to(root))
    started = time.monotonic()
    findings, timings = run_checks([rel], root)
    errors = [f for f in findings if f.severity == "error"]
    log(
        root,
        {
            "event": "post-edit",
            "session_id": payload.get("session_id"),
            "file": rel,
            "errors": len(errors),
            "warnings": len(findings) - len(errors),
            "rules": sorted({f.rule for f in findings}),
            "elapsed_s": round(time.monotonic() - started, 3),
            "timings": timings,
            "mode": mode,
        },
    )
    if not findings:
        return 0
    text = report(findings, rel)
    if errors and mode == "enforce":
        print(text, file=sys.stderr)
        return BLOCK
    emit_json(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": text,
            },
            **(
                {
                    "systemMessage": f"{TAG} warn モード: {rel} に {len(errors)} 件のエラー"
                }
                if errors
                else {}
            ),
        }
    )
    return 0


def _counter_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "unknown")
    return Path(tempfile.gettempdir()) / "cloud-ops-py-quality" / f"{safe}.json"


def stop(payload: dict[str, Any], mode: str) -> int:
    root = project_root(payload)
    files = changed_files(root)
    if not files:
        return 0
    findings, timings = run_checks(files, root)
    errors = [f for f in findings if f.severity == "error"]
    counter = _counter_file(str(payload.get("session_id", "")))
    blocks = 0
    if counter.exists():
        try:
            blocks = int(json.loads(counter.read_text()).get("blocks", 0))
        except (ValueError, OSError):
            blocks = 0
    log(
        root,
        {
            "event": "stop",
            "session_id": payload.get("session_id"),
            "files": files,
            "errors": len(errors),
            "rules": sorted({f.rule for f in errors}),
            "timings": timings,
            "blocks_so_far": blocks,
            "mode": mode,
        },
    )
    if not errors:
        counter.unlink(missing_ok=True)
        return 0
    text = report(errors, f"変更した {len(files)} ファイルの最終チェック")
    if mode == "enforce" and blocks < STOP_RETRY_LIMIT:
        counter.parent.mkdir(parents=True, exist_ok=True)
        counter.write_text(json.dumps({"blocks": blocks + 1}))
        print(text, file=sys.stderr)
        return BLOCK
    counter.unlink(missing_ok=True)
    emit_json(
        {
            "systemMessage": f"{TAG} 未解決のエラーが {len(errors)} 件あります"
            f"（{STOP_RETRY_LIMIT} 回差し戻しても直らなかったため終了を許可）。\n{text}"
        }
    )
    return 0


def check(paths: list[str]) -> int:
    root = Path.cwd().resolve()
    files = (
        [p for p in paths if in_scope(root / p, root)] if paths else changed_files(root)
    )
    if not files:
        print(f"{TAG} 対象の .py ファイルがありません。")
        return 0
    findings, timings = run_checks(files, root)
    if findings:
        print(report(findings, f"{len(files)} ファイル"))
    else:
        print(f"{TAG} {len(files)} ファイル: 問題なし {timings}")
    return 1 if any(f.severity == "error" for f in findings) else 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    mode = os.environ.get("CLOUD_OPS_PYCHECK", "enforce").strip().lower()
    if mode == "off":
        return 0
    if mode not in {"enforce", "warn"}:
        mode = "enforce"
    try:
        if command == "check":
            return check(argv[2:])
        payload = json.loads(sys.stdin.read() or "{}")
        if command == "post-edit":
            return post_edit(payload, mode)
        if command == "stop":
            return stop(payload, mode)
    except ToolFailure as exc:
        emit_json(
            {"systemMessage": f"{TAG} SETUP: {exc}（チェックをスキップしました）"}
        )
        return 0
    except json.JSONDecodeError:
        emit_json(
            {
                "systemMessage": f"{TAG} SETUP: フックの入力 JSON を読めません（チェックをスキップしました）"
            }
        )
        return 0
    except Exception as exc:  # a hook bug must surface to the user, not silently pass
        emit_json(
            {
                "systemMessage": f"{TAG} SETUP: フック自体が失敗しました: {type(exc).__name__}（チェックをスキップしました）"
            }
        )
        return 0
    print(f"usage: {argv[0]} post-edit|stop|check [FILE...]", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
