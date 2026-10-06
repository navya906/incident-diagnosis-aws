"""Turn CanonicalEvents into clean MetricSeries (sorted, de-duplicated, resolution-aware)."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime

from app.contracts.anomaly import MetricPoint, MetricSeries
from app.contracts.events import CanonicalEvent, EventSource


def infer_period_seconds(timestamps: list[datetime], default: int = 60) -> int:
    """Smallest typical spacing between points (robust to gaps from missing data)."""
    if len(timestamps) < 2:
        return default
    gaps = sorted(
        int((b - a).total_seconds()) for a, b in zip(timestamps, timestamps[1:], strict=False)
    )
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return default
    # The lower quartile ignores the long gaps left by dropped points.
    return max(1, gaps[len(gaps) // 4])


def series_from_events(events: list[CanonicalEvent]) -> list[MetricSeries]:
    """Group metric datapoints by (resource, metric).

    Duplicate timestamps are averaged; NaN/inf values are dropped. The period comes from event
    metadata when present, otherwise it is inferred from the timestamps.
    """
    buckets: dict[tuple[str, str], dict[datetime, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    periods: dict[tuple[str, str], int] = {}
    for e in events:
        if e.source != EventSource.CLOUDWATCH_METRIC or e.metric is None or e.value is None:
            continue
        if not math.isfinite(e.value):
            continue
        key = (e.resource_id, e.metric)
        buckets[key][e.timestamp].append(e.value)
        period = e.metadata.get("period_seconds")
        if isinstance(period, int) and period > 0:
            periods[key] = period
    out = []
    for (rid, metric), by_ts in sorted(buckets.items()):
        ts = sorted(by_ts)
        points = [MetricPoint(timestamp=t, value=statistics.fmean(by_ts[t])) for t in ts]
        period = periods.get((rid, metric)) or infer_period_seconds(ts)
        out.append(
            MetricSeries(resource_id=rid, metric=metric, period_seconds=period, points=points)
        )
    return out
