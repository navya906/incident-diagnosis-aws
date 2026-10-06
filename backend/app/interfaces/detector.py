from abc import ABC, abstractmethod

from app.contracts.anomaly import Anomaly, MetricSeries


class Detector(ABC):
    """Statistical anomaly detector. Must not involve any LLM."""

    name: str

    @abstractmethod
    def detect(self, series: MetricSeries) -> list[Anomaly]:
        """Return anomalies found in a single metric series."""
