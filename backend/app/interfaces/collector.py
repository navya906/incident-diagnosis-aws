from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.contracts.events import CanonicalEvent, EventSource


class CollectionRequest(BaseModel):
    """Collection is driven by an incident's affected resources and window, not global polling."""

    incident_id: str
    resource_ids: list[str] = Field(min_length=1)
    window_start: datetime
    window_end: datetime
    sources: list[EventSource] | None = None  # None = all sources the collector supports

    @model_validator(mode="after")
    def _window(self) -> "CollectionRequest":
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        return self


class Collector(ABC):
    """Emits CanonicalEvents. Real (boto3) and replay collectors must be schema-identical."""

    name: str

    @abstractmethod
    def collect(self, request: CollectionRequest) -> list[CanonicalEvent]:
        """Return events for the request, sorted by (timestamp, event_id)."""
