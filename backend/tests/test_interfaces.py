from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.interfaces import (
    CollectionRequest,
    Collector,
    Detector,
    Embedder,
    GraphStore,
    LLMClient,
    VectorStore,
)


@pytest.mark.parametrize("abc", [Collector, Detector, Embedder, GraphStore, LLMClient, VectorStore])
def test_interfaces_are_abstract(abc):
    with pytest.raises(TypeError):
        abc()


def test_collection_request_validates_window_and_resources():
    t = datetime(2025, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError):
        CollectionRequest(incident_id="i", resource_ids=["r"], window_start=t, window_end=t)
    with pytest.raises(ValidationError):
        CollectionRequest(
            incident_id="i", resource_ids=[], window_start=t, window_end=t + timedelta(hours=1)
        )
