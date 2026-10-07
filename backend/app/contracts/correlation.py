"""Correlation outputs: signals, event chains and candidate causes.

"Candidate cause" means: an observed signal that precedes the incident's symptoms (temporal
precedence) on a resource with a dependency path to the affected resource. It is a hypothesis
for the diagnosis step, not a causal claim.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SignalKind = Literal["change", "api_error", "error_log", "warning_log", "anomaly", "alarm"]


class Signal(BaseModel):
    """A noteworthy occurrence: a change, an API error, a new log template, an anomaly run."""

    signal_id: str
    kind: SignalKind
    resource_id: str
    timestamp: datetime
    event_ids: list[str] = Field(default_factory=list)
    summary: str
    count: int = Field(default=1, ge=1)


class InvestigationWindow(BaseModel):
    anchor: datetime  # alarm time
    minutes_before: int = Field(ge=0)
    minutes_after: int = Field(ge=0)
    start: datetime
    end: datetime

    def contains(self, t: datetime) -> bool:
        return self.start <= t <= self.end


class EventChain(BaseModel):
    """Signals ordered in time, each linked to the next by the same resource or a dependency."""

    chain_id: str
    signal_ids: list[str]
    resource_path: list[str]  # resource of each signal, in chain order
    event_ids: list[str]
    start: datetime
    end: datetime


class CandidateCause(BaseModel):
    signal: Signal
    score: float = Field(ge=0)
    chain: EventChain
    #: Path along which a failure of the signal's resource reaches the affected resource.
    dependency_path: list[str]
    rationale: str
    label: Literal["candidate cause"] = "candidate cause"


class CorrelationResult(BaseModel):
    incident_id: str
    window: InvestigationWindow
    onset_estimate: datetime
    affected_resources: list[str]
    signals: list[Signal]
    chains: list[EventChain]
    candidate_causes: list[CandidateCause]
