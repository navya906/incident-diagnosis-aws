"""Minimal DB models. JSON columns keep the schema portable (PostgreSQL and SQLite for tests).

The pgvector embedding column is added in the Phase 5 migration, together with the VectorStore.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="DETECTED")
    scenario_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    affected_resources: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Event(Base):
    __tablename__ = "events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incidents.id"), nullable=True, index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(32))
    service: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(300), index=True)
    event_type: Mapped[str] = mapped_column(String(128))
    metric: Mapped[str | None] = mapped_column(String(128), nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)


class AwsResource(Base):
    __tablename__ = "aws_resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incidents.id"), nullable=True, index=True
    )
    resource_id: Mapped[str] = mapped_column(String(300), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    service: Mapped[str] = mapped_column(String(64))
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ResourceRelationship(Base):
    __tablename__ = "resource_relationships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incidents.id"), nullable=True, index=True
    )
    source_id: Mapped[str] = mapped_column(String(300), index=True)
    target_id: Mapped[str] = mapped_column(String(300), index=True)
    relation_type: Mapped[str] = mapped_column(String(64))


class AnomalyRecord(Base):
    __tablename__ = "anomalies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    resource_id: Mapped[str] = mapped_column(String(300))
    metric: Mapped[str] = mapped_column(String(128))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    score: Mapped[float] = mapped_column(Float)
    baseline: Mapped[float] = mapped_column(Float)
    observed: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(64))


class EvidenceRecord(Base):
    __tablename__ = "evidence"

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    event_id: Mapped[str] = mapped_column(String(64))
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    components: Mapped[dict[str, Any]] = mapped_column(JSON)


class DiagnosisRecord(Base):
    __tablename__ = "diagnoses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    experiment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    condition: Mapped[str] = mapped_column(String(32), default="Full")
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    valid: Mapped[bool] = mapped_column(Boolean, default=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HistoricalIncident(Base):
    __tablename__ = "historical_incidents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    corpus_version: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    taxonomy_label: Mapped[str] = mapped_column(String(64))
    resolution: Mapped[str] = mapped_column(Text, default="")
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"

    experiment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    split: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(64), default="smoke-test / synthetic")
    dataset_version: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExperimentResult(Base):
    __tablename__ = "experiment_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_runs.experiment_id"), index=True
    )
    incident_id: Mapped[str] = mapped_column(String(64), index=True)
    condition: Mapped[str] = mapped_column(String(32))
    run_index: Mapped[int] = mapped_column(Integer, default=0)
    retrieved_evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    diagnosis: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ground_truth: Mapped[dict[str, Any]] = mapped_column(JSON)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
