"""Shared AWS plumbing: clients, retry/backoff, rate limiting, sensitive-field filtering.

Credentials are resolved only by the standard boto3 chain (env vars, shared config/profile, SSO,
instance/task role). Nothing in this repository stores or accepts raw keys.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, TypeVar

from app.config import AwsSettings
from app.logging_config import scrub

log = logging.getLogger(__name__)
T = TypeVar("T")

THROTTLE_CODES = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "ThrottledException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "RequestThrottled",
        "SlowDown",
        "LimitExceededException",
    }
)


class AwsClients:
    """Lazily created boto3 clients sharing one session and retry configuration."""

    def __init__(self, settings: AwsSettings, session: Any | None = None):
        import boto3  # noqa: PLC0415 - optional dependency (the `aws` extra)
        from botocore.config import Config  # noqa: PLC0415

        self.settings = settings
        self.region = settings.region
        self._session = session or boto3.session.Session(
            profile_name=settings.profile, region_name=settings.region
        )
        self._config = Config(
            region_name=settings.region,
            retries={"mode": "adaptive", "max_attempts": settings.max_attempts},
            connect_timeout=10,
            read_timeout=30,
            user_agent_extra="incident-diagnosis-collector",
        )
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    def client(self, service: str) -> Any:
        with self._lock:
            if service not in self._clients:
                self._clients[service] = self._session.client(service, config=self._config)
            return self._clients[service]

    def set_client(self, service: str, client: Any) -> None:
        """Inject a pre-built client (used with botocore Stubber in tests)."""
        self._clients[service] = client


def error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


def call_with_backoff(
    fn: Callable[..., T],
    *args: Any,
    attempts: int = 6,
    base_delay: float = 0.5,
    max_delay: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> T:
    """Call `fn`, retrying throttling errors with exponential backoff and full jitter.

    botocore's adaptive retry mode already retries; this outer loop covers sustained throttling
    (e.g. CloudTrail's 2 TPS limit) without failing the whole collection.
    """
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if error_code(exc) not in THROTTLE_CODES or attempt == attempts - 1:
                raise
            delay = min(max_delay, base_delay * 2**attempt) * random.uniform(0.5, 1.0)
            log.warning("throttled (%s), retrying in %.1fs", error_code(exc), delay)
            sleep(delay)
    raise AssertionError("unreachable")


class RateLimiter:
    """Token bucket: at most `rate` calls per second, bursts up to `burst`."""

    def __init__(
        self,
        rate: float,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rate, self.burst = rate, burst
        self._clock, self._sleep = clock, sleep
        self._tokens = float(burst)
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self.rate
                self._sleep(wait)
                self._last = self._clock()
                self._tokens = 0.0
            else:
                self._tokens -= 1


def paginate(
    call: Callable[..., dict], token_in: str, token_out: str, **params: Any
) -> Iterator[dict]:
    """Generic NextToken pagination with throttling backoff, stopping on a repeated token."""
    token: str | None = None
    seen: set[str] = set()
    while True:
        page = call_with_backoff(call, **params, **({token_in: token} if token else {}))
        yield page
        token = page.get(token_out)
        if not token or token in seen:
            return
        seen.add(token)


_SENSITIVE_KEY = re.compile(
    r"(passw|secret|token|credential|accesskey|access_key|privatekey|private_key|"
    r"authorization|sessionkey|apikey|api_key|signature|cookie)",
    re.I,
)
REDACTED = "***"


def sanitize(value: Any, max_chars: int = 2000, _depth: int = 0) -> Any:
    """Drop values under sensitive keys and scrub secret-looking substrings, recursively."""
    if _depth > 8:
        return REDACTED
    if isinstance(value, dict):
        return {
            str(k): (
                REDACTED if _SENSITIVE_KEY.search(str(k)) else sanitize(v, max_chars, _depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize(v, max_chars, _depth + 1) for v in value[:50]]
    if isinstance(value, str):
        text = scrub(value)
        return text if len(text) <= max_chars else text[:max_chars] + "...[truncated]"
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def to_ms(ts: datetime) -> int:
    return int(ts.astimezone(UTC).timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)
