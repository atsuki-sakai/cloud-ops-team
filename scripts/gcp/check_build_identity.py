"""Validate only the selected builder's identity metadata; never log raw input."""

from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (ValueError, UnicodeError):
        raise SystemExit("Build profile: invalid identity metadata") from None
    if (
        len(sys.argv) != 2
        or not isinstance(data, dict)
        or data.get("email") != sys.argv[1]
        or ("disabled" in data and data["disabled"] is not False)
    ):
        raise SystemExit("Build profile: builder identity is disabled or mismatched")


if __name__ == "__main__":
    main()
