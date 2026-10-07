"""Minimal JSON-over-HTTPS transport (stdlib only) with retries for 429 / 5xx / network errors.

External clients take an injectable `Post` callable so tests can record exactly what would be
sent without any network access.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

Post = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]

RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


def http_post(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (configured URL)
        return json.loads(resp.read().decode())


def post_with_retries(
    post: Post,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
    max_retries: int,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Exponential backoff (1, 2, 4 ... s) on retryable HTTP status codes and network errors."""
    attempt = 0
    while True:
        try:
            return post(url, headers, body, timeout)
        except urllib.error.HTTPError as e:
            if e.code not in RETRYABLE_STATUS or attempt >= max_retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt >= max_retries:
                raise
        sleep(2.0**attempt)
        attempt += 1
