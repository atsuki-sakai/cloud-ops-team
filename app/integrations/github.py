"""Minimal GitHub REST + GraphQL client (the issue tracker integration).

Token resolution order: ``GITHUB_TOKEN`` → ``GH_TOKEN`` → ``gh auth token``
(local development only). When deployed, inject the token from Secret
Manager as the ``GITHUB_TOKEN`` environment variable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

import httpx

from app.config import CONFIG
from app.core.cache import TtlValue
from app.core.errors import error_message, tool_error

API = "https://api.github.com"
_token_cache: TtlValue[str] = TtlValue()


class GitHubAuthError(RuntimeError):
    pass


def _load_token() -> str:
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if not token and (gh := shutil.which("gh")):
        try:
            token = subprocess.run(  # noqa: S603 -- fixed argv, resolved gh binary
                [gh, "auth", "token"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            token = ""
    if not token:
        raise GitHubAuthError(
            "GitHub token not found. Set GITHUB_TOKEN (recommended: from Secret Manager) "
            "or log in with `gh auth login` for local development."
        )
    return token


def github_token() -> str:
    """Resolve the token once per process; a failed lookup is retried next call."""
    return _token_cache.get_or_set(_load_token)


def _headers(accept: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {github_token()}",
        "Accept": accept or "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "cloud-ops-team-agent",
    }


def _error(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    message = body.get("message") if isinstance(body, dict) else None
    status = "error"
    if resp.status_code == 404:
        status = "not_found"
    elif resp.status_code in (401, 403):
        status = "permission_denied"
    elif resp.status_code == 422:
        status = "invalid"
    return {
        "status": status,
        "http_status": resp.status_code,
        "message": message or resp.text[:400],
        "errors": body.get("errors") if isinstance(body, dict) else None,
    }


def gh_rest(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    accept: str | None = None,
    timeout: float = 30.0,
) -> tuple[bool, Any]:
    """Return ``(ok, payload)``; raw media types preserve text as ``{"raw": text}``."""
    try:
        resp = httpx.request(
            method,
            path if path.startswith("http") else f"{API}{path}",
            params=params,
            json=json,
            headers=_headers(accept),
            timeout=timeout,
        )
    except GitHubAuthError as exc:
        return False, {"status": "auth_error", "message": error_message(exc)}
    except httpx.HTTPError as exc:
        return False, tool_error(exc)
    if resp.status_code >= 400:
        return False, _error(resp)
    if accept and "application/vnd.github.raw" in accept:
        return True, {"raw": resp.text}
    if resp.status_code == 204 or not resp.content:
        return True, {}
    try:
        return True, resp.json()
    except ValueError:
        return True, {"raw": resp.text[:2000]}


def gh_graphql(query: str, variables: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    ok, payload = gh_rest(
        "POST", "/graphql", json={"query": query, "variables": variables}
    )
    if not ok:
        return False, payload
    if payload.get("errors"):
        return False, {
            "status": "error",
            "message": "; ".join(e.get("message", "") for e in payload["errors"])[:500],
        }
    return True, payload.get("data") or {}


def repo_path() -> str:
    return f"/repos/{CONFIG.github_repo}"
