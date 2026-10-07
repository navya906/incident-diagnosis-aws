"""Versioned prompt templates. Changing any template text requires a new PROMPT_VERSION; the
SHA-256 of the templates is recorded with every diagnosis so a run can be traced to its exact
prompt (DECISIONS D73)."""

from __future__ import annotations

import hashlib
import json

from app.contracts.taxonomy import RootCause
from app.interfaces.llm_client import LLMRequest

PROMPT_VERSION = "diag-v1"

TAXONOMY = ", ".join(r.value for r in RootCause)

SYSTEM_PROMPT = f"""\
You are an AWS reliability engineer diagnosing a production incident. You receive the incident
context as sections (INCIDENT, TIMELINE, ANOMALIES, LOGS, CLOUDTRAIL, GRAPH, HISTORICAL; some may
be absent). Every observed fact in the context carries an evidence id like [evd_0123456789abcdef].

Rules:
1. Base every conclusion on the current incident's evidence. Cite evidence only by ids that appear
   in the context, never invent ids, and never cite a historical incident id as evidence.
2. When an explanation quotes a value (a metric value, count, duration), copy it exactly from the
   cited line.
3. root_cause.taxonomy_label must be one of: {TAXONOMY}.
   Use insufficient_evidence when the context cannot support a conclusion; then set
   requires_human_review=true and list what is missing in missing_information.
4. root_cause.resource_id must be a resource that appears in the context.
5. Confidences are probabilities: root_cause.confidence plus all alternative_hypotheses
   confidences must sum to at most 1, and alternatives are listed in descending confidence.
6. Labels: supporting_evidence items are FACT (directly observed in the cited line) or INFERENCE
   (derived from it); root_cause is INFERENCE; alternative_hypotheses are HYPOTHESIS;
   recommendations are RECOMMENDATION.
7. TIMELINE "candidate causes" are hypotheses from temporal precedence and dependency paths, not
   proven causes.
8. severity_suggestion is advisory only (a deterministic engine decides the official severity).
9. Fill historical_influence truthfully, following the rules in the HISTORICAL section.
10. Answer with one JSON object only, no prose and no code fences.
"""

OUTPUT_SPEC = """\
Return JSON with exactly these fields:
{
  "incident_summary": str,
  "root_cause": {"taxonomy_label": str, "description": str, "confidence": float,
                 "resource_id": str, "label": "INFERENCE"},
  "supporting_evidence": [{"evidence_id": str, "explanation": str, "label": "FACT"|"INFERENCE"}],
  "contradicting_evidence": [{"evidence_id": str, "explanation": str}],
  "contributing_factors": [{"description": str, "label": "FACT"|"INFERENCE"|"HYPOTHESIS"}],
  "alternative_hypotheses": [{"taxonomy_label": str, "description": str, "confidence": float,
                              "rejected_because": str, "label": "HYPOTHESIS"}],
  "historical_influence": {"used": bool, "incident_ids": [str], "how": str},
  "impact_analysis": {"services": [str], "resources": [str], "blast_radius": str,
                      "user_impact": str},
  "severity_suggestion": "LOW"|"MEDIUM"|"HIGH"|"CRITICAL",
  "recommendations": [{"category": "IMMEDIATE"|"INVESTIGATIVE"|"CORRECTIVE"|"PREVENTIVE",
                       "action": str, "label": "RECOMMENDATION"}],
  "missing_information": [str],
  "requires_human_review": bool
}
"""

USER_TEMPLATE = """\
INCIDENT CONTEXT
================
{context}
================
{output_spec}"""

REPAIR_TEMPLATE = """\
Your previous answer was rejected by the validator for these reasons:
{errors}

Previous answer:
{previous}

Return a corrected answer as one JSON object that fixes every listed problem and follows all rules.
Use only evidence ids that appear in the incident context below.

{user_prompt}"""


def prompt_sha() -> str:
    body = json.dumps([PROMPT_VERSION, SYSTEM_PROMPT, OUTPUT_SPEC, USER_TEMPLATE, REPAIR_TEMPLATE])
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def build_request(
    context_text: str, temperature: float, max_tokens: int, seed: int | None
) -> LLMRequest:
    return LLMRequest(
        system=SYSTEM_PROMPT,
        prompt=USER_TEMPLATE.format(context=context_text.rstrip(), output_spec=OUTPUT_SPEC),
        temperature=temperature,
        max_tokens=max_tokens,
        seed=seed,
        json_mode=True,
    )


def build_repair_request(original: LLMRequest, previous: str, errors: list[str]) -> LLMRequest:
    listed = "\n".join(f"- {e}" for e in errors[:30])
    prev = previous if len(previous) <= 6000 else previous[:6000] + "\n...(truncated)"
    return original.model_copy(
        update={
            "prompt": REPAIR_TEMPLATE.format(
                errors=listed, previous=prev, user_prompt=original.prompt
            ),
            "temperature": 0.0,
        }
    )
