"""Investigation pipeline: anomalies -> onset -> window -> chains/candidate causes -> evidence.

The same code serves real and offline data: events come from any `Collector`, the graph from
inventory records (real discovery or the dataset). Ablation switches (`use_graph`,
`use_anomalies`, `use_chains`) remove a signal's context entirely, not only its weight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.anomaly.detectors import build_detector
from app.anomaly.series import series_from_events
from app.config import Settings, get_settings
from app.contracts.anomaly import Anomaly
from app.contracts.correlation import CorrelationResult
from app.contracts.events import CanonicalEvent, EventSource
from app.contracts.evidence import EvidenceRanking, ScoreWeights
from app.correlation.chains import CorrelationEngine
from app.correlation.temporal import estimate_onset, find_alarm
from app.correlation.windows import investigation_window
from app.evidence.ranker import EvidenceRanker, rank_by_recency
from app.evidence.semantic import SemanticScorer
from app.graph.builder import build_graph
from app.graph.store import NetworkXGraphStore
from app.interfaces.detector import Detector
from app.offline.models import IncidentRecord, RelationshipRecord, ResourceRecord


def evidence_detector(settings: Settings) -> tuple[Detector, float]:
    """The detector used for the anomaly component, with its effective threshold."""
    method = settings.evidence.anomaly_method
    anomaly = settings.anomaly
    params = getattr(anomaly, method, None)
    if params is None:
        raise ValueError(f"unknown anomaly method {method!r}")
    if settings.evidence.anomaly_threshold is not None:
        params = params.model_copy(update={"threshold": settings.evidence.anomaly_threshold})
        anomaly = anomaly.model_copy(update={method: params})
    return build_detector(method, anomaly), float(params.threshold)


def incident_onset(
    incident: IncidentRecord,
    events: list[CanonicalEvent],
    anomalies: list[Anomaly] | None,
    graph: NetworkXGraphStore | None,
    settings: Settings,
) -> datetime:
    """Onset estimate from anomaly runs on the alarmed resource and on every resource whose
    failure can reach it (graph); only the alarmed resource when there is no graph."""
    affected = set(incident.affected_resources)
    related = graph.impact_sources(affected) if graph is not None else affected
    return estimate_onset(
        incident.alarm_time,
        find_alarm(events, affected, incident.alarm_time),
        anomalies,
        events,
        related_resources=related,
        lookback_minutes=settings.correlation.onset_lookback_minutes,
        min_run_points=settings.correlation.min_anomaly_run_points,
    )


@dataclass
class Investigation:
    incident_id: str
    onset: datetime
    anomalies: list[Anomaly] | None
    graph: NetworkXGraphStore | None
    correlation: CorrelationResult | None
    ranking: EvidenceRanking


class InvestigationPipeline:
    def __init__(self, settings: Settings | None = None, semantic: SemanticScorer | None = None):
        self.settings = settings or get_settings()
        self.detector, threshold = evidence_detector(self.settings)
        self.ranker = EvidenceRanker(self.settings.evidence, semantic, anomaly_threshold=threshold)
        self.correlator = CorrelationEngine(self.settings.correlation)

    def detect(self, events: list[CanonicalEvent]) -> list[Anomaly]:
        metrics = [e for e in events if e.source == EventSource.CLOUDWATCH_METRIC]
        out: list[Anomaly] = []
        for series in series_from_events(metrics):
            out.extend(self.detector.detect(series))
        return sorted(out, key=lambda a: (a.timestamp, a.resource_id, a.metric))

    def run(
        self,
        *,
        incident: IncidentRecord,
        events: list[CanonicalEvent],
        resources: list[ResourceRecord],
        relationships: list[RelationshipRecord],
        window_minutes: int | None = None,
        top_k: int | None = None,
        weights: ScoreWeights | None = None,
        use_graph: bool = True,
        use_anomalies: bool = True,
        use_chains: bool = True,
        anomalies: list[Anomaly] | None = None,
        graph: NetworkXGraphStore | None = None,
        ranking_mode: str = "score",
    ) -> Investigation:
        """Anomaly detection runs on all supplied events (the detector needs a trailing
        baseline); only events inside the investigation window are ranked or chained.
        `ranking_mode="recency"` replaces the evidence score by the K most recent events
        (ablation A5)."""
        if ranking_mode not in ("score", "recency"):
            raise ValueError(f"unknown ranking_mode {ranking_mode!r}")
        st = self.settings
        minutes = window_minutes or st.evidence.window_minutes
        if use_anomalies:
            anomalies = anomalies if anomalies is not None else self.detect(events)
        else:
            anomalies = None
        if use_graph:
            graph = graph or build_graph(resources, relationships)
        else:
            graph = None
        onset = incident_onset(incident, events, anomalies, graph, st)
        correlation = None
        if use_chains:
            window = investigation_window(
                incident.alarm_time, minutes, st.evidence.post_alarm_minutes
            )
            correlation = self.correlator.correlate(
                incident_id=incident.incident_id,
                affected_resources=incident.affected_resources,
                events=events,
                anomalies=anomalies,
                window=window,
                onset=onset,
                graph=graph,
            )
        if ranking_mode == "recency":
            k = top_k or st.evidence.top_k
            ranking = EvidenceRanking(
                incident_id=incident.incident_id,
                window_minutes=minutes,
                onset_estimate=onset,
                top_k=k,
                weights=weights or st.evidence.weights,
                candidates=0,
                items=rank_by_recency(
                    incident.incident_id,
                    events,
                    incident.alarm_time,
                    minutes,
                    k,
                    st.evidence.post_alarm_minutes,
                ),
            )
            return Investigation(
                incident_id=incident.incident_id,
                onset=onset,
                anomalies=anomalies,
                graph=graph,
                correlation=correlation,
                ranking=ranking,
            )
        ranking = self.ranker.rank(
            incident_id=incident.incident_id,
            alarm_time=incident.alarm_time,
            affected_resources=incident.affected_resources,
            description=incident.description,
            events=events,
            onset=onset,
            anomalies=anomalies,
            graph=graph,
            known_resources={r.resource_id for r in resources},
            window_minutes=minutes,
            top_k=top_k,
            weights=weights,
        )
        return Investigation(
            incident_id=incident.incident_id,
            onset=onset,
            anomalies=anomalies,
            graph=graph,
            correlation=correlation,
            ranking=ranking,
        )

    def run_offline(self, collector, incident_id: str, **kwargs) -> Investigation:
        """Convenience for `ReplayCollector`: full incident window, dataset inventory."""
        incident = collector.incident(incident_id)
        resources, relationships = collector.inventory(incident_id)
        events = collector.collect(collector.default_request(incident_id))
        return self.run(
            incident=incident,
            events=events,
            resources=resources,
            relationships=relationships,
            **kwargs,
        )
