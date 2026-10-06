"""CloudTrail management events via LookupEvents.

LookupEvents allows 2 requests/second per account and region and one lookup attribute per call,
so calls are rate limited and retried on throttling. CloudTrail delivers events with a lag
(typically up to ~15 minutes); collecting a window that ends inside that lag adds a warning.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from app.collectors.aws_common import AwsClients, RateLimiter, paginate, sanitize
from app.collectors.resources import ResourceIndex
from app.contracts.events import CanonicalEvent, EventSource, Severity
from app.interfaces.collector import CollectionRequest, Collector

log = logging.getLogger(__name__)
_MAX_PARAM_CHARS = 1500


def _user(detail: dict) -> str:
    ui = detail.get("userIdentity") or {}
    return ui.get("arn") or ui.get("principalId") or ui.get("type") or "unknown"


class CloudTrailCollector(Collector):
    name = "cloudtrail"
    IAM_ACTIONS = frozenset({"cloudtrail:LookupEvents"})

    def __init__(
        self,
        clients: AwsClients,
        index: ResourceIndex,
        rate_limiter: RateLimiter | None = None,
        now: datetime | None = None,
    ):
        self.clients, self.index = clients, index
        s = clients.settings
        self.limiter = rate_limiter or RateLimiter(s.cloudtrail_requests_per_second)
        self._now = now
        self.warnings: list[str] = []

    def _throttled_lookup(self, **params):
        self.limiter.acquire()
        return self.clients.client("cloudtrail").lookup_events(**params)

    def collect(self, request: CollectionRequest) -> list[CanonicalEvent]:
        self.warnings = []
        if request.sources is not None and EventSource.CLOUDTRAIL not in request.sources:
            return []
        s = self.clients.settings
        now = self._now or datetime.now(UTC)
        if request.window_end > now - timedelta(minutes=s.cloudtrail_ingestion_lag_minutes):
            self.warnings.append(
                "cloudtrail: window ends within the ingestion lag "
                f"({s.cloudtrail_ingestion_lag_minutes} min); recent events may be missing"
            )
        seen: set[str] = set()
        events: list[CanonicalEvent] = []
        for rid in request.resource_ids:
            res = self.index.get(rid)
            if res is None:
                self.warnings.append(f"cloudtrail: unknown resource {rid}")
                continue
            for name in res.cloudtrail_names:
                for page in paginate(
                    self._throttled_lookup,
                    "NextToken",
                    "NextToken",
                    LookupAttributes=[{"AttributeKey": "ResourceName", "AttributeValue": name}],
                    StartTime=request.window_start,
                    EndTime=request.window_end,
                    MaxResults=50,
                ):
                    for raw in page.get("Events", []):
                        eid = raw["EventId"]
                        if eid in seen:
                            continue
                        seen.add(eid)
                        ev = self._to_event(raw, rid, res.region)
                        if ev is not None:
                            events.append(ev)
        return sorted(events, key=lambda e: (e.timestamp, e.event_id))

    def _to_event(self, raw: dict, rid: str, region: str) -> CanonicalEvent | None:
        s = self.clients.settings
        try:
            detail = json.loads(raw.get("CloudTrailEvent") or "{}")
        except json.JSONDecodeError:
            detail = {}
        read_only = str(raw.get("ReadOnly", detail.get("readOnly", "false"))).lower() == "true"
        if read_only and not s.cloudtrail_include_read_only:
            return None
        name = raw.get("EventName") or detail.get("eventName") or "Unknown"
        source = raw.get("EventSource") or detail.get("eventSource") or "unknown.amazonaws.com"
        user = sanitize(_user(detail))
        code = detail.get("errorCode")
        message = f"{name} by {user}"
        if code:
            message += f" failed: {code}"
            if detail.get("errorMessage"):
                message += f": {detail['errorMessage']}"
        metadata = {
            "eventSource": source,
            "awsRegion": detail.get("awsRegion", region),
            "userIdentity": user,
            "readOnly": read_only,
        }
        if code:
            metadata["errorCode"] = code
        params = detail.get("requestParameters")
        if isinstance(params, dict):
            clean = sanitize(params)
            text = json.dumps(clean, sort_keys=True, default=str)
            metadata["requestParameters"] = (
                clean if len(text) <= _MAX_PARAM_CHARS else {"truncated": text[:_MAX_PARAM_CHARS]}
            )
        ts = raw.get("EventTime")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return CanonicalEvent.build(
            timestamp=ts.astimezone(UTC),
            source=EventSource.CLOUDTRAIL,
            service=source.split(".")[0],
            resource_id=rid,
            event_type=name,
            severity=Severity.ERROR if code else Severity.INFO,
            message=sanitize(message),
            metadata=metadata,
            raw_ref=f"ct:{raw['EventId']}",
        )
