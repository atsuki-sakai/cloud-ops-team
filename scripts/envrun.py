"""Run a command with the variables from ``.env`` (explicit environment wins).

    uv run python scripts/envrun.py ./scripts/gcp/deploy_cloud_run.sh

Used by the Makefile so deployment scripts read the same configuration as the
agent. python-dotenv understands quoting and trailing ``# comments``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: envrun.py <command> [args...]")
    env_file = Path(os.environ.get("OPS_ENV_FILE", ROOT / ".env"))
    values = dotenv_values(env_file) if env_file.exists() else {}
    env = {key: value for key, value in values.items() if value is not None}
    env.update(os.environ)
    os.execvpe(sys.argv[1], sys.argv[1:], env)


if __name__ == "__main__":
    main()
