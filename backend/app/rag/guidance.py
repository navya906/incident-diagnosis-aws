"""'Do not blindly reuse past diagnoses': the prompt section and the output check.

`render_historical_section` is the only way retrieved incidents enter a prompt. It states that
past incidents are context, not evidence, and how `historical_influence` must be filled in.
`validate_historical_influence` checks a diagnosis against those rules; the Phase 6 output
validator rejects (then repairs once) any diagnosis that fails it (DECISIONS D64).
"""

from __future__ import annotations

import re

from app.contracts.diagnosis import Diagnosis
from app.contracts.historical import RetrievedIncident

HISTORICAL_PROMPT_VERSION = "historical-v1"

HISTORICAL_GUIDANCE = """\
HISTORICAL INCIDENTS (context only, NOT evidence for the current incident)
Rules:
1. Past incidents only suggest what to check. They are not evidence. Never cite a historical
   incident id as an evidence_id; cite only evidence ids from the current incident's context.
2. Do not copy a past root cause or resolution. Adopt a past explanation only if the current
   evidence supports it, and say which current evidence does.
3. If the current evidence contradicts a past incident's explanation, say so and do not use it.
4. Fill historical_influence truthfully: used=true only if a past incident changed your
   reasoning; then list its incident_ids (only ids shown below) and explain in "how" what you
   took from it and which current evidence confirmed it. Otherwise used=false and no ids.
"""

_WORD = re.compile(r"[a-z0-9]+")
COPY_JACCARD = 0.9
MIN_HOW_CHARS = 20


def render_historical_section(retrieved: list[RetrievedIncident], max_chars: int = 1200) -> str:
    if not retrieved:
        return HISTORICAL_GUIDANCE + "\n(no similar past incidents were retrieved)\n"
    parts = [HISTORICAL_GUIDANCE]
    for r in retrieved:
        summary = r.summary if len(r.summary) <= max_chars else r.summary[: max_chars - 3] + "..."
        parts.append(
            f"[{r.incident_id}] (HISTORICAL, similarity {r.score:.2f}, past label "
            f"{r.taxonomy_label.value})\n{summary}\n"
        )
    return "\n".join(parts)


def _jaccard(a: str, b: str) -> float:
    x, y = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    return len(x & y) / len(x | y) if x | y else 0.0


def validate_historical_influence(
    diagnosis: Diagnosis, retrieved: list[RetrievedIncident]
) -> list[str]:
    """Problems with how the diagnosis used past incidents (empty list = acceptable)."""
    problems = []
    retrieved_ids = {r.incident_id for r in retrieved}
    hi = diagnosis.historical_influence
    cited = {e.evidence_id for e in diagnosis.supporting_evidence}
    cited |= {e.evidence_id for e in diagnosis.contradicting_evidence}
    if cited & retrieved_ids:
        problems.append(
            "historical incidents cited as evidence: " + ", ".join(sorted(cited & retrieved_ids))
        )
    if hi.used:
        unknown = set(hi.incident_ids) - retrieved_ids
        if unknown:
            problems.append(
                "historical_influence lists incidents that were not retrieved: "
                + ", ".join(sorted(unknown))
            )
        if len(hi.how.strip()) < MIN_HOW_CHARS:
            problems.append("historical_influence.used=true needs an explanation in 'how'")
    for r in retrieved:
        if _jaccard(diagnosis.root_cause.description, r.root_cause_text) >= COPY_JACCARD:
            problems.append(f"root cause copies past incident {r.incident_id} verbatim")
            if not hi.used or r.incident_id not in hi.incident_ids:
                problems.append(
                    f"past incident {r.incident_id} was used but not declared in "
                    "historical_influence"
                )
    return problems
