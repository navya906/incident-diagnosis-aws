"""B1: rule-based diagnosis (no LLM, no ranking, no graph, no retrieval).

Fixed keyword rules (the same table as the stub LLM, `STUB_RULES`, written on dev scenarios
only) are applied directly to the raw events in [alarm - 30 min, alarm + 10 min]. Metric
datapoints count as a signal when the value is more than double or less than half the median of
the same series before the window. Each rule hit adds its weight (CloudTrail/Config changes x2);
the best label wins, the strongest change event (else the strongest hit) gives the resource, and
the top three hits are cited. Because B1 shares the stub's rules, B1 vs Full with the stub only
measures the effect of the pipeline's evidence selection, not of an LLM (DECISIONS D79).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import timedelta

from app.ai.llm_clients import _COMPILED
from app.contracts.diagnosis import Diagnosis
from app.contracts.events import CanonicalEvent, EventSource
from app.contracts.evidence import make_evidence_id
from app.offline.models import IncidentRecord

MODEL = "rules-b1-v1"
BEFORE = timedelta(minutes=30)
AFTER = timedelta(minutes=10)


def _text(e: CanonicalEvent, baseline: dict) -> str | None:
    if e.source == EventSource.CLOUDWATCH_METRIC:
        base = baseline.get((e.resource_id, e.metric))
        if base is None or e.value is None:
            return None
        if (base > 0 and (e.value > 2 * base or e.value < 0.5 * base)) or (
            base == 0 and e.value > 0
        ):
            return f"{e.metric}={e.value:g} anomalous"
        return None
    if e.source == EventSource.ALARM:
        return None
    return f"{e.event_type} {e.message}"


def diagnose_rules(incident: IncidentRecord, events: list[CanonicalEvent]) -> Diagnosis:
    start, end = incident.alarm_time - BEFORE, incident.alarm_time + AFTER
    history = defaultdict(list)
    for e in events:
        if (
            e.source == EventSource.CLOUDWATCH_METRIC
            and e.timestamp < start
            and e.value is not None
        ):
            history[(e.resource_id, e.metric)].append(e.value)
    baseline = {k: statistics.median(v) for k, v in history.items() if v}
    scores: dict[str, float] = defaultdict(float)
    hits: dict[str, list[tuple[float, CanonicalEvent]]] = defaultdict(list)
    for e in sorted(events, key=lambda e: (e.timestamp, e.event_id)):
        if not (start <= e.timestamp <= end):
            continue
        text = _text(e, baseline)
        if text is None:
            continue
        boost = 2.0 if e.source in (EventSource.CLOUDTRAIL, EventSource.AWS_CONFIG) else 1.0
        for label, pattern, w in _COMPILED:
            if pattern.search(text):
                scores[label] += w * boost
                hits[label].append((w * boost, e))
    affected = incident.affected_resources[0]
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    if not ranked or ranked[0][1] < 3.0:
        return Diagnosis.model_validate(
            {
                "incident_summary": f"{incident.title}. Rule-based baseline (B1).",
                "root_cause": {
                    "taxonomy_label": "insufficient_evidence",
                    "description": "No rule matched strongly enough.",
                    "confidence": 0.3,
                    "resource_id": affected,
                },
                "severity_suggestion": "MEDIUM",
                "missing_information": ["matching signals"],
                "requires_human_review": True,
            }
        )
    best, best_score = ranked[0]
    total = sum(v for _, v in ranked)
    conf = round(min(0.9, max(0.35, best_score / total)), 2)
    strongest = sorted(hits[best], key=lambda x: (-x[0], x[1].timestamp, x[1].event_id))
    cited, seen = [], set()
    for _, e in strongest:
        if e.event_id not in seen:
            seen.add(e.event_id)
            cited.append(e)
        if len(cited) == 3:
            break
    changes = [e for e in cited if e.source in (EventSource.CLOUDTRAIL, EventSource.AWS_CONFIG)]
    resource = (changes or cited)[0].resource_id
    rest = ranked[1:3]
    rest_total = sum(v for _, v in rest) or 1.0
    alts = [
        {
            "taxonomy_label": label,
            "description": f"Rules also match {label.replace('_', ' ')}.",
            "confidence": int((1 - conf) * 0.95 * v / rest_total * 100) / 100,
            "rejected_because": "fewer matching signals",
        }
        for label, v in rest
    ]
    alts.sort(key=lambda a: -a["confidence"])
    return Diagnosis.model_validate(
        {
            "incident_summary": f"{incident.title}. Rule-based baseline (B1).",
            "root_cause": {
                "taxonomy_label": best,
                "description": f"Rules point to {best.replace('_', ' ')} on {resource}.",
                "confidence": conf,
                "resource_id": resource,
            },
            "supporting_evidence": [
                {
                    "evidence_id": make_evidence_id(incident.incident_id, e.event_id),
                    "explanation": f"{e.source.value} {e.event_type} on {e.resource_id}",
                    "label": "FACT",
                }
                for e in cited
            ],
            "alternative_hypotheses": alts,
            "impact_analysis": {"resources": sorted({affected, resource})},
            "severity_suggestion": "MEDIUM",
            "recommendations": [
                {"category": "INVESTIGATIVE", "action": f"Check the matched signals on {resource}."}
            ],
            "requires_human_review": conf < 0.6,
        }
    )
