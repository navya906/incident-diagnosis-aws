"""CanonicalEvent: the single event shape emitted by every collector (real and replay)."""

from __future__ import annotations

import hashlib
import re
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


# ----------------------------------------------------------------------------------------------
# Per-source schema. Every collector (real or replay) must satisfy these; checked by
# `contract_violations` in tests (DECISIONS D31).

#: Canonical resource ids, shared by real collectors, inventory discovery and the simulator.
CANONICAL_RESOURCE_ID = re.compile(
    r"^(alb|tg|ecs/service|rds|lambda/function|ec2/instance|sqs/queue|sg|iam/role|s3/bucket)"
    r"/[A-Za-z0-9._+=@-]+$"
)


class SourceSchema(BaseModel):
    model_config = ConfigDict(frozen=True)

    required_metadata: frozenset[str]
    optional_metadata: frozenset[str] = frozenset()
    raw_ref_prefix: str
    fixed_event_type: str | None = None  # None: free-form (e.g. the CloudTrail eventName)
    has_metric_value: bool = False


SOURCE_SCHEMAS: dict[EventSource, SourceSchema] = {
    EventSource.CLOUDWATCH_METRIC: SourceSchema(
        required_metadata=frozenset({"namespace", "stat", "period_seconds", "unit"}),
        raw_ref_prefix="cw:",
        fixed_event_type="metric_datapoint",
        has_metric_value=True,
    ),
    EventSource.CLOUDWATCH_LOG: SourceSchema(
        required_metadata=frozenset({"log_group", "log_stream"}),
        raw_ref_prefix="cwlogs:",
        fixed_event_type="log_line",
    ),
    EventSource.CLOUDTRAIL: SourceSchema(
        required_metadata=frozenset({"eventSource", "awsRegion", "userIdentity"}),
        optional_metadata=frozenset({"errorCode", "requestParameters", "readOnly"}),
        raw_ref_prefix="ct:",
    ),
    EventSource.AWS_CONFIG: SourceSchema(
        required_metadata=frozenset({"resourceType", "configurationItemStatus"}),
        optional_metadata=frozenset({"configurationStateId"}),
        raw_ref_prefix="cfg:",
        fixed_event_type="ConfigurationItemChange",
    ),
    EventSource.ALARM: SourceSchema(
        required_metadata=frozenset({"alarm_name", "state", "previous_state", "threshold"}),
        raw_ref_prefix="alarm:",
        fixed_event_type="alarm_state_change",
        has_metric_value=True,
    ),
}


def contract_violations(event: CanonicalEvent) -> list[str]:
    """Return every way `event` deviates from the per-source schema (empty list = valid)."""
    schema = SOURCE_SCHEMAS[event.source]
    problems: list[str] = []
    keys = set(event.metadata)
    missing = schema.required_metadata - keys
    extra = keys - schema.required_metadata - schema.optional_metadata
    if missing:
        problems.append(f"missing metadata {sorted(missing)}")
    if extra:
        problems.append(f"unexpected metadata {sorted(extra)}")
    if not (event.raw_ref or "").startswith(schema.raw_ref_prefix):
        problems.append(f"raw_ref must start with {schema.raw_ref_prefix!r}")
    if schema.fixed_event_type and event.event_type != schema.fixed_event_type:
        problems.append(f"event_type must be {schema.fixed_event_type!r}")
    if schema.has_metric_value and (event.metric is None or event.value is None):
        problems.append("metric and value are required")
    if not schema.has_metric_value and event.metric is not None:
        problems.append("metric must be None for this source")
    if not CANONICAL_RESOURCE_ID.match(event.resource_id):
        problems.append(f"non-canonical resource_id {event.resource_id!r}")
    expected_id = make_event_id(
        timestamp=event.timestamp,
        source=event.source.value,
        service=event.service,
        resource_id=event.resource_id,
        event_type=event.event_type,
        metric=event.metric,
        raw_ref=event.raw_ref,
    )
    if event.event_id != expected_id:
        problems.append("event_id does not match make_event_id(...)")
    return problems
