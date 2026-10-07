"""Evidence ranking (BRIEF Section 2).

    score = w_t*temporal + w_r*resource + w_a*anomaly + w_s*semantic + w_d*dependency

Components, each in [0, 1]:

  temporal    exp decay of |t - onset| (separate time constants before/after the onset)
  resource    1 for an affected (alarmed) resource, `other_resource` for any other inventoried
              resource, 0 for a resource outside the inventory. Uses no graph.
  anomaly     metric datapoints: 0.5 + 0.5 * min(1, (score - thr) / max(|thr|, 1)) when the
              evidence detector flagged the point, else 0. Other events: the event-type prior
              (change, api_error, error/warning/info log, read_only), never 0 by construction.
  semantic    `SemanticScorer` similarity between the event text and the incident description
  dependency  1 when the event's resource is an affected resource or a failure of it can reach
              one through the impact graph (it can explain the symptoms); `DOWNSTREAM_EFFECT`
              when only the reverse holds (a symptom, not a cause); 0 when unconnected or when
              no graph is given. Hop distance is deliberately not used: causes sit 0 to 3 hops
              from the alarmed resource depending on the fault (DECISIONS D50).

Ablating a signal = weight 0. Alarm events are the incident trigger, not evidence candidates.
Selection keeps at most `max_per_group` items per redundancy group (DECISIONS D51).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.config import EvidenceSettings
from app.contracts.anomaly import Anomaly
from app.contracts.events import CanonicalEvent
from app.contracts.evidence import (
    EvidenceItem,
    EvidenceRanking,
    ScoreComponents,
    ScoreWeights,
    make_evidence_id,
)
from app.correlation.events import event_kind, event_text, group_key
from app.correlation.temporal import temporal_score
from app.correlation.windows import events_in_window, investigation_window
from app.evidence.semantic import LexicalSemanticScorer, SemanticScorer
from app.graph.store import NetworkXGraphStore

OTHER_RESOURCE = 0.25
DOWNSTREAM_EFFECT = 0.3


@dataclass(frozen=True)
class ScoredEvent:
    event: CanonicalEvent
    components: ScoreComponents
    score: float
    group: tuple[str, ...]


def _r(x: float) -> float:
    return round(min(1.0, max(0.0, x)), 6)


class EvidenceRanker:
    def __init__(
        self,
        settings: EvidenceSettings | None = None,
        semantic: SemanticScorer | None = None,
        anomaly_threshold: float = 3.0,
    ):
        self.settings = settings or EvidenceSettings()
        self.semantic = semantic or LexicalSemanticScorer()
        self.anomaly_threshold = anomaly_threshold

    # ------------------------------------------------------------------ components
    def _anomaly_component(self, e: CanonicalEvent, flagged: dict) -> float:
        kind = event_kind(e)
        if kind == "metric":
            a = flagged.get((e.resource_id, e.metric, e.timestamp))
            if a is None:
                return 0.0
            thr = self.anomaly_threshold
            return _r(0.5 + 0.5 * min(1.0, (a.score - thr) / max(abs(thr), 1.0)))
        return _r(self.settings.event_priors.get(kind, 0.0))

    @staticmethod
    def _dependency_component(
        rid: str, affected: set[str], graph: NetworkXGraphStore | None, cache: dict
    ) -> float:
        if graph is None:
            return 0.0
        if rid not in cache:
            if rid in affected or any(graph.impact_path(rid, a) for a in affected):
                cache[rid] = 1.0
            elif any(graph.impact_path(a, rid) for a in affected):
                cache[rid] = DOWNSTREAM_EFFECT
            else:
                cache[rid] = 0.0
        return cache[rid]

    # ------------------------------------------------------------------ scoring
    def score_events(
        self,
        *,
        candidates: list[CanonicalEvent],
        affected_resources: Iterable[str],
        description: str,
        onset: datetime,
        anomalies: Iterable[Anomaly] | None,
        graph: NetworkXGraphStore | None,
        known_resources: set[str] | None = None,
        weights: ScoreWeights | None = None,
    ) -> list[ScoredEvent]:
        st = self.settings
        w = weights or st.weights
        affected = set(affected_resources)
        flagged: dict[tuple[str, str | None, datetime], Anomaly] = {}
        for a in anomalies or ():
            k = (a.resource_id, a.metric, a.timestamp)
            if k not in flagged or a.score > flagged[k].score:
                flagged[k] = a
        sem = self.semantic.scores(description, [event_text(e) for e in candidates])
        dep_cache: dict[str, float] = {}
        out = []
        for e, s in zip(candidates, sem, strict=True):
            if e.resource_id in affected:
                res = 1.0
            elif known_resources is None or e.resource_id in known_resources:
                res = OTHER_RESOURCE
            else:
                res = 0.0
            c = ScoreComponents(
                temporal=_r(
                    temporal_score(
                        e.timestamp,
                        onset,
                        st.temporal_tau_before_minutes,
                        st.temporal_tau_after_minutes,
                    )
                ),
                resource=res,
                anomaly=self._anomaly_component(e, flagged),
                semantic=_r(s),
                dependency=self._dependency_component(e.resource_id, affected, graph, dep_cache),
            )
            out.append(ScoredEvent(e, c, round(w.combine(c), 6), group_key(e)))
        return out

    def select(self, scored: list[ScoredEvent], top_k: int) -> list[ScoredEvent]:
        """Top-K with redundancy control.

        Groups are ordered by their best score. From each group, the members scoring at least
        `group_member_ratio` x the group's best are eligible and the *earliest* `max_per_group`
        of them are taken: the first occurrences of a log pattern or of an anomalous stretch
        carry the information, later repeats add little (DECISIONS D51). With `max_per_group`
        0 this is a plain top-K by score. Ties: earlier first, then event id.
        """
        st = self.settings
        order = sorted(scored, key=lambda s: (-s.score, s.event.timestamp, s.event.event_id))
        if not st.max_per_group:
            return order[:top_k]
        groups: dict[tuple[str, ...], list[ScoredEvent]] = {}
        for s in order:
            groups.setdefault(s.group, []).append(s)
        chosen: list[ScoredEvent] = []
        for members in groups.values():
            floor = st.group_member_ratio * members[0].score
            eligible = sorted(
                (m for m in members if m.score >= floor),
                key=lambda s: (s.event.timestamp, s.event.event_id),
            )
            for m in eligible[: st.max_per_group]:
                chosen.append(m)
                if len(chosen) == top_k:
                    return chosen
        return chosen

    def rank(
        self,
        *,
        incident_id: str,
        alarm_time: datetime,
        affected_resources: list[str],
        description: str,
        events: list[CanonicalEvent],
        onset: datetime,
        anomalies: Iterable[Anomaly] | None,
        graph: NetworkXGraphStore | None,
        known_resources: set[str] | None = None,
        window_minutes: int | None = None,
        top_k: int | None = None,
        weights: ScoreWeights | None = None,
    ) -> EvidenceRanking:
        st = self.settings
        minutes = window_minutes or st.window_minutes
        k = top_k or st.top_k
        w = weights or st.weights
        window = investigation_window(alarm_time, minutes, st.post_alarm_minutes)
        candidates = [e for e in events_in_window(events, window) if event_kind(e) != "alarm"]
        scored = self.score_events(
            candidates=candidates,
            affected_resources=affected_resources,
            description=description,
            onset=onset,
            anomalies=anomalies,
            graph=graph,
            known_resources=known_resources,
            weights=w,
        )
        items = [
            EvidenceItem(
                evidence_id=make_evidence_id(incident_id, s.event.event_id),
                event_id=s.event.event_id,
                rank=i,
                score=s.score,
                components=s.components,
            )
            for i, s in enumerate(self.select(scored, k), start=1)
        ]
        return EvidenceRanking(
            incident_id=incident_id,
            window_minutes=minutes,
            onset_estimate=onset,
            top_k=k,
            weights=w,
            candidates=len(candidates),
            items=items,
        )


def rank_by_recency(
    incident_id: str,
    events: list[CanonicalEvent],
    alarm_time: datetime,
    window_minutes: int,
    top_k: int,
    post_alarm_minutes: int = 10,
) -> list[EvidenceItem]:
    """A5 baseline: the K most recent non-alarm events in the window, no scoring."""
    window = investigation_window(alarm_time, window_minutes, post_alarm_minutes)
    candidates = [e for e in events_in_window(events, window) if event_kind(e) != "alarm"]
    recent = sorted(candidates, key=lambda e: (e.timestamp, e.event_id), reverse=True)[:top_k]
    zero = ScoreComponents(temporal=0, resource=0, anomaly=0, semantic=0, dependency=0)
    return [
        EvidenceItem(
            evidence_id=make_evidence_id(incident_id, e.event_id),
            event_id=e.event_id,
            rank=i,
            score=0.0,
            components=zero,
        )
        for i, e in enumerate(recent, start=1)
    ]
