"""Offline dataset records.

Each incident is stored as two files so ground truth cannot leak into what the system sees:
``<id>.json`` (observable: incident, inventory, events) and ``<id>.truth.json`` (ground truth,
anomaly labels, generator metadata). Only the observable file is read by ``ReplayCollector``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.contracts.events import CanonicalEvent
from app.contracts.ground_truth import GroundTruth

Category = Literal["standard", "red_herring", "insufficient_evidence", "compound"]
Split = Literal["dev", "test"]


class ResourceRecord(BaseModel):
    resource_id: str
    resource_type: str
    service: str
    role: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class RelationshipRecord(BaseModel):
    """Edge from dependent (source) to dependency (target)."""

    source_id: str
    target_id: str
    relation_type: str


class IncidentRecord(BaseModel):
    incident_id: str
    title: str
    description: str  # blinded: symptoms only
    alarm_time: datetime
    window_start: datetime
    window_end: datetime
    affected_resources: list[str]
    region: str = "us-east-1"


class ObservableScenario(BaseModel):
    incident: IncidentRecord
    resources: list[ResourceRecord]
    relationships: list[RelationshipRecord]
    events: list[CanonicalEvent]


class AnomalyLabel(BaseModel):
    """An injected-fault window on one metric series (labels for detector evaluation)."""

    resource_id: str
    metric: str
    start: datetime
    end: datetime


class InjectedFault(BaseModel):
    fault: str
    onset: datetime
    primary_resource_id: str
    trigger_visible: bool


class ScenarioMeta(BaseModel):
    scenario_key: str
    category: Category
    split: Split
    topology: str
    faults: list[InjectedFault]
    severity: float
    noise: float
    period_seconds: int
    missing_rate: float
    degradation: str | None = None
    red_herring_event_ids: list[str] = Field(default_factory=list)
    label: str = "smoke-test / synthetic"


class TruthRecord(BaseModel):
    incident_id: str
    ground_truth: GroundTruth
    anomaly_labels: list[AnomalyLabel]
    meta: ScenarioMeta


class ManifestEntry(BaseModel):
    incident_id: str
    split: Split
    category: Category
    fault_type: str
    topology: str
    period_seconds: int
    sha256_observable: str
    sha256_truth: str


class Manifest(BaseModel):
    dataset_version: str
    generator_version: str
    seed: int
    label: str = "smoke-test / synthetic"
    counts: dict[str, int]
    content_sha256: str
    entries: list[ManifestEntry]
