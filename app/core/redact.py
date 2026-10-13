"""Best-effort redaction of secrets and personal data before text leaves GCP.

Log excerpts end up in GitHub issues, so anything that looks like a
credential, token, e-mail address, card number or full IP is masked here.
The model is additionally instructed never to copy such data verbatim.
"""

from __future__ import annotations

import re
from typing import Any

_PRIVATE_IP = re.compile(
    r"^(0\.0\.0\.0$|10\.|127\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)"
)


def _mask_ip(match: re.Match[str]) -> str:
    ip = match.group(0)
    return ip if _PRIVATE_IP.match(ip) else f"{match.group(1)}.x"


def _mask_email(match: re.Match[str]) -> str:
    domain = match.group(1)
    if domain.endswith("gserviceaccount.com"):
        return match.group(0)
    return f"***@{domain}"


_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    # Explicit credential material
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
        "[REDACTED-JWT]",
    ),
    (
        re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{8,}\b"),
        "[REDACTED-STRIPE-KEY]",
    ),
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{30,}\b"), "[REDACTED-GOOGLE-KEY]"),
    (re.compile(r"\bya29\.[0-9A-Za-z\-_]{20,}\b"), "[REDACTED-OAUTH-TOKEN]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED-GITHUB-TOKEN]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED-SLACK-TOKEN]"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        "[REDACTED-PRIVATE-KEY]",
    ),
    # key=value style secrets
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)"
            r"(\s*[:=]\s*)[\"']?[^\s\"',;&]{4,}"
        ),
        r"\1\2[REDACTED]",
    ),
    # Connection strings with credentials
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:]+):[^@\s]+@"), r"\1:[REDACTED]@"),
    # Personal data
    (re.compile(r"\b[\w.+-]+@([\w-]+\.[\w.-]+)\b"), _mask_email),
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[REDACTED-NUMBER]"),
    (re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}\b"), _mask_ip),
]


def redact(text: str) -> str:
    """Mask secrets / PII in ``text``. Idempotent and safe on empty input."""
    if not text:
        return text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_obj(value: Any) -> Any:
    """Recursively redact every string inside dicts / lists."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value
