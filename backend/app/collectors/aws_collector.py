"""Composite real-AWS collector (metrics, logs, CloudTrail, Config) with an optional event cache."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from pydantic import TypeAdapter

from app.collectors.aws_common import AwsClients
from app.collectors.cloudtrail import CloudTrailCollector
from app.collectors.cloudwatch_logs import CloudWatchLogsCollector
from app.collectors.cloudwatch_metrics import CloudWatchMetricsCollector
from app.collectors.config_history import ConfigHistoryCollector
from app.collectors.inventory import IAM_ACTIONS as INVENTORY_ACTIONS
from app.collectors.inventory import Inventory, InventoryDiscovery
from app.collectors.resources import ResourceIndex
from app.config import AwsSettings
from app.contracts.events import CanonicalEvent
from app.interfaces.collector import CollectionRequest, Collector

log = logging.getLogger(__name__)
_EVENTS = TypeAdapter(list[CanonicalEvent])


class EventCache:
    """On-disk cache of collected events, keyed by collector, request and resource fingerprint."""

    def __init__(self, directory: str | Path, ttl_seconds: int, clock=time.time):
        self.dir = Path(directory)
        self.ttl = ttl_seconds
        self._clock = clock

    @staticmethod
    def key(collector: str, request: CollectionRequest, index: ResourceIndex) -> str:
        fingerprint = [
            index.get(r).model_dump(mode="json") if index.get(r) else None
            for r in sorted(request.resource_ids)
        ]
        payload = json.dumps(
            [collector, request.model_dump(mode="json"), fingerprint], sort_keys=True
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, key: str) -> list[CanonicalEvent] | None:
        path = self.dir / f"{key}.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
        if self._clock() - data["created"] > self.ttl:
            return None
        return _EVENTS.validate_python(data["events"])

    def put(self, key: str, events: list[CanonicalEvent]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {"created": self._clock(), "events": _EVENTS.dump_python(events, mode="json")}
        (self.dir / f"{key}.json").write_text(json.dumps(payload))


class AwsCollector(Collector):
    """Real collector. Emits CanonicalEvents with the same schema as ReplayCollector."""

    name = "aws"

    def __init__(
        self,
        clients: AwsClients,
        index: ResourceIndex,
        cache: EventCache | None = None,
        collectors: list[Collector] | None = None,
    ):
        self.clients, self.index, self.cache = clients, index, cache
        self.collectors = collectors or [
            CloudWatchMetricsCollector(clients, index),
            CloudWatchLogsCollector(clients, index),
            CloudTrailCollector(clients, index),
            ConfigHistoryCollector(clients, index),
        ]
        self.warnings: list[str] = []

    @classmethod
    def from_settings(
        cls, settings: AwsSettings, seed_arns: list[str], session=None
    ) -> tuple[AwsCollector, Inventory]:
        clients = AwsClients(settings, session=session)
        inventory = InventoryDiscovery(clients).discover(seed_arns)
        cache = (
            EventCache(settings.cache_dir, settings.cache_ttl_seconds)
            if settings.cache_dir
            else None
        )
        return cls(clients, inventory.index, cache=cache), inventory

    def collect(self, request: CollectionRequest) -> list[CanonicalEvent]:
        self.warnings = []
        merged: dict[str, CanonicalEvent] = {}
        for c in self.collectors:
            key = EventCache.key(c.name, request, self.index) if self.cache else None
            cached = self.cache.get(key) if self.cache else None
            if cached is not None:
                events = cached
            else:
                events = c.collect(request)
                warnings = getattr(c, "warnings", [])
                self.warnings += warnings
                # Results with warnings (e.g. CloudTrail ingestion lag) are not cached.
                if self.cache and not warnings:
                    self.cache.put(key, events)
            for e in events:
                merged[e.event_id] = e
        for w in self.warnings:
            log.warning(w)
        return sorted(merged.values(), key=lambda e: (e.timestamp, e.event_id))


def required_iam_actions() -> frozenset[str]:
    """Every AWS API action the collectors and inventory discovery call."""
    return frozenset(
        CloudWatchMetricsCollector.IAM_ACTIONS
        | CloudWatchLogsCollector.IAM_ACTIONS
        | CloudTrailCollector.IAM_ACTIONS
        | ConfigHistoryCollector.IAM_ACTIONS
        | INVENTORY_ACTIONS
    )
