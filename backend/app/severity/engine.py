"""Deterministic severity engine. The LLM's `severity_suggestion` is advisory only.

Factors (each measured from telemetry inside [onset, window end], DECISIONS D76):

  affected_resources  affected (alarmed) resources plus resources with >= 2 anomalous points
                      after the onset; points from `resources_bounds`
  error_rate          peak of 5XX / RequestCount (ALB) and Errors / Invocations (Lambda)
  duration            onset -> last anomalous point on an affected resource (or the alarm)
  availability        peak capacity loss: ECS running tasks vs pre-onset baseline, ALB unhealthy
                      hosts vs routed capacity, Lambda throttle rate, EC2 status-check failures
  latency             peak / pre-onset median of ALB TargetResponseTime, Lambda Duration or SQS
                      ApproximateAgeOfOldestMessage (an addition to the brief's list: slow
                      requests hurt users without failing)
  criticality         static business criticality from config (first matching rule per
                      resource, highest wins): LOW -1, MEDIUM 0, HIGH +1, CRITICAL +2

points(value) = number of bounds the value exceeds. total = sum of points + criticality offset;
level by `level_bounds`; error rate or availability loss >= `major_outage_fraction` forces at
least HIGH. A factor without data scores 0 and is reported as unknown.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import datetime

from pydantic import BaseModel

from app.config import SeveritySettings
from app.contracts.anomaly import Anomaly
from app.contracts.diagnosis import SeveritySuggestion
from app.contracts.events import CanonicalEvent, EventSource
from app.graph.store import NetworkXGraphStore
from app.offline.models import IncidentRecord, ResourceRecord

LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
CRITICALITY_OFFSET = {"LOW": -1, "MEDIUM": 0, "HIGH": 1, "CRITICAL": 2}
METHOD = "deterministic-v1"


class SeverityFactor(BaseModel):
    name: str
    value: float | None
    unit: str
    points: int
    known: bool
    detail: str


class SeverityAssessment(BaseModel):
    level: SeveritySuggestion
    score: int
    factors: list[SeverityFactor]
    criticality: str
    affected_resources: list[str]
    affected_services: list[str]
    data_complete: bool
    rationale: str
    method: str = METHOD
    label: str = "deterministic"

    def factor(self, name: str) -> SeverityFactor:
        return next(f for f in self.factors if f.name == name)


def _points(value: float | None, bounds: list[float]) -> int:
    return 0 if value is None else sum(1 for b in bounds if value > b)


class SeverityEngine:
    def __init__(self, settings: SeveritySettings | None = None):
        self.settings = settings or SeveritySettings()
        self._rules = [(re.compile(r.match), r.level) for r in self.settings.criticality_rules]

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _series(events, onset: datetime, end: datetime):
        """{(resource, metric): {timestamp: value}} split into before-onset and impact."""
        before, during = defaultdict(dict), defaultdict(dict)
        for e in events:
            if e.source != EventSource.CLOUDWATCH_METRIC or e.value is None:
                continue
            target = before if e.timestamp < onset else during if e.timestamp <= end else None
            if target is not None:
                target[(e.resource_id, e.metric)][e.timestamp] = e.value
        return before, during

    def criticality(self, resources: list[str]) -> str:
        best = None
        for rid in resources:
            service = rid.split("/", 1)[0]
            for pattern, level in self._rules:
                if pattern.search(rid) or pattern.search(service):
                    if best is None or LEVELS.index(level) > LEVELS.index(best):
                        best = level
                    break
        return best or self.settings.default_criticality

    # ------------------------------------------------------------------ assess
    def assess(
        self,
        *,
        incident: IncidentRecord,
        events: list[CanonicalEvent],
        onset: datetime,
        anomalies: list[Anomaly] | None,
        resources: list[ResourceRecord] | None = None,
        graph: NetworkXGraphStore | None = None,
    ) -> SeverityAssessment:
        st = self.settings
        end = incident.window_end
        before, during = self._series(events, onset, end)
        attrs = {r.resource_id: r.attributes for r in resources or []}

        # affected resources and duration ---------------------------------
        flagged = defaultdict(list)
        for a in anomalies or ():
            if onset <= a.timestamp <= end:
                flagged[a.resource_id].append(a.timestamp)
        affected = set(incident.affected_resources)
        affected |= {r for r, ts in flagged.items() if len(set(ts)) >= 2}
        services = sorted({r.split("/", 1)[0] for r in affected})
        last = max(
            (t for r in incident.affected_resources for t in flagged.get(r, [])),
            default=incident.alarm_time,
        )
        duration = max(0.0, (max(last, incident.alarm_time) - onset).total_seconds() / 60)

        # error rate ------------------------------------------------------
        rates = []
        for (rid, metric), req in during.items():
            if metric == "RequestCount":
                errs = [
                    during.get((rid, m), {})
                    for m in ("HTTPCode_Target_5XX_Count", "HTTPCode_ELB_5XX_Count")
                ]
                for t, n in req.items():
                    if n > 0:
                        rates.append(min(1.0, sum(s.get(t, 0.0) for s in errs) / n))
            elif metric == "Invocations":
                errs = during.get((rid, "Errors"), {})
                rates += [min(1.0, errs.get(t, 0.0) / n) for t, n in req.items() if n > 0]
        error_rate = max(rates) if rates else None

        # availability ----------------------------------------------------
        losses = []
        baseline_tasks: dict[str, float] = {}
        for (rid, metric), vals in before.items():
            if metric == "RunningTaskCount" and vals:
                baseline_tasks[rid] = statistics.median(vals.values())
        for rid, base in list(baseline_tasks.items()):
            base = float(attrs.get(rid, {}).get("desired_count", base) or base)
            baseline_tasks[rid] = base
            now = during.get((rid, "RunningTaskCount"), {})
            if now and base > 0:
                losses.append(max(0.0, 1 - min(now.values()) / base))
        for (rid, metric), vals in during.items():
            if metric == "UnHealthyHostCount" and vals:
                capacity = 0.0
                if graph is not None and graph.has_node(rid):
                    capacity = sum(baseline_tasks.get(t, 0.0) for t in graph.downstream(rid, 1))
                capacity = capacity or 1.0
                losses.append(min(1.0, max(vals.values()) / capacity))
            elif metric == "Throttles":
                inv = during.get((rid, "Invocations"), {})
                losses += [min(1.0, n / inv[t]) for t, n in vals.items() if inv.get(t, 0) > 0]
            elif metric == "StatusCheckFailed" and vals:
                losses.append(1.0 if max(vals.values()) > 0 else 0.0)
        availability_loss = max(losses) if losses else None

        # latency ---------------------------------------------------------
        ratios = []
        for metric in ("TargetResponseTime", "Duration", "ApproximateAgeOfOldestMessage"):
            for (rid, m), vals in during.items():
                base_vals = before.get((rid, m), {})
                if m == metric and vals and base_vals:
                    base = statistics.median(base_vals.values())
                    if base > 0:
                        ratios.append(max(vals.values()) / base)
        latency_ratio = max(ratios) if ratios else None

        crit = self.criticality(sorted(affected))
        factors = [
            SeverityFactor(
                name="affected_resources",
                value=float(len(affected)),
                unit="resources",
                points=max(
                    _points(len(affected), st.resources_bounds), 3 if len(services) >= 3 else 0
                ),
                known=True,
                detail=f"{len(affected)} resource(s) across {len(services)} service(s): "
                + ", ".join(sorted(affected)),
            ),
            SeverityFactor(
                name="error_rate",
                value=None if error_rate is None else round(error_rate, 4),
                unit="fraction",
                points=_points(error_rate, st.error_rate_bounds),
                known=error_rate is not None,
                detail="peak 5XX/requests or errors/invocations"
                if rates
                else "no request or invocation metrics",
            ),
            SeverityFactor(
                name="duration",
                value=round(duration, 1),
                unit="minutes",
                points=_points(duration, st.duration_minutes_bounds),
                known=True,
                detail=f"onset {onset:%H:%M} to {max(last, incident.alarm_time):%H:%M}",
            ),
            SeverityFactor(
                name="availability",
                value=None if availability_loss is None else round(availability_loss, 4),
                unit="fraction of capacity lost",
                points=_points(availability_loss, st.availability_loss_bounds),
                known=availability_loss is not None,
                detail="peak task/host/throttle/status-check loss"
                if losses
                else "no capacity or health metrics",
            ),
            SeverityFactor(
                name="latency",
                value=None if latency_ratio is None else round(latency_ratio, 2),
                unit="x pre-onset median",
                points=min(2, _points(latency_ratio, st.latency_ratio_bounds)),
                known=latency_ratio is not None,
                detail="peak latency / baseline" if ratios else "no latency metrics",
            ),
        ]
        score = sum(f.points for f in factors) + CRITICALITY_OFFSET[crit]
        idx = next((i for i, b in enumerate(st.level_bounds) if score <= b), len(LEVELS) - 1)
        reasons = [f"score {score} ({crit} criticality)"]
        major = max(error_rate or 0.0, availability_loss or 0.0)
        if major >= st.major_outage_fraction and idx < 2:
            idx = 2
            reasons.append(f"major outage ({major:.0%} >= {st.major_outage_fraction:.0%})")
        known = [f for f in factors if f.known]
        return SeverityAssessment(
            level=SeveritySuggestion(LEVELS[idx]),
            score=score,
            factors=factors,
            criticality=crit,
            affected_resources=sorted(affected),
            affected_services=services,
            data_complete=len(known) == len(factors),
            rationale="; ".join(
                reasons + [f"{f.name}={f.value} {f.unit} -> {f.points}" for f in factors]
            ),
        )
