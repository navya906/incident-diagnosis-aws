"""Historical knowledge base: leakage-guarded indexing, similarity retrieval, re-ranking.

Retrieval = top-`candidates` nearest neighbours by embedding cosine, then re-ranked by

    score = w_sim * similarity + w_sig * signal_overlap + w_ctx * context_match

  similarity      cosine between the query text and the record's `search_text`
  signal_overlap  Jaccard of signal vocabularies (anomalous metric names, API calls by kind,
                  log templates)
  context_match   0.5 * same alarm metric + 0.5 * Jaccard of resource (node) types

and the top-k are returned (DECISIONS D63).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.config import RagSettings
from app.contracts.events import CanonicalEvent, EventSource
from app.contracts.historical import HistoricalRecord, RetrievedIncident
from app.correlation.events import event_kind, message_template
from app.graph.store import NetworkXGraphStore, node_type_for
from app.interfaces.embedder import Embedder
from app.interfaces.vector_store import VectorStore
from app.offline.models import IncidentRecord, ResourceRecord


class LeakageError(RuntimeError):
    """A record that identifies a held-out (test) incident was about to be indexed."""


@dataclass
class RetrievalQuery:
    text: str
    signals: set[str] = field(default_factory=set)
    alarm_metric: str | None = None
    resource_types: set[str] = field(default_factory=set)
    incident_id: str | None = None  # excluded from results (leave-one-out)


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 0.0


def event_signal(e: CanonicalEvent) -> str | None:
    """Signal vocabulary entry for an event (same scheme as `record_from_scenario`)."""
    if e.source == EventSource.CLOUDWATCH_METRIC:
        return f"metric:{e.metric}"
    if e.source == EventSource.CLOUDWATCH_LOG:
        return f"log:{message_template(e.message)}"
    if e.source in (EventSource.CLOUDTRAIL, EventSource.AWS_CONFIG):
        return f"{event_kind(e)}:{e.event_type}"
    return None


def build_query(
    incident: IncidentRecord,
    events: list[CanonicalEvent],
    evidence_event_ids: list[str],
    resources: list[ResourceRecord] | None = None,
    graph: NetworkXGraphStore | None = None,
    extra_lines: list[str] | None = None,
) -> RetrievalQuery:
    """Query from what is known at diagnosis time: the blinded description plus the ranked
    evidence (Phase 4) and, optionally, candidate-cause summaries. Never ground truth."""
    by_id = {e.event_id: e for e in events}
    lines, signals = [], set()
    for eid in evidence_event_ids:
        e = by_id.get(eid)
        if e is None:
            continue
        if e.source == EventSource.CLOUDWATCH_METRIC:
            line = f"{e.metric} anomalous on {e.resource_id}"
        elif e.source == EventSource.CLOUDWATCH_LOG:
            line = f"log on {e.resource_id}: {e.message}"
        else:
            line = f"{e.event_type} on {e.resource_id}: {e.message}"
        if line not in lines:
            lines.append(line)
        if (s := event_signal(e)) is not None:
            signals.add(s)
    lines += extra_lines or []
    alarm = next(
        (
            e
            for e in events
            if e.source == EventSource.ALARM and e.resource_id in incident.affected_resources
        ),
        None,
    )
    if resources is not None:
        types = {node_type_for(r.resource_type, r.resource_id) for r in resources}
    elif graph is not None:
        types = {graph.node_attrs(n)["node_type"] for n in graph.nodes()}
    else:
        types = set()
    return RetrievalQuery(
        text=f"{incident.title}. {incident.description} Key signals: " + "; ".join(lines),
        signals=signals,
        alarm_metric=alarm.metric if alarm else None,
        resource_types=types,
        incident_id=incident.incident_id,
    )


class KnowledgeBase:
    def __init__(
        self,
        embedder: Embedder,
        store: VectorStore,
        index_name: str = "historical",
        forbidden_fingerprints: set[str] | None = None,
    ):
        self.embedder = embedder
        self.store = store
        self.index_name = index_name
        self.forbidden = set(forbidden_fingerprints or ())
        self.records: dict[str, HistoricalRecord] = {}

    def build(self, records: list[HistoricalRecord]) -> int:
        """Index records. Refuses (LeakageError) any record carrying a forbidden fingerprint,
        before anything is written."""
        for r in records:
            hit = self.forbidden & set(r.fingerprints)
            if hit:
                raise LeakageError(
                    f"{r.incident_id} matches {len(hit)} held-out fingerprint(s); "
                    "test incidents must never be indexed"
                )
        if len({r.incident_id for r in records}) != len(records):
            raise ValueError("duplicate incident ids in the knowledge base")
        self.store.create_index(self.index_name, self.embedder.model_name, self.embedder.dimension)
        if not records:
            return 0
        vectors = self.embedder.embed([r.search_text for r in records])
        self.store.add(
            self.index_name,
            [r.incident_id for r in records],
            vectors,
            [{"taxonomy_label": r.taxonomy_label.value, "source": r.source} for r in records],
            self.embedder.model_name,
        )
        self.records.update({r.incident_id: r for r in records})
        return len(records)

    def indexed_ids(self) -> set[str]:
        return self.store.ids(self.index_name)


class HistoricalRetriever:
    def __init__(self, kb: KnowledgeBase, settings: RagSettings | None = None):
        self.kb = kb
        self.settings = settings or RagSettings()

    def retrieve(
        self,
        query: RetrievalQuery,
        k: int | None = None,
        exclude_ids: set[str] | None = None,
        rerank: bool = True,
    ) -> list[RetrievedIncident]:
        st = self.settings
        k = k or st.top_k
        exclude = set(exclude_ids or ())
        if query.incident_id:
            exclude.add(query.incident_id)
        [vector] = self.kb.embedder.embed([query.text])
        hits = self.kb.store.search(
            self.kb.index_name,
            vector,
            max(st.candidates, k),
            self.kb.embedder.model_name,
            exclude_ids=exclude,
        )
        w = st.rerank
        scored = []
        for h in hits:
            rec = self.kb.records[h.id]
            sig = _jaccard(query.signals, rec.signals)
            ctx = 0.5 * float(
                query.alarm_metric is not None and query.alarm_metric == rec.alarm_metric
            ) + 0.5 * _jaccard(query.resource_types, rec.resource_types)
            score = (
                w.similarity * h.score + w.signals * sig + w.context * ctx if rerank else h.score
            )
            scored.append((round(score, 6), h.score, sig, ctx, rec))
        scored.sort(key=lambda x: (-x[0], x[4].incident_id))
        out = []
        for score, sim, sig, ctx, rec in scored:
            if score < st.min_score:
                continue
            out.append(
                RetrievedIncident(
                    incident_id=rec.incident_id,
                    rank=len(out) + 1,
                    score=score,
                    similarity=round(sim, 6),
                    signal_overlap=round(sig, 6),
                    context_match=round(ctx, 6),
                    taxonomy_label=rec.taxonomy_label,
                    title=rec.title,
                    summary=rec.summary,
                    root_cause_text=rec.root_cause_text,
                    resolution=rec.resolution,
                )
            )
            if len(out) == k:
                break
        return out
