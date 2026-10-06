"""CanonicalEvent: the single event shape emitted by every collector (real and replay)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventSource(StrEnum):
    CLOUDWATCH_METRIC = "cloudwatch_metric"
    CLOUDWATCH_LOG = "cloudwatch_log"
    CLOUDTRAIL = "cloudtrail"
    AWS_CONFIG = "aws_config"
    ALARM = "alarm"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def make_event_id(
    *,
    timestamp: datetime,
    source: str,
    service: str,
    resource_id: str,
    event_type: str,
    metric: str | None,
    raw_ref: str | None,
) -> str:
    """Deterministic event id: identical inputs always give the identical id."""
    ts = _as_utc(timestamp).isoformat(timespec="microseconds")
    key = "|".join([ts, str(source), service, resource_id, event_type, metric or "", raw_ref or ""])
    return "ev_" + hashlib.sha256(key.encode()).hexdigest()[:16]


class CanonicalEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    timestamp: datetime
    source: EventSource
    service: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    metric: str | None = None
    value: float | None = None
    severity: Severity = Severity.INFO
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw_ref: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _as_utc(v)

    @classmethod
    def build(
        cls,
        *,
        timestamp: datetime,
        source: EventSource,
        service: str,
        resource_id: str,
        event_type: str,
        metric: str | None = None,
        value: float | None = None,
        severity: Severity = Severity.INFO,
        message: str = "",
        metadata: dict[str, Any] | None = None,
        raw_ref: str | None = None,
    ) -> CanonicalEvent:
        """Construct an event with its deterministic id filled in."""
        return cls(
            event_id=make_event_id(
                timestamp=timestamp,
                source=source.value,
                service=service,
                resource_id=resource_id,
                event_type=event_type,
                metric=metric,
                raw_ref=raw_ref,
            ),
            timestamp=timestamp,
            source=source,
            service=service,
            resource_id=resource_id,
            event_type=event_type,
            metric=metric,
            value=value,
            severity=severity,
            message=message,
            metadata=metadata or {},
            raw_ref=raw_ref,
        )
