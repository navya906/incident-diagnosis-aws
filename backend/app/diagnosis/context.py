"""Context builder: ordered sections under a token budget, every event line tagged with its
stable evidence id.

Sections, always in this order (BRIEF Phase 6): incident, timeline, anomalies, logs, CloudTrail,
graph, historical. Each line that states an observed fact carries ``[evd_...]`` (the Phase 4
evidence id of that event); the diagnosis may cite only those ids (citation verifier).

Token budget: tokens are estimated as ceil(chars / 4) (deterministic; API usage reports the real
count). Each present section gets its share of the budget, capped at what it needs; budget left
over flows to later sections that need more, in section order. Inside a section, items are kept
in priority order (ranked evidence first, then signals, then the rest) until the allocation is
used; the section header is always kept, and dropped items are counted (DECISIONS D72).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, Field

from app.config import DiagnosisSettings
from app.contracts.anomaly import Anomaly
from app.contracts.events import CanonicalEvent, EventSource
from app.contracts.evidence import make_evidence_id
from app.contracts.historical import RetrievedIncident
from app.evidence.pipeline import Investigation
from app.offline.models import IncidentRecord
from app.rag.guidance import render_historical_section

SECTION_ORDER = ("incident", "timeline", "anomalies", "logs", "cloudtrail", "graph", "historical")
SOURCE_TAG = {
    EventSource.CLOUDWATCH_METRIC: "METRIC",
    EventSource.CLOUDWATCH_LOG: "LOG",
    EventSource.CLOUDTRAIL: "CLOUDTRAIL",
    EventSource.AWS_CONFIG: "CONFIG",
    EventSource.ALARM: "ALARM",
}
#: One evidence line: [evd_id] ISO-time resource TAG text
EVIDENCE_LINE = re.compile(
    r"^\[(?P<evd>evd_[0-9a-f]{16})\] (?P<time>\S+) (?P<resource>\S+) (?P<tag>[A-Z]+) (?P<text>.*)$"
)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
MAX_MESSAGE_CHARS = 300


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


class EvidenceRef(BaseModel):
    """What the citation verifier knows about one citable evidence id."""

    evidence_id: str
    event_id: str
    timestamp: datetime
    source: str
    resource_id: str
    event_type: str
    metric: str | None = None
    value: float | None = None
    line: str
    numbers: list[float] = Field(default_factory=list)


class SectionStats(BaseModel):
    name: str
    allocated_tokens: int
    used_tokens: int
    items_included: int
    items_dropped: int


class ContextBundle(BaseModel):
    incident_id: str
    text: str
    sections: list[SectionStats]
    evidence_index: dict[str, EvidenceRef]
    resources: list[str]
    retrieved_ids: list[str] = Field(default_factory=list)
    budget_tokens: int
    used_tokens: int

    def section_names(self) -> list[str]:
        return [s.name for s in self.sections]


@dataclass
class _Item:
    text: str
    priority: tuple[int, float, str]  # (group, rank or epoch seconds, tie-break)
    evidence_ids: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text) + 1


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text: str, n: int = MAX_MESSAGE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3] + "..."


class ContextBuilder:
    def __init__(self, settings: DiagnosisSettings | None = None):
        self.settings = settings or DiagnosisSettings()

    # ------------------------------------------------------------------ evidence lines
    def _line(self, incident_id: str, e: CanonicalEvent, anomaly: Anomaly | None) -> str:
        evd = make_evidence_id(incident_id, e.event_id)
        tag = SOURCE_TAG[e.source]
        if e.source == EventSource.CLOUDWATCH_METRIC:
            text = f"{e.metric}={e.value:g}"
            if anomaly is not None:
                text += (
                    f" (anomalous: baseline {anomaly.baseline:.4g}, {anomaly.method} score "
                    f"{anomaly.score:.1f})"
                )
        elif e.source == EventSource.CLOUDWATCH_LOG:
            text = f"{e.severity.value}: {_clip(e.message)}"
        elif e.source == EventSource.CLOUDTRAIL:
            who = e.metadata.get("userIdentity", "")
            err = e.metadata.get("errorCode")
            text = e.event_type + (f" errorCode={err}" if err else "")
            text += f" by {who}: {_clip(e.message)}" if who else f": {_clip(e.message)}"
        else:
            text = _clip(e.message)
        return f"[{evd}] {_iso(e.timestamp)} {e.resource_id} {tag} {text}"

    @staticmethod
    def _ref(evd: str, e: CanonicalEvent, line: str) -> EvidenceRef:
        # Hex ids like f7e821be contain "7e821", which parses as infinity: keep finite only.
        nums = [v for x in _NUMBER.findall(line.split(" ", 2)[2]) if math.isfinite(v := float(x))]
        if e.value is not None:
            nums.append(float(e.value))
        if "threshold" in e.metadata:
            nums.append(float(e.metadata["threshold"]))
        return EvidenceRef(
            evidence_id=evd,
            event_id=e.event_id,
            timestamp=e.timestamp,
            source=e.source.value,
            resource_id=e.resource_id,
            event_type=e.event_type,
            metric=e.metric,
            value=e.value,
            line=line,
            numbers=sorted(set(nums)),
        )

    # ------------------------------------------------------------------ build
    def build(
        self,
        *,
        incident: IncidentRecord,
        events: list[CanonicalEvent],
        investigation: Investigation,
        retrieved: list[RetrievedIncident] | None = None,
        include_graph: bool = True,
        include_historical: bool = True,
    ) -> ContextBundle:
        st = self.settings
        iid = incident.incident_id
        by_id = {e.event_id: e for e in events}
        anomaly_at: dict[tuple, Anomaly] = {}
        for a in investigation.anomalies or ():
            k = (a.resource_id, a.metric, a.timestamp)
            if k not in anomaly_at or a.score > anomaly_at[k].score:
                anomaly_at[k] = a
        index: dict[str, EvidenceRef] = {}

        def item_for(e: CanonicalEvent, priority: tuple) -> _Item:
            line = self._line(iid, e, anomaly_at.get((e.resource_id, e.metric, e.timestamp)))
            evd = make_evidence_id(iid, e.event_id)
            index.setdefault(evd, self._ref(evd, e, line))
            return _Item(line, priority, [evd])

        items: dict[str, list[_Item]] = {name: [] for name in SECTION_ORDER}
        seen: set[str] = set()

        # incident ------------------------------------------------------------
        corr = investigation.correlation
        head = [
            f"Title: {incident.title}",
            f"Description (symptoms only): {incident.description}",
            f"Alarm time: {_iso(incident.alarm_time)}; "
            f"estimated onset: {_iso(investigation.onset)}",
            f"Affected resources: {', '.join(incident.affected_resources)}",
            f"Collection window: {_iso(incident.window_start)} to {_iso(incident.window_end)}",
        ]
        items["incident"] = [_Item(t, (0, i, "")) for i, t in enumerate(head)]
        for e in events:
            if e.source == EventSource.ALARM and e.resource_id in incident.affected_resources:
                items["incident"].append(item_for(e, (1, e.timestamp.timestamp(), e.event_id)))
                seen.add(e.event_id)

        # timeline: candidate causes and their chains ------------------------
        if corr is not None:
            sig = {s.signal_id: s for s in corr.signals}
            for i, c in enumerate(corr.candidate_causes, start=1):
                chain = []
                for sid in c.chain.signal_ids:
                    s = sig.get(sid)
                    ids = [make_evidence_id(iid, x) for x in (s.event_ids[:1] if s else [])]
                    for x in s.event_ids[:1] if s else []:
                        if x in by_id:
                            item_for(by_id[x], (9, 0, x))  # make the chain ids citable
                    chain.append(f"{s.kind}@{s.resource_id}" + (f" [{ids[0]}]" if ids else ""))
                text = (
                    f"Candidate cause {i} (score {c.score:.2f}): {c.signal.kind} on "
                    f"{c.signal.resource_id} at {_iso(c.signal.timestamp)}: "
                    f"{_clip(c.signal.summary, 160)} | dependency path: "
                    + " -> ".join(c.dependency_path)
                    + " | chain: "
                    + " -> ".join(chain)
                )
                items["timeline"].append(_Item(text, (0, i, "")))

        # ranked evidence into anomalies / logs / cloudtrail ------------------
        def section_of(e: CanonicalEvent) -> str | None:
            if e.source == EventSource.CLOUDWATCH_METRIC:
                return "anomalies"
            if e.source == EventSource.CLOUDWATCH_LOG:
                return "logs"
            if e.source in (EventSource.CLOUDTRAIL, EventSource.AWS_CONFIG):
                return "cloudtrail"
            return None

        for ev in investigation.ranking.items:
            e = by_id.get(ev.event_id)
            if e is None or e.event_id in seen or (sec := section_of(e)) is None:
                continue
            items[sec].append(item_for(e, (0, ev.rank, e.event_id)))
            seen.add(e.event_id)
        if corr is not None:
            budget = st.max_signals_per_section
            for s in corr.signals:
                if s.kind == "alarm":
                    continue
                for x in s.event_ids[:1]:
                    e = by_id.get(x)
                    if e is None or x in seen or (sec := section_of(e)) is None:
                        continue
                    if sum(1 for it in items[sec] if it.priority[0] == 1) >= budget:
                        continue
                    items[sec].append(item_for(e, (1, e.timestamp.timestamp(), x)))
                    seen.add(x)

        # graph ---------------------------------------------------------------
        graph = investigation.graph
        resources: set[str] = set(incident.affected_resources)
        if include_graph and graph is not None:
            resources |= set(graph.nodes())
            node_line = "Resources: " + "; ".join(
                f"{n} ({graph.node_attrs(n)['node_type']})" for n in graph.nodes()
            )
            edge_line = "Dependencies (dependent -> dependency): " + "; ".join(
                f"{s} -{k}-> {t}" for s, t, k in graph.edges()
            )
            items["graph"] = [_Item(node_line, (0, 0, "")), _Item(edge_line, (0, 1, ""))]
            for a in incident.affected_resources:
                sources = sorted(graph.impact_sources({a}) - {a})
                items["graph"].append(
                    _Item(
                        f"Failures that can reach {a} start at: {', '.join(sources) or 'none'}",
                        (1, 0, a),
                    )
                )

        # historical ----------------------------------------------------------
        retrieved_ids: list[str] = []
        if include_historical and retrieved is not None:
            retrieved_ids = [r.incident_id for r in retrieved]
            items["historical"] = [_Item(render_historical_section(retrieved), (0, 0, ""))]

        present = [n for n in SECTION_ORDER if items[n]]
        if not include_graph:
            present = [n for n in present if n != "graph"]
        text, stats, used = self._fit(present, items, retrieved)
        cited_ok = {evd for evd in index if f"[{evd}]" in text}
        index = {k: v for k, v in index.items() if k in cited_ok}
        # Known resources = every resource that appears in the context.
        resources |= {ref.resource_id for ref in index.values()}
        return ContextBundle(
            incident_id=iid,
            text=text,
            sections=stats,
            evidence_index=index,
            resources=sorted(resources),
            retrieved_ids=[r for r in retrieved_ids if r in text],
            budget_tokens=st.context_token_budget,
            used_tokens=used,
        )

    # ------------------------------------------------------------------ baselines (B2, B3)
    def _baseline(
        self, incident: IncidentRecord, events: list[CanonicalEvent], raw: bool
    ) -> ContextBundle:
        """B2 (`raw=False`): the incident section only. B3 (`raw=True`): plus raw telemetry
        chosen newest-first until the same token budget is used, with no ranking, anomaly
        annotations, chains, graph or history; selected lines are shown oldest-first per source
        section. The onset shown is the alarm time (no anomaly detection) (DECISIONS D78)."""
        st = self.settings
        iid = incident.incident_id
        index: dict[str, EvidenceRef] = {}

        def item_for(e: CanonicalEvent, priority: tuple) -> _Item:
            line = self._line(iid, e, None)
            evd = make_evidence_id(iid, e.event_id)
            index.setdefault(evd, self._ref(evd, e, line))
            return _Item(line, priority, [evd])

        items: dict[str, list[_Item]] = {name: [] for name in SECTION_ORDER}
        head = [
            f"Title: {incident.title}",
            f"Description (symptoms only): {incident.description}",
            f"Alarm time: {_iso(incident.alarm_time)}",
            f"Affected resources: {', '.join(incident.affected_resources)}",
            f"Collection window: {_iso(incident.window_start)} to {_iso(incident.window_end)}",
        ]
        items["incident"] = [_Item(t, (0, i, "")) for i, t in enumerate(head)]
        for e in events:
            if e.source == EventSource.ALARM and e.resource_id in incident.affected_resources:
                items["incident"].append(item_for(e, (1, e.timestamp.timestamp(), e.event_id)))
        if raw:
            section = {
                EventSource.CLOUDWATCH_METRIC: "anomalies",
                EventSource.CLOUDWATCH_LOG: "logs",
                EventSource.CLOUDTRAIL: "cloudtrail",
                EventSource.AWS_CONFIG: "cloudtrail",
            }
            budget = st.context_token_budget - sum(i.tokens for i in items["incident"]) - 40
            chosen, used = [], 0
            newest = sorted(
                (e for e in events if e.source in section),
                key=lambda e: (e.timestamp, e.event_id),
                reverse=True,
            )
            for e in newest:
                it = item_for(e, (0, e.timestamp.timestamp(), e.event_id))
                if used + it.tokens > budget:
                    index.pop(it.evidence_ids[0], None)
                    break
                used += it.tokens
                chosen.append((e, it))
            for e, it in chosen:
                items[section[e.source]].append(it)
        present = [n for n in SECTION_ORDER if items[n]]
        text, stats, used_tokens = self._fit(present, items, None)
        index = {k: v for k, v in index.items() if f"[{k}]" in text}
        resources = set(incident.affected_resources) | {r.resource_id for r in index.values()}
        return ContextBundle(
            incident_id=iid,
            text=text,
            sections=stats,
            evidence_index=index,
            resources=sorted(resources),
            budget_tokens=st.context_token_budget,
            used_tokens=used_tokens,
        )

    def build_raw(self, *, incident: IncidentRecord, events: list[CanonicalEvent]) -> ContextBundle:
        return self._baseline(incident, events, raw=True)

    def build_description(
        self, *, incident: IncidentRecord, events: list[CanonicalEvent]
    ) -> ContextBundle:
        return self._baseline(incident, events, raw=False)

    # ------------------------------------------------------------------ budget
    def _fit(self, present: list[str], items: dict[str, list[_Item]], retrieved):
        st = self.settings
        budget = st.context_token_budget
        shares = st.section_shares.model_dump()
        total_share = sum(shares[n] for n in present) or 1.0
        headers = {n: f"## {n.upper()}" for n in present}
        need = {
            n: estimate_tokens(headers[n]) + 1 + sum(i.tokens for i in items[n]) for n in present
        }
        alloc = {n: min(need[n], int(budget * shares[n] / total_share)) for n in present}
        left = budget - sum(alloc.values())
        for n in present:
            extra = min(left, need[n] - alloc[n])
            alloc[n] += extra
            left -= extra
        out, stats, used = [], [], 0
        for n in present:
            chosen = [headers[n]]
            tokens = estimate_tokens(headers[n]) + 1
            dropped = 0
            ordered = sorted(items[n], key=lambda i: i.priority)
            if n == "historical" and retrieved:
                ordered = self._historical_fit(retrieved, alloc[n] - tokens)
            for it in ordered:
                if tokens + it.tokens <= alloc[n] or (n == "incident" and it.priority[0] == 0):
                    chosen.append(it.text)
                    tokens += it.tokens
                else:
                    dropped += 1
            out.append("\n".join(chosen))
            stats.append(
                SectionStats(
                    name=n,
                    allocated_tokens=alloc[n],
                    used_tokens=tokens,
                    items_included=len(chosen) - 1,
                    items_dropped=dropped,
                )
            )
            used += tokens
        return "\n\n".join(out) + "\n", stats, used

    @staticmethod
    def _historical_fit(retrieved: list[RetrievedIncident], room: int) -> list[_Item]:
        """The guidance plus as many past incidents as fit (most similar first)."""
        for n in range(len(retrieved), 0, -1):
            for max_chars in (1200, 600, 300):
                text = render_historical_section(retrieved[:n], max_chars=max_chars)
                if estimate_tokens(text) + 1 <= room:
                    return [_Item(text, (0, 0, ""))]
        return [_Item(render_historical_section([]), (0, 0, ""))]
