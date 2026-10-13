"""Thin, read-only REST client for Google Cloud APIs.

Uses Application Default Credentials (gcloud ADC locally, the runtime
service account when deployed). Every call returns ``(ok, payload)``:
``payload`` is the decoded JSON body on success, or a normalized error
dict (``status`` in ``api_disabled`` / ``permission_denied`` /
``not_found`` / ``rate_limited`` / ``error``) that tools can pass straight
back to the model instead of raising.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Any

import google.auth
import httpx
from google.auth.transport.requests import Request

from app.config import CONFIG

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_lock = threading.Lock()
_creds = None
_gcloud_token: tuple[float, str] | None = None

# OPS_GCP_AUTH=adc (default): Application Default Credentials — the runtime
# service account when deployed, `gcloud auth application-default login` locally.
# OPS_GCP_AUTH=gcloud: local-development escape hatch that borrows the active
# `gcloud` account's token (useful when ADC belongs to a different account).
_AUTH_MODE = (os.getenv("OPS_GCP_AUTH") or "adc").strip().lower()


def _gcloud_access_token() -> str:
    global _gcloud_token
    if _gcloud_token and time.time() - _gcloud_token[0] < 45 * 60:
        return _gcloud_token[1]
    if not shutil.which("gcloud"):
        raise RuntimeError("OPS_GCP_AUTH=gcloud but the gcloud CLI is not installed")
    token = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    _gcloud_token = (time.time(), token)
    return token


def _access_token() -> str:
    global _creds
    with _lock:
        if _AUTH_MODE == "gcloud":
            return _gcloud_access_token()
        if _creds is None:
            _creds, _ = google.auth.default(scopes=_SCOPES)
        if not _creds.valid:
            _creds.refresh(Request())
        return _creds.token


def _normalize_error(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    err = body.get("error", {}) if isinstance(body, dict) else {}
    message = (err.get("message") if isinstance(err, dict) else None) or resp.text[:500]
    reason, service = "", ""
    for detail in (err.get("details") or []) if isinstance(err, dict) else []:
        if isinstance(detail, dict) and detail.get("reason"):
            reason = detail["reason"]
            service = (detail.get("metadata") or {}).get("service", "")
    status = "error"
    if resp.status_code == 403 and (
        reason == "SERVICE_DISABLED"
        or "has not been used in project" in message
        or "API has not been enabled" in message
    ):
        status = "api_disabled"
    elif resp.status_code == 403:
        status = "permission_denied"
    elif resp.status_code == 404:
        status = "not_found"
    elif resp.status_code == 429:
        status = "rate_limited"
    return {
        "status": status,
        "http_status": resp.status_code,
        "message": message,
        "reason": reason,
        "api": service,
        "url": str(resp.request.url).split("?", 1)[0],
    }


def gcp_request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> tuple[bool, dict[str, Any]]:
    """Perform one authenticated request. Never raises for HTTP errors."""
    try:
        headers = {
            "Authorization": f"Bearer {_access_token()}",
            "x-goog-user-project": CONFIG.target,
        }
        resp = httpx.request(
            method, url, params=params, json=json, headers=headers, timeout=timeout
        )
    except Exception as exc:  # network / auth failures
        return False, {
            "status": "error",
            "message": f"{type(exc).__name__}: {exc}",
            "url": url,
        }
    if resp.status_code >= 400:
        return False, _normalize_error(resp)
    if not resp.content:
        return True, {}
    try:
        return True, resp.json()
    except ValueError:
        return True, {"raw": resp.text[:2000]}


def gcp_get(
    url: str, params: dict[str, Any] | None = None
) -> tuple[bool, dict[str, Any]]:
    return gcp_request("GET", url, params=params)


def gcp_post(
    url: str, json: dict[str, Any] | None = None
) -> tuple[bool, dict[str, Any]]:
    return gcp_request("POST", url, json=json)


def gcp_list(
    url: str,
    item_key: str,
    *,
    params: dict[str, Any] | None = None,
    method: str = "GET",
    json: dict[str, Any] | None = None,
    max_pages: int = 5,
) -> tuple[bool, list[Any] | dict[str, Any]]:
    """Follow ``nextPageToken`` pagination and concatenate ``item_key`` lists."""
    items: list[Any] = []
    params = dict(params or {})
    body = dict(json or {})
    token: str | None = None
    for _ in range(max_pages):
        if token:
            if method == "GET":
                params["pageToken"] = token
            else:
                body["pageToken"] = token
        ok, payload = gcp_request(
            method, url, params=params, json=body if method != "GET" else None
        )
        if not ok:
            return False, payload
        items.extend(payload.get(item_key, []) or [])
        token = payload.get("nextPageToken")
        if not token:
            break
    return True, items


def project_path() -> str:
    return f"projects/{CONFIG.target}"
