"""ReplayCollector: serves a generated dataset through the same interface as the real collectors."""

from __future__ import annotations

from pathlib import Path

from app.contracts.events import CanonicalEvent
from app.interfaces.collector import CollectionRequest, Collector
from app.offline.dataset import DatasetLoader
from app.offline.models import IncidentRecord, RelationshipRecord, ResourceRecord


class ReplayCollector(Collector):
    """Reads only the observable part of each incident; ground truth is never touched."""

    name = "replay"

    def __init__(self, dataset_dir: str | Path | DatasetLoader):
        self.loader = (
            dataset_dir if isinstance(dataset_dir, DatasetLoader) else DatasetLoader(dataset_dir)
        )

    def incident_ids(self, split: str | None = None) -> list[str]:
        return self.loader.incident_ids(split)

    def incident(self, incident_id: str) -> IncidentRecord:
        return self.loader.load(incident_id).incident

    def inventory(self, incident_id: str) -> tuple[list[ResourceRecord], list[RelationshipRecord]]:
        """Resources and relationships for building the dependency graph offline."""
        s = self.loader.load(incident_id)
        return s.resources, s.relationships

    def collect(self, request: CollectionRequest) -> list[CanonicalEvent]:
        scenario = self.loader.load(request.incident_id)
        resources = set(request.resource_ids)
        sources = set(request.sources) if request.sources is not None else None
        events = [
            e
            for e in scenario.events
            if e.resource_id in resources
            and request.window_start <= e.timestamp <= request.window_end
            and (sources is None or e.source in sources)
        ]
        return sorted(events, key=lambda e: (e.timestamp, e.event_id))

    def default_request(self, incident_id: str) -> CollectionRequest:
        """Every inventoried resource over the incident window (convenience for offline runs)."""
        s = self.loader.load(incident_id)
        return CollectionRequest(
            incident_id=incident_id,
            resource_ids=[r.resource_id for r in s.resources],
            window_start=s.incident.window_start,
            window_end=s.incident.window_end,
        )
