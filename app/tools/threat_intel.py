"""Cloud-neutral threat intelligence and version hygiene tools.

- ``fetch_cisa_kev``: CISA Known Exploited Vulnerabilities, filtered by the stack keywords
- ``check_eol_status``: end-of-life / latest-release status from endoflife.date
- ``web_threat_research``: one bounded Google Search-grounded research call
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import httpx

from app.config import CONFIG
from app.core.redact import redact
from app.core.timeutil import utcnow
from app.tools.context import effective_stack_keywords

KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
EOL_URL = "https://endoflife.date/api/{product}.json"

_kev_cache: tuple[float, dict[str, Any]] | None = None


def fetch_cisa_kev(days: int) -> dict:
    """Fetch CISA's Known Exploited Vulnerabilities catalog and return entries
    added in the last ``days`` days, highlighting those matching the stack
    keywords configured for this project.

    Args:
        days: Look-back window in days (1-365).
    """
    global _kev_cache
    days = max(1, min(int(days), 365))
    now = time.time()
    if _kev_cache and now - _kev_cache[0] < 3600:
        data = _kev_cache[1]
    else:
        try:
            resp = httpx.get(KEV_URL, timeout=60, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "error", "message": f"KEV fetch failed: {exc}"}
        _kev_cache = (now, data)
    cutoff = (utcnow() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    keywords = [k.lower() for k in effective_stack_keywords()]

    def compact(v: dict[str, Any]) -> dict[str, Any]:
        return {
            "cve": v.get("cveID"),
            "vendor": v.get("vendorProject"),
            "product": v.get("product"),
            "name": v.get("vulnerabilityName"),
            "date_added": v.get("dateAdded"),
            "due_date": v.get("dueDate"),
            "ransomware": v.get("knownRansomwareCampaignUse"),
            "description": str(v.get("shortDescription") or "")[:220],
        }

    recent = sorted(
        (
            v
            for v in data.get("vulnerabilities") or []
            if str(v.get("dateAdded", "")) >= cutoff
        ),
        key=lambda v: v.get("dateAdded", ""),
        reverse=True,
    )
    matches = [
        v
        for v in recent
        if any(
            k
            in f"{v.get('vendorProject', '')} {v.get('product', '')} {v.get('vulnerabilityName', '')}".lower()
            for k in keywords
        )
    ]
    return {
        "status": "ok",
        "catalog_version": data.get("catalogVersion"),
        "released": data.get("dateReleased"),
        "window_days": days,
        "recent_count": len(recent),
        "ransomware_count": sum(
            1
            for v in recent
            if str(v.get("knownRansomwareCampaignUse", "")).lower() == "known"
        ),
        "stack_keywords": list(effective_stack_keywords()),
        "stack_matches": [compact(v) for v in matches[:40]],
        "recent": [compact(v) for v in recent[:40]],
    }


def check_eol_status(product: str, version: str) -> dict:
    """Look up end-of-life / latest-release status on endoflife.date.

    Args:
        product: endoflife.date slug, e.g. ``postgresql``, ``nodejs``, ``rust``,
            ``debian``, ``alpine``, ``python``, ``nextjs``, ``react``, ``ubuntu``.
        version: Version in use (e.g. ``17``, ``22.11.0``, ``1.82``).
    """
    slug = product.strip().lower()
    try:
        resp = httpx.get(
            EOL_URL.format(product=slug), timeout=30, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        return {"status": "error", "message": f"endoflife.date fetch failed: {exc}"}
    if resp.status_code == 404:
        return {
            "status": "not_found",
            "message": f"unknown product slug '{slug}'",
            "hint": "https://endoflife.date/api/all.json で slug を確認",
        }
    if resp.status_code >= 400:
        return {"status": "error", "http_status": resp.status_code}
    cycles = resp.json()
    ver = version.strip().lstrip("v")
    parts = ver.split(".")
    candidates = [ver, ".".join(parts[:2]), parts[0]]
    match = next(
        (c for cand in candidates for c in cycles if str(c.get("cycle")) == cand), None
    )
    if match is None:
        return {
            "status": "not_found",
            "product": slug,
            "version": version,
            "known_cycles": [str(c.get("cycle")) for c in cycles[:10]],
        }
    today = utcnow().date()
    eol = match.get("eol")
    is_eol: bool | None
    days_until_eol = None
    if isinstance(eol, bool):
        is_eol = eol
    elif isinstance(eol, str):
        try:
            eol_date = dt.date.fromisoformat(eol)
            is_eol = eol_date <= today
            days_until_eol = (eol_date - today).days
        except ValueError:
            is_eol = None
    else:
        is_eol = None
    newer = [
        str(c.get("cycle")) for c in cycles if cycles.index(c) < cycles.index(match)
    ]
    return {
        "status": "ok",
        "product": slug,
        "version": version,
        "cycle": str(match.get("cycle")),
        "is_eol": is_eol,
        "eol": eol,
        "days_until_eol": days_until_eol,
        "latest_in_cycle": match.get("latest"),
        "latest_release_date": match.get("latestReleaseDate"),
        "lts": match.get("lts"),
        "support": match.get("support"),
        "newest_cycle": str(cycles[0].get("cycle")) if cycles else None,
        "newer_cycles": newer,
        "url": f"https://endoflife.date/{slug}",
    }


# ---------------------------------------------------------------------------
# Web threat research (Google Search grounding, single bounded call)
# ---------------------------------------------------------------------------

_RESEARCH_MODEL = CONFIG.research_model
_RESEARCH_TIMEOUT_MS = 90_000


def web_threat_research(focus: str, days: int) -> dict:
    """Research recent, actively exploited vulnerabilities, attack campaigns and
    malware trends relevant to ``focus`` using Google Search grounding.

    One bounded call (90 s timeout, no retries) that returns a Japanese
    summary plus the grounding sources. Call it once per audit with the whole
    stack in ``focus``.

    Args:
        focus: Technologies / platforms to cover, e.g. ``"Google Cloud (Cloud Run,
            Cloud SQL, IAM), PostgreSQL 17, Node.js 22, Next.js, Rust, Debian bookworm"``.
        days: Look-back window in days (7-90).
    """
    from google import genai
    from google.genai import types as genai_types

    days = max(7, min(int(days), 90))
    today = utcnow().date()
    since = today - dt.timedelta(days=days)
    prompt = (
        "あなたは脅威インテリジェンスの調査担当です。Google 検索を使い、"
        f"{since.isoformat()} から {today.isoformat()} の期間に公表・観測された、次の技術スタックに関係する\n"
        "「実際に悪用されている脆弱性（CISA KEV 掲載や in-the-wild の報告）」「攻撃キャンペーン」「マルウェア / ランサムウェアの流行」"
        "「クラウド（特に Google Cloud / Cloud Run / Cloud SQL / IAM / サービスアカウント鍵）を狙う手口」を調べてください。\n\n"
        f"対象スタック: {focus}\n\n"
        "出力形式（日本語、最大 10 件、新しい順）:\n"
        "- [YYYY-MM-DD] 項目名 — 影響する製品 / バージョン — 何が起きているか（1〜2 文）— 推奨対応 — 出典 URL\n\n"
        "期間外の古い情報や関連性の低いものは含めないでください。該当がなければ「該当なし」と明記してください。"
    )
    try:
        client = genai.Client()
        response = client.models.generate_content(
            model=_RESEARCH_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                temperature=0.2,
                max_output_tokens=3000,
                http_options=genai_types.HttpOptions(timeout=_RESEARCH_TIMEOUT_MS),
            ),
        )
    except Exception as exc:
        return {
            "status": "error",
            "message": f"{type(exc).__name__}: {str(exc)[:300]}",
            "hint": "Web 調査はスキップし、CISA KEV の結果のみで脅威動向をまとめてください。",
        }
    text = response.text or ""
    sources: list[dict[str, str]] = []
    queries: list[str] = []
    try:
        meta = response.candidates[0].grounding_metadata
        for chunk in meta.grounding_chunks or []:
            web = getattr(chunk, "web", None)
            if web and web.uri:
                sources.append({"title": web.title or "", "uri": web.uri})
        queries = list(meta.web_search_queries or [])
    except (AttributeError, IndexError, TypeError):
        pass
    return {
        "status": "ok",
        "period": {"from": since.isoformat(), "to": today.isoformat()},
        "focus": focus,
        "summary": redact(text)[:6000],
        "sources": sources[:20],
        "search_queries": queries[:10],
    }
