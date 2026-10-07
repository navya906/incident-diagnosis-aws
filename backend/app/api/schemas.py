"""Request models: unknown fields are rejected, sizes are bounded, ids are canonical."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.lifecycle import Status
from app.contracts.events import CANONICAL_RESOURCE_ID, CanonicalEvent
from app.offline.models import RelationshipRecord, ResourceRecord

MAX_WINDOW = timedelta(hours=48)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IncidentCreate(_Strict):
    """Native JSON form of POST /api/incidents."""

    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=5000)
    alarm_time: datetime
    affected_resources: list[str] = Field(min_length=1, max_length=50)
    window_start: datetime | None = None
    window_end: datetime | None = None
    onset_at: datetime | None = None
    region: str = Field(default="us-east-1", pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    dedup_key: str | None = Field(default=None, max_length=300)

    @field_validator("affected_resources")
    @classmethod
    def _canonical(cls, v: list[str]) -> list[str]:
        bad = [r for r in v if not CANONICAL_RESOURCE_ID.match(r)]
        if bad:
            raise ValueError(f"non-canonical resource ids: {bad[:5]}")
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _window(self) -> IncidentCreate:
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("give both window_start and window_end, or neither")
        if self.window_start is not None:
            if self.window_end <= self.window_start:
                raise ValueError("window_end must be after window_start")
            if self.window_end - self.window_start > MAX_WINDOW:
                raise ValueError("window longer than 48 hours")
        return self


class TransitionIn(_Strict):
    to: Status
    note: str = Field(default="", max_length=2000)


class DiagnoseIn(_Strict):
    #: Conditions that make sense for a live incident (no ground truth needed).
    condition: Literal["Full", "A1", "A2", "A3", "A4", "A5", "B4"] = "Full"
    samples: int | None = Field(default=None, ge=0, le=10)


class EventsIn(_Strict):
    events: list[CanonicalEvent] = Field(min_length=1)


class InventoryIn(_Strict):
    resources: list[ResourceRecord] = Field(default_factory=list, max_length=500)
    relationships: list[RelationshipRecord] = Field(default_factory=list, max_length=2000)


class ImportOfflineIn(_Strict):
    offline_incident_id: str = Field(pattern=r"^inc-[0-9a-f]{10}$")
