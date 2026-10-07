"""Per-diagnosis metrics and the recommendation rubric.

Root-cause match is deterministic (BRIEF Section 3): taxonomy label and primary resource.
Faithfulness uses the citation verifier on the FIRST answer (before repair), so the
hallucination rate measures what the model produced, not what survived validation.

Recommendation quality: `rubric_score` is a deterministic proxy (LOCAL-ONLY) in [0, 1] = mean of
four checks: an IMMEDIATE action exists; an action names the root-cause resource; the actions
share content words with the ground-truth resolution (Jaccard >= 0.15); an INVESTIGATIVE or
PREVENTIVE action exists. `JUDGE_PROMPT` is the rubric for an LLM judge on real runs, with a
seeded spot-check sample exported for human review (DECISIONS D81).
"""

from __future__ import annotations

import re

from app.contracts.events import CanonicalEvent
from app.contracts.evidence import make_evidence_id
from app.contracts.ground_truth import GroundTruth
from app.contracts.taxonomy import RootCause
from app.diagnosis.engine import DiagnosisResult

_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = frozenset(
    "the and for with from that this then into its are was were has have not but you your "
    "all any can will should check confirm".split()
)
RUBRIC_VERSION = "rubric-v1"

JUDGE_PROMPT = """\
You grade an incident diagnosis against the reference. Score 0-4, one point each:
1. The root cause names the same failure mechanism as the reference (wording may differ).
2. The root cause names the right resource.
3. The recommendations would resolve the incident per the reference resolution.
4. Nothing important is invented (claims are supported by the cited evidence).
Reference root cause: {reference}
Reference resolution: {resolution}
Diagnosis root cause: {diagnosis}
Diagnosis recommendations: {recommendations}
Answer with JSON: {{"score": 0-4, "reason": "..."}}"""


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def rubric_score(result: DiagnosisResult, truth: GroundTruth) -> float | None:
    d = result.diagnosis
    if d is None:
        return 0.0
    recs = d.recommendations
    text = " ".join(r.action for r in recs)
    short = d.root_cause.resource_id.rsplit("/", 1)[-1]
    words, ref = _words(text), _words(truth.resolution)
    overlap = len(words & ref) / len(words | ref) if words | ref else 0.0
    checks = [
        any(r.category.value == "IMMEDIATE" for r in recs),
        bool(short) and (d.root_cause.resource_id in text or short in text),
        overlap >= 0.15,
        any(r.category.value in ("INVESTIGATIVE", "PREVENTIVE") for r in recs),
    ]
    return sum(checks) / len(checks)


def record_metrics(
    result: DiagnosisResult, truth: GroundTruth, events: list[CanonicalEvent]
) -> dict:
    """Everything the report aggregates, for one diagnosis of one incident."""
    d = result.diagnosis
    gt_label, gt_res = truth.taxonomy_label.value, truth.primary_resource_id
    evd_to_event = {make_evidence_id(result.incident_id, e.event_id): e.event_id for e in events}
    gt_events = set(truth.evidence_event_ids)
    m: dict = {
        "valid": result.valid,
        "repaired": any(a.kind == "primary-repair" for a in result.attempts),
        "label_correct": False,
        "resource_correct": False,
        "root_cause_correct": False,
        "top3_correct": False,
        "secondary_found": None,
        "is_insufficient_case": truth.taxonomy_label == RootCause.INSUFFICIENT_EVIDENCE,
        "cited": 0,
        "cited_precision": None,
        "cited_recall": None,
        "context_recall": None,
        "context_precision": None,
        "first_cited": 0,
        "first_unsupported": 0,
        "verbalized_confidence": result.verbalized_confidence,
        "sc_confidence": result.self_consistency.agreement_with_primary,
        "rubric": rubric_score(result, truth),
        "historical_used": bool(d and d.historical_influence.used),
        "requires_review": bool(d and d.requires_human_review),
        "severity_level": result.severity.level.value,
        "severity_score": result.severity.score,
        "latency_ms": result.latency_ms,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "cost_usd": result.cost_usd,
        "predicted_label": d.root_cause.taxonomy_label.value if d else None,
    }
    primary = [a for a in result.attempts if a.kind == "primary"]
    if primary:
        m["first_cited"], m["first_unsupported"] = primary[0].cited, primary[0].unsupported
    context_events = list(dict.fromkeys(result.ranked_event_ids))
    if gt_events and result.context_mode != "rules":  # B1 has no model context
        hit = len(gt_events & set(context_events))
        m["context_recall"] = hit / len(gt_events)
        m["context_precision"] = hit / len(context_events) if context_events else 0.0
    if d is None:
        return m
    labels = [d.root_cause.taxonomy_label.value] + [
        a.taxonomy_label.value for a in d.alternative_hypotheses
    ]
    m["label_correct"] = labels[0] == gt_label
    m["resource_correct"] = d.root_cause.resource_id == gt_res
    m["root_cause_correct"] = m["label_correct"] and m["resource_correct"]
    m["top3_correct"] = gt_label in labels[:3]
    if truth.secondary_labels:
        m["secondary_found"] = any(s.value in labels for s in truth.secondary_labels)
    cited = {evd_to_event.get(e.evidence_id) for e in d.supporting_evidence} - {None}
    m["cited"] = len(cited)
    if cited:
        m["cited_precision"] = len(cited & gt_events) / len(cited)
    if gt_events:
        m["cited_recall"] = len(cited & gt_events) / len(gt_events)
    return m
