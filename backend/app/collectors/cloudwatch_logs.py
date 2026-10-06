"""CloudWatch Logs via FilterLogEvents: bounded per log group and filtered for problem lines."""

from __future__ import annotations

import logging
import re

from app.collectors.aws_common import AwsClients, error_code, from_ms, paginate, sanitize, to_ms
from app.collectors.resources import ResourceIndex
from app.contracts.events import CanonicalEvent, EventSource, Severity
from app.interfaces.collector import CollectionRequest, Collector

log = logging.getLogger(__name__)

_LEVELS = [
    (re.compile(r"\b(CRITICAL|FATAL|PANIC)\b", re.I), Severity.CRITICAL),
    (re.compile(r"\b(ERROR|Exception|Traceback|AccessDenied\w*|denied)\b", re.I), Severity.ERROR),
    (re.compile(r"\b(WARN|WARNING)\b", re.I), Severity.WARNING),
]


def infer_severity(message: str) -> Severity:
    for pattern, level in _LEVELS:
        if pattern.search(message):
            return level
    return Severity.INFO


class CloudWatchLogsCollector(Collector):
    name = "cloudwatch_logs"
    IAM_ACTIONS = frozenset({"logs:FilterLogEvents"})

    def __init__(self, clients: AwsClients, index: ResourceIndex):
        self.clients, self.index = clients, index
        self.warnings: list[str] = []

    def collect(self, request: CollectionRequest) -> list[CanonicalEvent]:
        self.warnings = []
        if request.sources is not None and EventSource.CLOUDWATCH_LOG not in request.sources:
            return []
        s = self.clients.settings
        logs = self.clients.client("logs")
        events: list[CanonicalEvent] = []
        for rid in request.resource_ids:
            res = self.index.get(rid)
            if res is None:
                self.warnings.append(f"logs: unknown resource {rid}")
                continue
            for group in res.log_groups:
                params = {
                    "logGroupName": group,
                    "startTime": to_ms(request.window_start),
                    "endTime": to_ms(request.window_end),
                    "limit": min(10000, s.logs_max_events_per_group),
                }
                if s.logs_filter_pattern:
                    params["filterPattern"] = s.logs_filter_pattern
                taken = 0
                try:
                    for page in paginate(
                        logs.filter_log_events, "nextToken", "nextToken", **params
                    ):
                        for raw in page.get("events", []):
                            if taken >= s.logs_max_events_per_group:
                                break
                            taken += 1
                            message = sanitize(
                                raw.get("message", "").rstrip(), s.logs_max_message_chars
                            )
                            stream = raw.get("logStreamName", "")
                            events.append(
                                CanonicalEvent.build(
                                    timestamp=from_ms(raw["timestamp"]),
                                    source=EventSource.CLOUDWATCH_LOG,
                                    service=res.service,
                                    resource_id=rid,
                                    event_type="log_line",
                                    severity=infer_severity(message),
                                    message=message,
                                    metadata={"log_group": group, "log_stream": stream},
                                    raw_ref=f"cwlogs:{group}:{stream}:{raw.get('eventId', '')}",
                                )
                            )
                        if taken >= s.logs_max_events_per_group:
                            self.warnings.append(
                                f"logs: {group} truncated at {s.logs_max_events_per_group} events"
                            )
                            break
                except Exception as exc:
                    if error_code(exc) == "ResourceNotFoundException":
                        self.warnings.append(f"logs: log group {group} not found")
                        continue
                    raise
        return sorted(events, key=lambda e: (e.timestamp, e.event_id))
