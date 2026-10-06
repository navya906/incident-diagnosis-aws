"""Run the configured detectors over an incident's events."""

from __future__ import annotations

from app.anomaly.detectors import build_detectors
from app.anomaly.series import series_from_events
from app.config import AnomalySettings
from app.contracts.anomaly import Anomaly
from app.contracts.events import CanonicalEvent
from app.interfaces.detector import Detector


class AnomalyService:
    def __init__(
        self, settings: AnomalySettings | None = None, detectors: list[Detector] | None = None
    ):
        self.detectors = detectors or build_detectors(settings or AnomalySettings())

    def detect(self, events: list[CanonicalEvent]) -> list[Anomaly]:
        """All anomalies from all configured methods, ordered by time, resource, metric, method."""
        out: list[Anomaly] = []
        for series in series_from_events(events):
            for d in self.detectors:
                out.extend(d.detect(series))
        return sorted(out, key=lambda a: (a.timestamp, a.resource_id, a.metric, a.method))
