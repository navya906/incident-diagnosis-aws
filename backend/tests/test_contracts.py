from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.contracts import (
    FAULT_TYPES,
    CanonicalEvent,
    EventSource,
    GroundTruth,
    RootCause,
    ScoreComponents,
    ScoreWeights,
)

T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def _ev(**kw):
    base = dict(
        timestamp=T0,
        source=EventSource.CLOUDWATCH_METRIC,
        service="rds",
        resource_id="db-1",
        event_type="metric_datapoint",
        metric="CPUUtilization",
        value=42.0,
    )
    base.update(kw)
    return CanonicalEvent.build(**base)


def test_taxonomy_is_the_closed_twelve_label_set():
    assert len(RootCause) == 12
    assert len(FAULT_TYPES) == 10
    assert RootCause.OTHER not in FAULT_TYPES
    assert RootCause.INSUFFICIENT_EVIDENCE not in FAULT_TYPES


def test_event_id_is_deterministic_and_sensitive():
    assert _ev().event_id == _ev().event_id
    assert _ev().event_id != _ev(resource_id="db-2").event_id
    assert _ev().event_id != _ev(timestamp=T0 + timedelta(seconds=1)).event_id


def test_event_id_independent_of_timezone_representation():
    ist = timezone(timedelta(hours=5, minutes=30))
    assert _ev(timestamp=T0).event_id == _ev(timestamp=T0.astimezone(ist)).event_id


def test_naive_timestamp_treated_as_utc():
    ev = _ev(timestamp=datetime(2025, 1, 1, 12, 0))
    assert ev.timestamp.tzinfo is not None
    assert ev.event_id == _ev().event_id


def test_event_is_frozen_and_validates_required_fields():
    ev = _ev()
    with pytest.raises(ValidationError):
        ev.value = 1.0
    with pytest.raises(ValidationError):
        CanonicalEvent(
            event_id="x", timestamp=T0, source="nope", service="s", resource_id="r", event_type="t"
        )


def test_ground_truth_rejects_label_outside_taxonomy():
    kwargs = dict(primary_resource_id="r", root_cause_text="t", onset_time=T0, resolution="x")
    GroundTruth(taxonomy_label="cpu_saturation", **kwargs)
    with pytest.raises(ValidationError):
        GroundTruth(taxonomy_label="gremlins", **kwargs)


def test_score_weights_combine_and_ablate():
    c = ScoreComponents(temporal=1, resource=1, anomaly=0, semantic=0.5, dependency=0)
    assert ScoreWeights().combine(c) == pytest.approx(0.25 + 0.20 + 0.075)
    assert ScoreWeights(temporal=0).combine(c) == pytest.approx(0.20 + 0.075)


def test_score_components_bounded():
    with pytest.raises(ValidationError):
        ScoreComponents(temporal=1.5, resource=0, anomaly=0, semantic=0, dependency=0)
