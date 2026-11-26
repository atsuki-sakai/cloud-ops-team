"""Structured audit records written as one JSON line per event."""

from __future__ import annotations

import json
import logging
from typing import Any


def log_event(
    logger: logging.Logger, event: str, /, *, sort_keys: bool = False, **fields: Any
) -> None:
    """Log ``{"event": event, **fields}`` as JSON at INFO level."""
    logger.info(json.dumps({"event": event, **fields}, sort_keys=sort_keys))
