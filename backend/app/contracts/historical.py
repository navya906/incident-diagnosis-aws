"""Historical-incident knowledge base records and retrieval results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.contracts.taxonomy import RootCause


class HistoricalRecord(BaseModel):
    """A past, resolved incident. `search_text` holds only what was observable (symptoms and key
    signals) and is what gets embedded; `summary` adds the post-incident root cause and fix."""

    incident_id: str
    corpus_version: str
    source: Literal["historical_corpus", "dataset_dev", "real_capture"]
    title: str
    search_text: str
    summary: str
    taxonomy_label: RootCause
    root_cause_text: str
    resolution: str
    alarm_metric: str | None = None
    signals: list[str] = Field(default_factory=list)
    resource_types: list[str] = Field(default_factory=list)
    #: Incident id, evidence event ids and a description hash; used by the leakage guard.
    fingerprints: list[str] = Field(default_factory=list)
    label: str = "smoke-test / synthetic"


class RetrievedIncident(BaseModel):
    incident_id: str
    rank: int = Field(ge=1)
    score: float
    similarity: float
    signal_overlap: float = Field(ge=0, le=1)
    context_match: float = Field(ge=0, le=1)
    taxonomy_label: RootCause
    title: str
    summary: str
    root_cause_text: str
    resolution: str
    #: Past incidents are context, never evidence for the current incident.
    label: Literal["HISTORICAL"] = "HISTORICAL"
