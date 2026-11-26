"""Pick user-visible strings by ``OPS_REPORT_LANGUAGE`` (English fallback)."""

from __future__ import annotations

from collections.abc import Mapping


def localized(
    table: Mapping[str, Mapping[str, str]], language: str, key: str, **fmt: object
) -> str:
    """Return ``table[language][key]`` (``en`` when the language is missing).

    ``fmt`` fills ``str.format`` placeholders; without it the text is returned
    as written.
    """
    text = table.get(language, table["en"])[key]
    return text.format(**fmt) if fmt else text
