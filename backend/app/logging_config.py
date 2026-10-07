"""Logging setup with secret filtering (plain-text or JSON lines)."""

from __future__ import annotations

import json
import logging
import re
import sys

_SECRET_PATTERNS = [
    re.compile(
        r"(?i)(api[_-]?key|secret|token|password|authorization)(['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]+)"
    ),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]


_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_X_API_KEY = re.compile(r"(?i)(x-api-key['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]+)")
#: Exact secret values the process knows (API keys); always replaced (D94).
_KNOWN: set[str] = set()


def register_secret(value: str) -> None:
    """Make `scrub` replace this exact value wherever it appears (e.g. configured API keys)."""
    if value and len(value) >= 6:
        _KNOWN.add(value)


def scrub(text: str) -> str:
    for value in sorted(_KNOWN, key=len, reverse=True):
        if value in text:
            text = text.replace(value, "***")
    # Bearer/Basic first: otherwise "Authorization: Bearer <token>" only loses the word Bearer.
    text = _BEARER.sub(lambda m: f"{m.group(1)} ***", text)
    text = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    text = _X_API_KEY.sub(lambda m: f"{m.group(1)}***", text)
    return _SECRET_PATTERNS[1].sub("AKIA****************", text)


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload)


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(SecretFilter())
    handler.setFormatter(
        _JsonFormatter()
        if json_logs
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
