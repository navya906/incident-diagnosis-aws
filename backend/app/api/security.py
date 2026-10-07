"""API-key authentication and rate limiting.

Authentication (DECISIONS D94):
- Every /api route needs a key: `X-API-Key: <key>` or HTTP Basic with the key as the password
  (`https://any:<key>@host/api/incidents`, the form SNS HTTPS subscriptions support).
- Keys come from `api.api_keys` (env only). Comparison is constant-time over all keys. The
  actor recorded in audit logs is `key-<first 12 hex of sha256(key)>`, never the key.
- No keys configured and `auth_disabled` false: every /api request is rejected (fail closed).
  `auth_disabled` is refused in aws mode.
- Configured keys are registered with the log scrubber, so they never appear in logs.

Rate limiting: one token bucket per client (the actor for valid keys, else the client IP, so
failed guesses are limited too): `rate_limit_per_minute` refill, `rate_limit_burst` capacity;
429 with Retry-After when empty.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.config import Settings
from app.logging_config import register_secret


def key_id(key: str) -> str:
    return "key-" + hashlib.sha256(key.encode()).hexdigest()[:12]


class AuthConfigError(RuntimeError):
    """Unsafe authentication configuration (e.g. auth disabled in aws mode)."""


class ApiKeyAuth:
    def __init__(self, settings: Settings):
        self.disabled = settings.api.auth_disabled
        if self.disabled and settings.data_mode == "aws":
            raise AuthConfigError("api.auth_disabled is not allowed in aws mode")
        self._keys = [k.get_secret_value() for k in settings.api.api_keys if k.get_secret_value()]
        for k in self._keys:
            register_secret(k)

    @property
    def configured(self) -> bool:
        return self.disabled or bool(self._keys)

    @staticmethod
    def presented(headers) -> str | None:
        key = headers.get("x-api-key")
        if key:
            return key
        auth = headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth[6:].strip(), validate=True).decode()
            except (binascii.Error, UnicodeDecodeError):
                return None
            return decoded.split(":", 1)[1] if ":" in decoded else None
        return None

    def actor(self, headers) -> str | None:
        """The actor id for a valid key, "anonymous" when auth is disabled, else None."""
        if self.disabled:
            return "anonymous"
        key = self.presented(headers)
        if not key:
            return None
        ok = False
        for k in self._keys:  # compare against every key so timing does not reveal which
            ok |= hmac.compare_digest(key.encode(), k.encode())
        return key_id(key) if ok else None


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, per_minute: int, burst: int, clock: Callable[[], float] = time.monotonic):
        self.rate = per_minute / 60.0
        self.burst = float(burst)
        self.clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def take(self, client: str) -> float:
        """0 when allowed, else the seconds to wait before retrying."""
        with self._lock:
            now = self.clock()
            b = self._buckets.get(client)
            if b is None:
                b = self._buckets[client] = _Bucket(self.burst, now)
            b.tokens = min(self.burst, b.tokens + (now - b.updated) * self.rate)
            b.updated = now
            if b.tokens >= 1:
                b.tokens -= 1
                return 0.0
            return math.ceil((1 - b.tokens) / self.rate)
