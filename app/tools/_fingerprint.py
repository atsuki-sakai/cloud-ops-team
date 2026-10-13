"""Stable fingerprints for error groups and the metadata block kept in issues.

The fingerprint is what lets the team recognise "the same error again"
across runs without a database: it is embedded in the issue body as an
HTML comment and searched for before a new issue is opened.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
_TS = re.compile(
    r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?", re.I
)
_HEX = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_WS = re.compile(r"\s+")
_META = re.compile(r"<!--\s*ops-meta:\s*(\{.*?\})\s*-->", re.S)


def message_template(message: str, max_len: int = 240) -> str:
    """Normalise a log line so that variable parts collapse to placeholders."""
    text = (message or "").strip().lower()
    text = _UUID.sub("<uuid>", text)
    text = _TS.sub("<ts>", text)
    text = _HEX.sub("<hex>", text)
    text = _NUM.sub("<n>", text)
    text = _WS.sub(" ", text)
    return text[:max_len]


def fingerprint(resource_type: str, service: str, template: str) -> str:
    """16-hex-char digest identifying an error group."""
    raw = f"{resource_type}|{service}|{template}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def search_text(message: str, max_len: int = 60) -> str:
    """A literal substring of ``message`` suitable for a later log search.

    Picks the longest run of alphabetic words (so it survives changing ids
    and numbers) and caps the length. Falls back to the first characters.
    """
    text = (message or "").split("\n", 1)[0]
    text = text.split("{", 1)[0]
    runs = re.findall(r"[A-Za-z][A-Za-z' :\-]{8,}", text)
    best = max(runs, key=len).strip(" :-'") if runs else text.strip()
    return best[:max_len].strip()


def build_meta_comment(meta: dict[str, Any]) -> str:
    """Render the hidden metadata block appended to every issue body."""
    return f"<!-- ops-meta: {json.dumps(meta, ensure_ascii=False, sort_keys=True)} -->"


def parse_meta_comment(body: str) -> dict[str, Any]:
    """Extract the metadata block from an issue body (empty dict if absent)."""
    if not body:
        return {}
    match = _META.search(body)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
