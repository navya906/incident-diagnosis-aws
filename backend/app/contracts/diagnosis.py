"""Structured diagnosis output schema. Invalid LLM output is repaired once, then rejected."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.contracts.taxonomy import RootCause

_EPS = 1e-6


class SeveritySuggestion(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RecommendationCategory(StrEnum):
    IMMEDIATE = "IMMEDIATE"
    INVESTIGATIVE = "INVESTIGATIVE"
    CORRECTIVE = "CORRECTIVE"
    PREVENTIVE = "PREVENTIVE"


class RootCauseClaim(BaseModel):
    taxonomy_label: RootCause
    description: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    resource_id: str
    label: Literal["INFERENCE"] = "INFERENCE"


class SupportingEvidence(BaseModel):
    evidence_id: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    label: Literal["FACT", "INFERENCE"]


class ContradictingEvidence(BaseModel):
    evidence_id: str = Field(min_length=1)
    explanation: str = Field(min_length=1)


class ContributingFactor(BaseModel):
    description: str = Field(min_length=1)
    label: Literal["FACT", "INFERENCE", "HYPOTHESIS"]


class AlternativeHypothesis(BaseModel):
    taxonomy_label: RootCause
    description: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    rejected_because: str = Field(min_length=1)
    label: Literal["HYPOTHESIS"] = "HYPOTHESIS"


class HistoricalInfluence(BaseModel):
    used: bool = False
    incident_ids: list[str] = Field(default_factory=list)
    how: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> HistoricalInfluence:
        if self.used and not self.incident_ids:
            raise ValueError("historical_influence.used=true requires incident_ids")
        if not self.used and self.incident_ids:
            raise ValueError("historical_influence.used=false must not list incident_ids")
        return self


class ImpactAnalysis(BaseModel):
    services: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    blast_radius: str = ""
    user_impact: str = ""


class Recommendation(BaseModel):
    category: RecommendationCategory
    action: str = Field(min_length=1)
    label: Literal["RECOMMENDATION"] = "RECOMMENDATION"


class Diagnosis(BaseModel):
    incident_summary: str = Field(min_length=1)
    root_cause: RootCauseClaim
    supporting_evidence: list[SupportingEvidence] = Field(default_factory=list)
    contradicting_evidence: list[ContradictingEvidence] = Field(default_factory=list)
    contributing_factors: list[ContributingFactor] = Field(default_factory=list)
    alternative_hypotheses: list[AlternativeHypothesis] = Field(default_factory=list)
    historical_influence: HistoricalInfluence = Field(default_factory=HistoricalInfluence)
    impact_analysis: ImpactAnalysis = Field(default_factory=ImpactAnalysis)
    severity_suggestion: SeveritySuggestion  # advisory only; the severity engine is authoritative
    recommendations: list[Recommendation] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    requires_human_review: bool

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Diagnosis:
        insufficient = self.root_cause.taxonomy_label == RootCause.INSUFFICIENT_EVIDENCE
        if not insufficient and not self.supporting_evidence:
            raise ValueError("a conclusion requires at least one supporting_evidence item")
        if insufficient and not self.requires_human_review:
            raise ValueError("insufficient_evidence requires requires_human_review=true")
        total = self.root_cause.confidence + sum(a.confidence for a in self.alternative_hypotheses)
        if total > 1 + _EPS:
            raise ValueError(f"root cause + alternative confidences sum to {total:.3f} > 1")
        confs = [a.confidence for a in self.alternative_hypotheses]
        if confs != sorted(confs, reverse=True):
            raise ValueError("alternative_hypotheses must be ranked by descending confidence")
        return self

    def cited_evidence_ids(self) -> set[str]:
        return {e.evidence_id for e in self.supporting_evidence} | {
            e.evidence_id for e in self.contradicting_evidence
        }


def needs_review(diagnosis: Diagnosis, confidence_threshold: float) -> bool:
    """Policy for requires_human_review: low confidence or insufficient evidence."""
    return (
        diagnosis.root_cause.confidence < confidence_threshold
        or diagnosis.root_cause.taxonomy_label == RootCause.INSUFFICIENT_EVIDENCE
    )
