"""Evidence items and scoring configuration contracts."""

import hashlib
from datetime import datetime

from pydantic import BaseModel, Field


class ScoreComponents(BaseModel):
    temporal: float = Field(ge=0, le=1)
    resource: float = Field(ge=0, le=1)
    anomaly: float = Field(ge=0, le=1)
    semantic: float = Field(ge=0, le=1)
    dependency: float = Field(ge=0, le=1)


class ScoreWeights(BaseModel):
    """score = w_t*temporal + w_r*resource + w_a*anomaly + w_s*semantic + w_d*dependency.

    Ablating a signal means setting its weight to 0.
    """

    temporal: float = Field(default=0.25, ge=0)
    resource: float = Field(default=0.20, ge=0)
    anomaly: float = Field(default=0.25, ge=0)
    semantic: float = Field(default=0.15, ge=0)
    dependency: float = Field(default=0.15, ge=0)

    def combine(self, c: ScoreComponents) -> float:
        return (
            self.temporal * c.temporal
            + self.resource * c.resource
            + self.anomaly * c.anomaly
            + self.semantic * c.semantic
            + self.dependency * c.dependency
        )


class EvidenceItem(BaseModel):
    evidence_id: str
    event_id: str
    rank: int = Field(ge=1)
    score: float
    components: ScoreComponents


def make_evidence_id(incident_id: str, event_id: str) -> str:
    """Stable evidence id: depends only on the incident and the event, never on rank, K or
    weights, so citations stay valid across re-rankings and ablations."""
    return "evd_" + hashlib.sha256(f"{incident_id}|{event_id}".encode()).hexdigest()[:16]


class EvidenceRanking(BaseModel):
    incident_id: str
    window_minutes: int
    onset_estimate: datetime
    top_k: int
    weights: ScoreWeights
    candidates: int  # events in the window that were scored
    items: list[EvidenceItem]
