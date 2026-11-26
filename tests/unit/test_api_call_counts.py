"""External API call counts stay flat as inputs grow (N+1 regressions).

The ``api-call-in-loop`` ast-grep rule only sees syntax. These tests run the
code and count the calls it actually makes through the patched entry points.
"""

from __future__ import annotations

import time

import pytest

from app.providers.gcp import security
from app.tools import issues


@pytest.fixture(autouse=True)
def _fresh_label_cache():
    issues._ensured_labels.clear()
    yield
    issues._ensured_labels.clear()


def _fake_label_api(existing: list[str]):
    created: list[str] = []

    def fake(method, path, **kwargs):
        if method == "GET" and path.endswith("/labels"):
            page = kwargs["params"]["page"]
            size = kwargs["params"]["per_page"]
            chunk = existing[(page - 1) * size : page * size]
            return True, [{"name": name} for name in chunk]
        if method == "POST" and path.endswith("/labels"):
            created.append(kwargs["json"]["name"])
            return True, {"name": kwargs["json"]["name"]}
        raise AssertionError(f"unexpected call {method} {path}")

    return fake, created


@pytest.mark.parametrize("label_count", [1, 5, 40])
def test_label_check_reads_once_regardless_of_label_count(api_calls, label_count):
    labels = [f"label-{i}" for i in range(label_count)]
    fake, created = _fake_label_api(existing=[*labels[1:], "unrelated"])
    api_calls.patch(issues, "gh_rest", fake)

    ok, out = issues._ensure_labels(labels)

    assert ok and out == ["label-0"] and created == ["label-0"]
    assert api_calls.count("gh_rest", "GET") == 1
    assert api_calls.count("gh_rest", "POST") == 1


def test_label_check_pages_through_large_repositories(api_calls):
    existing = [f"x-{i}" for i in range(250)] + ["Ops"]
    fake, created = _fake_label_api(existing=existing)
    api_calls.patch(issues, "gh_rest", fake)

    # GitHub compares label names case-insensitively: "Ops" already exists.
    ok, _ = issues._ensure_labels(["ops"])

    assert ok and created == []
    assert api_calls.count("gh_rest", "GET") == 3  # 100 + 100 + 51 names


def test_confirmed_labels_are_not_read_again(api_calls):
    fake, _ = _fake_label_api(existing=["ops"])
    api_calls.patch(issues, "gh_rest", fake)

    issues._ensure_labels(["ops"])
    issues._ensure_labels(["ops", "ops"])

    assert api_calls.count("gh_rest") == 1


def test_label_read_failure_stops_before_any_write(api_calls):
    api_calls.patch(
        issues,
        "gh_rest",
        lambda *a, **k: (False, {"status": "error", "http_status": 503}),
    )

    ok, out = issues._ensure_labels(["a", "b"])

    assert not ok and out["http_status"] == 503
    assert api_calls.count("gh_rest", "POST") == 0


def test_image_scan_calls_once_per_image_with_bounded_concurrency(api_calls):
    def fake_list(url, key, **kwargs):
        image = kwargs["params"]["filter"].split('resourceUrl="https://', 1)[1][:-1]
        time.sleep(0.05)  # long enough for the pool to overlap calls up to its bound
        return True, [{"vulnerability": {"severity": "LOW"}, "image": image}]

    api_calls.patch(security, "gcp_list", fake_list)
    images = [f"gcr.io/p/app-{i}@sha256:{i:064x}" for i in range(10)]

    fetched = security._image_occurrences_bounded(images)

    assert list(fetched) == images
    assert all(fetched[image][1][0]["image"] == image for image in images)
    assert api_calls.count("gcp_list") == len(images)
    assert 1 < api_calls.peak_concurrency <= security._SCAN_WORKERS
