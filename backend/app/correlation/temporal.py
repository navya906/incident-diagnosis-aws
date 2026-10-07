"""Onset estimation and temporal proximity scoring."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime, timedelta

from app.contracts.anomaly import Anomaly
from app.contracts.events import CanonicalEvent, EventSource


def find_alarm(
    events: Iterable[CanonicalEvent], affected_resources: Iterable[str], alarm_time: datetime
) -> CanonicalEvent | None:
    """The alarm event on an affected resource closest to the incident's alarm time."""
    affected = set(affected_resources)
    alarms = [e for e in events if e.source == EventSource.ALARM and e.resource_id in affected]
    if not alarms:
        return None
    return min(alarms, key=lambda e: (abs((e.timestamp - alarm_time).total_seconds()), e.event_id))


def _period_seconds(events: Iterable[CanonicalEvent], resource_id: str, metric: str) -> int:
    for e in events:
        if (
            e.source == EventSource.CLOUDWATCH_METRIC
            and e.resource_id == resource_id
            and e.metric == metric
        ):
            return int(e.metadata.get("period_seconds", 60))
    return 60


def estimate_onset(
    alarm_time: datetime,
    alarm: CanonicalEvent | None,
    anomalies: Iterable[Anomaly] | None,
    events: Iterable[CanonicalEvent] = (),
    related_resources: Iterable[str] | None = None,
    lookback_minutes: float = 15.0,
    min_run_points: int = 2,
) -> datetime:
    """Earliest start of an anomaly run that is still active at the alarm.

    A run is a stretch of flagged points on one series, allowing one missing or unflagged point
    between flags; it is active at the alarm when its last flag is at most two periods before
    the alarm. Candidate series: the alarmed metric (any run length) and every metric of
    `related_resources` (runs of at least `min_run_points`), typically the resources whose
    failure can reach the alarmed resource. Runs starting more than `lookback_minutes` before
    the alarm are ignored. Without anomalies or any active run, returns the alarm time (the
    A3 "no anomaly detection" ablation lands here). See DECISIONS D54.
    """
    if anomalies is None:
        return alarm_time
    events = list(events)
    related = set(related_resources or ())
    by_series: dict[tuple[str, str], set[datetime]] = {}
    for a in anomalies:
        is_alarm_series = alarm is not None and (a.resource_id, a.metric) == (
            alarm.resource_id,
            alarm.metric,
        )
        if is_alarm_series or a.resource_id in related:
            by_series.setdefault((a.resource_id, a.metric), set()).add(a.timestamp)
    earliest = alarm_time - timedelta(minutes=lookback_minutes)
    starts = []
    for (rid, metric), times in by_series.items():
        period = timedelta(seconds=_period_seconds(events, rid, metric))
        flagged = sorted(t for t in times if t <= alarm_time + period)
        if not flagged or alarm_time - flagged[-1] > 2 * period:
            continue
        run = [flagged[-1]]
        for t in reversed(flagged[:-1]):
            if run[-1] - t > 2 * period:
                break
            run.append(t)
        is_alarm_series = alarm is not None and (rid, metric) == (alarm.resource_id, alarm.metric)
        if (is_alarm_series or len(run) >= min_run_points) and run[-1] >= earliest:
            starts.append(run[-1])
    return min(starts) if starts else alarm_time


def temporal_score(
    t: datetime, onset: datetime, tau_before_minutes: float, tau_after_minutes: float
) -> float:
    """exp(-|t - onset| / tau), with separate time constants before and after the onset.

    Causes tend to sit just before the onset and effects just after; both decay with distance.
    """
    dt = (t - onset).total_seconds() / 60.0
    tau = tau_before_minutes if dt < 0 else tau_after_minutes
    return math.exp(-abs(dt) / tau)


def rank_by_temporal_proximity(
    events: Iterable[CanonicalEvent],
    onset: datetime,
    tau_before_minutes: float = 5.0,
    tau_after_minutes: float = 10.0,
) -> list[tuple[CanonicalEvent, float]]:
    """Events ordered by temporal proximity to the onset (ties: time, then event id)."""
    scored = [
        (e, temporal_score(e.timestamp, onset, tau_before_minutes, tau_after_minutes))
        for e in events
    ]
    return sorted(scored, key=lambda p: (-p[1], p[0].timestamp, p[0].event_id))
