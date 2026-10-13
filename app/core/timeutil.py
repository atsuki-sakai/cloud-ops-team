"""Small UTC time helpers shared by providers and tools."""

from __future__ import annotations

import datetime as dt
import re

_FRACTION = re.compile(r"(\.\d{6})\d+")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso_utc(value: dt.datetime) -> str:
    """Format as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> dt.datetime | None:
    """Parse RFC 3339 timestamps (nanosecond fractions allowed); ``None`` if invalid."""
    if not value:
        return None
    try:
        cleaned = _FRACTION.sub(r"\1", value.strip()).replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(cleaned)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    except ValueError:
        return None
