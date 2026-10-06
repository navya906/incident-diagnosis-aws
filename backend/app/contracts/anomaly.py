"""Anomaly and metric-series contracts shared by detectors and correlation."""

from datetime import datetime

from pydantic import BaseModel, Field


class MetricPoint(BaseModel):
    timestamp: datetime
    value: float


class MetricSeries(BaseModel):
    resource_id: str
    metric: str
    period_seconds: int = Field(gt=0)
    points: list[MetricPoint]


class Anomaly(BaseModel):
    resource_id: str
    metric: str
    timestamp: datetime
    score: float
    baseline: float
    observed: float
    method: str
