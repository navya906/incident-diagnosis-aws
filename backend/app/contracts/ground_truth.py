"""Ground truth attached to every evaluation incident."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.contracts.taxonomy import RootCause


class GroundTruth(BaseModel):
    taxonomy_label: RootCause
    primary_resource_id: str
    root_cause_text: str
    onset_time: datetime
    evidence_event_ids: list[str] = Field(default_factory=list)
    resolution: str
    #: Compound/concurrent-fault cases list every additional injected fault.
    secondary_labels: list[RootCause] = Field(default_factory=list)
