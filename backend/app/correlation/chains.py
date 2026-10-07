"""Signals, event chains and candidate causes.

1. Signals: mutating CloudTrail/Config changes, failed API calls, new WARNING+ log templates
   (first occurrence; templates already seen before the window are background and skipped), runs
   of at least `min_anomaly_run_points` flagged points per metric series, and the alarm.
2. Links: signal a -> b when a is not later than b, b follows within `max_link_gap_minutes`, and
   b's resource is a's resource or is reachable from it in the impact graph within
   `max_link_hops` (a failure of a's resource can reach b's resource).
3. Chains end at the anchor (the alarm signal; else the first anomaly run on an affected
   resource). Every signal with a link path to the anchor precedes the alarm; it is a candidate
   cause when its resource also has a dependency path to the affected resource. Candidates are
   ranked by kind strength x temporal proximity to the estimated onset, and each one gets the
   strongest chain from it to the anchor.

No step claims causality: the output is labelled "candidate cause" (DECISIONS D52).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

import networkx as nx

from app.config import CorrelationSettings
from app.contracts.anomaly import Anomaly
from app.contracts.correlation import (
    CandidateCause,
    CorrelationResult,
    EventChain,
    InvestigationWindow,
    Signal,
)
from app.contracts.events import CanonicalEvent, EventSource
from app.correlation.events import event_kind, message_template
from app.correlation.temporal import temporal_score
from app.graph.store import NetworkXGraphStore

#: Candidate causes decay quickly once they start after the onset (they look like effects).
CAUSE_TAU_BEFORE_MINUTES = 10.0
CAUSE_TAU_AFTER_MINUTES = 3.0


def _sid(*parts: object) -> str:
    return "sig_" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12]


def _fmt_time(t: datetime) -> str:
    return t.strftime("%H:%M:%S")


def extract_signals(
    events: Iterable[CanonicalEvent],
    anomalies: Iterable[Anomaly] | None,
    window: InvestigationWindow,
    min_anomaly_run_points: int = 2,
) -> list[Signal]:
    events = sorted(events, key=lambda e: (e.timestamp, e.event_id))
    out: list[Signal] = []

    # Changes, API errors, alarms: one signal per event.
    for e in events:
        if not window.contains(e.timestamp):
            continue
        kind = event_kind(e)
        if kind in ("change", "api_error", "alarm"):
            out.append(
                Signal(
                    signal_id=_sid(kind, e.event_id),
                    kind=kind,
                    resource_id=e.resource_id,
                    timestamp=e.timestamp,
                    event_ids=[e.event_id],
                    summary=f"{e.event_type}: {e.message}"[:300],
                )
            )

    # New WARNING+ log templates: first occurrence inside the window, unseen before it.
    groups: dict[tuple[str, str], list[CanonicalEvent]] = defaultdict(list)
    seen_before: set[tuple[str, str]] = set()
    for e in events:
        if e.source != EventSource.CLOUDWATCH_LOG:
            continue
        kind = event_kind(e)
        if kind not in ("error_log", "warning_log"):
            continue
        key = (e.resource_id, message_template(e.message))
        if e.timestamp < window.start:
            seen_before.add(key)
        elif window.contains(e.timestamp):
            groups[key].append(e)
    for (rid, template), evs in groups.items():
        if (rid, template) in seen_before:
            continue
        first = evs[0]
        out.append(
            Signal(
                signal_id=_sid("log", first.event_id),
                kind=event_kind(first),
                resource_id=rid,
                timestamp=first.timestamp,
                event_ids=[e.event_id for e in evs[:3]],
                summary=f"new log pattern ({len(evs)}x): {first.message}"[:300],
                count=len(evs),
            )
        )

    # Anomaly runs per metric series.
    if anomalies is not None:
        period: dict[tuple[str, str], int] = {}
        event_at: dict[tuple[str, str, datetime], str] = {}
        for e in events:
            if e.source == EventSource.CLOUDWATCH_METRIC and e.metric:
                k = (e.resource_id, e.metric)
                period.setdefault(k, int(e.metadata.get("period_seconds", 60)))
                event_at[(e.resource_id, e.metric, e.timestamp)] = e.event_id
        flagged: dict[tuple[str, str], dict[datetime, Anomaly]] = defaultdict(dict)
        for a in anomalies:
            if window.contains(a.timestamp):
                prev = flagged[(a.resource_id, a.metric)].get(a.timestamp)
                if prev is None or a.score > prev.score:
                    flagged[(a.resource_id, a.metric)][a.timestamp] = a
        for (rid, metric), by_time in flagged.items():
            gap = timedelta(seconds=2 * period.get((rid, metric), 60))
            runs: list[list[Anomaly]] = []
            for t in sorted(by_time):
                if runs and t - runs[-1][-1].timestamp <= gap:
                    runs[-1].append(by_time[t])
                else:
                    runs.append([by_time[t]])
            for run in runs:
                if len(run) < min_anomaly_run_points:
                    continue
                head = run[0]
                peak = max(run, key=lambda a: a.score)
                ids = [
                    event_at[(rid, metric, a.timestamp)]
                    for a in run[:2]
                    if (rid, metric, a.timestamp) in event_at
                ]
                out.append(
                    Signal(
                        signal_id=_sid("anomaly", rid, metric, head.timestamp.isoformat()),
                        kind="anomaly",
                        resource_id=rid,
                        timestamp=head.timestamp,
                        event_ids=ids,
                        summary=(
                            f"{metric} anomalous on {rid} from {_fmt_time(head.timestamp)} "
                            f"({len(run)} points, {head.method}; baseline {head.baseline:.4g}, "
                            f"peak observed {peak.observed:.4g})"
                        ),
                        count=len(run),
                    )
                )
    return sorted(out, key=lambda s: (s.timestamp, s.signal_id))


class CorrelationEngine:
    def __init__(self, settings: CorrelationSettings | None = None):
        self.settings = settings or CorrelationSettings()

    # ------------------------------------------------------------------ linking
    def _reaches(self, graph: NetworkXGraphStore | None, src: str, dst: str, cache: dict) -> bool:
        if src == dst:
            return True
        if graph is None:
            return False
        key = (src, dst)
        if key not in cache:
            d = graph.impact_distance(src, dst)
            cache[key] = d is not None and d <= self.settings.max_link_hops
        return cache[key]

    def link_graph(self, signals: list[Signal], graph: NetworkXGraphStore | None) -> nx.DiGraph:
        gap = timedelta(minutes=self.settings.max_link_gap_minutes)
        dag = nx.DiGraph()
        for s in signals:
            dag.add_node(s.signal_id)
        cache: dict = {}
        # `signals` is sorted by (timestamp, id), so i < j gives a DAG even for equal times.
        for i, a in enumerate(signals):
            if a.kind == "alarm":
                continue
            for b in signals[i + 1 :]:
                if b.timestamp - a.timestamp > gap:
                    break
                if self._reaches(graph, a.resource_id, b.resource_id, cache):
                    dag.add_edge(a.signal_id, b.signal_id)
        return dag

    # ------------------------------------------------------------------ main entry
    def correlate(
        self,
        *,
        incident_id: str,
        affected_resources: list[str],
        events: list[CanonicalEvent],
        anomalies: list[Anomaly] | None,
        window: InvestigationWindow,
        onset: datetime,
        graph: NetworkXGraphStore | None,
    ) -> CorrelationResult:
        st = self.settings
        signals = extract_signals(events, anomalies, window, st.min_anomaly_run_points)
        by_id = {s.signal_id: s for s in signals}
        affected = set(affected_resources)
        dag = self.link_graph(signals, graph)

        anchor = self._anchor(signals, affected, window.anchor)
        causes: list[CandidateCause] = []
        if anchor is not None:
            ancestors = nx.ancestors(dag, anchor.signal_id)
            best = self._best_paths(dag, ancestors | {anchor.signal_id}, anchor.signal_id, by_id)
            scored: list[tuple[float, Signal, list[str]]] = []
            for sid in ancestors:
                s = by_id[sid]
                dep = self._dependency_path(graph, s.resource_id, affected)
                if dep is None:
                    continue
                strength = st.signal_strength.get(s.kind, 0.0)
                score = strength * temporal_score(
                    s.timestamp, onset, CAUSE_TAU_BEFORE_MINUTES, CAUSE_TAU_AFTER_MINUTES
                )
                if score > 0:
                    scored.append((score, s, dep))
            scored.sort(key=lambda x: (-x[0], x[1].timestamp, x[1].signal_id))
            for score, s, dep in scored[: st.max_candidate_causes]:
                chain = self._chain(best[s.signal_id], by_id)
                lead = (anchor.timestamp - s.timestamp).total_seconds() / 60.0
                causes.append(
                    CandidateCause(
                        signal=s,
                        score=round(score, 6),
                        chain=chain,
                        dependency_path=dep,
                        rationale=(
                            f"{s.kind} on {s.resource_id} at {_fmt_time(s.timestamp)} precedes "
                            f"the alarm by {lead:.1f} min and links to it through "
                            f"{len(chain.signal_ids) - 1} later signal(s); dependency path: "
                            + " -> ".join(dep)
                        ),
                    )
                )
        chains: dict[str, EventChain] = {}
        for c in causes:
            chains.setdefault(c.chain.chain_id, c.chain)
        return CorrelationResult(
            incident_id=incident_id,
            window=window,
            onset_estimate=onset,
            affected_resources=sorted(affected),
            signals=signals,
            chains=list(chains.values()),
            candidate_causes=causes,
        )

    @staticmethod
    def _anchor(signals: list[Signal], affected: set[str], alarm_time: datetime) -> Signal | None:
        alarms = [s for s in signals if s.kind == "alarm" and s.resource_id in affected]
        if alarms:
            return min(
                alarms,
                key=lambda s: (abs((s.timestamp - alarm_time).total_seconds()), s.signal_id),
            )
        runs = [s for s in signals if s.kind == "anomaly" and s.resource_id in affected]
        return runs[0] if runs else None

    @staticmethod
    def _dependency_path(
        graph: NetworkXGraphStore | None, resource_id: str, affected: set[str]
    ) -> list[str] | None:
        if resource_id in affected:
            return [resource_id]
        if graph is None:
            return None
        paths = [p for a in sorted(affected) if (p := graph.impact_path(resource_id, a))]
        return min(paths, key=len) if paths else None

    def _best_paths(
        self, dag: nx.DiGraph, nodes: set[str], anchor: str, by_id: dict[str, Signal]
    ) -> dict[str, list[str]]:
        """For every node, the path to the anchor with the largest summed signal strength."""
        strength = self.settings.signal_strength
        sub = dag.subgraph(nodes)
        best: dict[str, tuple[float, list[str]]] = {anchor: (0.0, [anchor])}
        for n in reversed(list(nx.topological_sort(sub))):
            if n == anchor:
                continue
            options = [best[m] for m in sorted(sub.successors(n)) if m in best]
            if not options:
                continue
            value, path = max(options, key=lambda o: (o[0], -len(o[1])))
            best[n] = (value + strength.get(by_id[n].kind, 0.0), [n, *path])
        return {k: v[1] for k, v in best.items()}

    @staticmethod
    def _chain(path: list[str], by_id: dict[str, Signal]) -> EventChain:
        sigs = [by_id[s] for s in path]
        event_ids = list(dict.fromkeys(eid for s in sigs for eid in s.event_ids))
        return EventChain(
            chain_id="chain_" + hashlib.sha256("|".join(path).encode()).hexdigest()[:12],
            signal_ids=path,
            resource_path=[s.resource_id for s in sigs],
            event_ids=event_ids,
            start=sigs[0].timestamp,
            end=sigs[-1].timestamp,
        )
