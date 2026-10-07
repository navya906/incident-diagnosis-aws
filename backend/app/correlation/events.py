"""Event classification shared by evidence ranking and chain detection."""

from __future__ import annotations

import re

from app.contracts.events import CanonicalEvent, EventSource, Severity

_READ_ONLY_PREFIXES = ("Describe", "Get", "List", "Lookup", "Head", "BatchGet")
_VARIABLE = re.compile(r"0x[0-9a-fA-F]+|\b[0-9a-fA-F]{8,}\b|\d+(?:\.\d+)?")


def event_kind(e: CanonicalEvent) -> str:
    """One of: metric, alarm, change, api_error, read_only, error_log, warning_log, info_log."""
    if e.source == EventSource.CLOUDWATCH_METRIC:
        return "metric"
    if e.source == EventSource.ALARM:
        return "alarm"
    if e.source == EventSource.AWS_CONFIG:
        return "change"
    if e.source == EventSource.CLOUDTRAIL:
        if e.metadata.get("errorCode"):
            return "api_error"
        if e.metadata.get("readOnly") is True or e.event_type.startswith(_READ_ONLY_PREFIXES):
            return "read_only"
        return "change"
    if e.severity in (Severity.ERROR, Severity.CRITICAL):
        return "error_log"
    if e.severity == Severity.WARNING:
        return "warning_log"
    return "info_log"


def message_template(message: str) -> str:
    """Message with numbers and hex ids replaced, so repeated log lines share a template."""
    return _VARIABLE.sub("#", message).strip()


def group_key(e: CanonicalEvent) -> tuple[str, ...]:
    """Redundancy group: one metric series, one log template, one API call or config stream."""
    if e.source == EventSource.CLOUDWATCH_METRIC:
        return ("metric", e.resource_id, e.metric or "")
    if e.source == EventSource.CLOUDWATCH_LOG:
        return ("log", e.resource_id, message_template(e.message))
    if e.source == EventSource.CLOUDTRAIL:
        return ("trail", e.resource_id, e.event_type)
    return (e.source.value, e.resource_id, e.event_type)


def event_text(e: CanonicalEvent) -> str:
    """Text used by the semantic component."""
    if e.source == EventSource.CLOUDWATCH_METRIC:
        return f"{e.metric} {e.resource_id}"
    return f"{e.event_type} {e.message}"
