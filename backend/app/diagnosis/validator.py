"""Output validation and citation verification.

`validate_output` rejects (with reasons that feed the single repair attempt):
- text that is not one JSON object (code fences and surrounding prose are tolerated);
- schema violations (`Diagnosis`, including "a conclusion needs supporting evidence");
- citations of evidence ids that are not in the context (hallucinated or historical ids);
- FACT explanations quoting a number that is not in the cited evidence line (within tolerance);
- a root-cause resource that does not appear in the context;
- `historical_influence` misuse (`validate_historical_influence`).

`requires_human_review` is not a rejection reason: the configured policy (`needs_review`) is
applied deterministically afterwards and recorded as an override (DECISIONS D74).
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

from app.contracts.diagnosis import Diagnosis, needs_review
from app.contracts.historical import RetrievedIncident
from app.contracts.taxonomy import RootCause
from app.diagnosis.context import ContextBundle
from app.rag.guidance import validate_historical_influence

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
#: Standalone numbers: not part of ids, times, dates or resource names.
_QUOTED_NUMBER = re.compile(r"(?<![\w.:/-])-?\d+(?:\.\d+)?(?![\w:/-]|\.\d)")
_STRIP = re.compile(
    r"\[?evd_[0-9a-f]{16}\]?|\d{4}-\d{2}-\d{2}T[\d:.]+Z?|\b\d{1,2}:\d{2}(?::\d{2})?\b"
)


class ValueMismatch(BaseModel):
    evidence_id: str
    quoted: float


class CitationReport(BaseModel):
    cited: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    value_mismatches: list[ValueMismatch] = Field(default_factory=list)
    checked_values: int = 0

    @property
    def unsupported(self) -> int:
        return len(set(self.unknown) | {m.evidence_id for m in self.value_mismatches})

    @property
    def unsupported_rate(self) -> float:
        """Share of cited evidence ids that do not exist or whose quoted values do not match."""
        return self.unsupported / len(self.cited) if self.cited else 0.0


class ValidationOutcome(BaseModel):
    diagnosis: Diagnosis | None = None
    errors: list[str] = Field(default_factory=list)
    citation: CitationReport = Field(default_factory=CitationReport)
    policy_overrides: list[str] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.diagnosis is not None and not self.errors


def parse_json(text: str) -> tuple[dict | None, str | None]:
    body = _FENCE.sub("", text.strip())
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None, "output is not a JSON object"
    try:
        data = json.loads(body[start : end + 1])
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e.msg} at position {e.pos}"
    if not isinstance(data, dict):
        return None, "output is not a JSON object"
    return data, None


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= max(tol * max(abs(a), abs(b)), 1e-9)


def verify_citations(d: Diagnosis, ctx: ContextBundle, tolerance: float = 0.01) -> CitationReport:
    cited = [e.evidence_id for e in d.supporting_evidence]
    cited += [e.evidence_id for e in d.contradicting_evidence]
    report = CitationReport(cited=list(dict.fromkeys(cited)))
    report.unknown = [c for c in report.cited if c not in ctx.evidence_index]
    for item in d.supporting_evidence:
        ref = ctx.evidence_index.get(item.evidence_id)
        if ref is None or item.label != "FACT":
            continue
        for raw in _QUOTED_NUMBER.findall(_STRIP.sub(" ", item.explanation)):
            x = float(raw)
            if x.is_integer() and abs(x) < 10:
                continue  # small counts and ordinals ("2 tasks", "#1") are not quoted values
            report.checked_values += 1
            if not any(_close(x, n, tolerance) for n in ref.numbers):
                report.value_mismatches.append(ValueMismatch(evidence_id=ref.evidence_id, quoted=x))
    return report


def _schema_errors(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "(root)"
        out.append(f"schema: {loc}: {err['msg']}")
    return out


def validate_output(
    text: str,
    ctx: ContextBundle,
    retrieved: list[RetrievedIncident] | None,
    review_threshold: float,
    tolerance: float = 0.01,
) -> ValidationOutcome:
    data, err = parse_json(text)
    if err:
        return ValidationOutcome(errors=[err])
    try:
        d = Diagnosis.model_validate(data)
    except ValidationError as e:
        return ValidationOutcome(errors=_schema_errors(e))

    errors: list[str] = []
    citation = verify_citations(d, ctx, tolerance)
    for c in citation.unknown:
        errors.append(f"citation: {c} is not an evidence id in the context")
    for m in citation.value_mismatches:
        errors.append(
            f"citation: {m.evidence_id} explanation quotes {m.quoted:g}, which is not in that "
            "evidence line"
        )
    rc = d.root_cause
    if rc.taxonomy_label != RootCause.INSUFFICIENT_EVIDENCE and rc.resource_id not in ctx.resources:
        errors.append(f"root_cause.resource_id {rc.resource_id!r} does not appear in the context")
    shown = [r for r in (retrieved or []) if r.incident_id in ctx.retrieved_ids]
    if d.historical_influence.used and not shown:
        errors.append("historical_influence.used=true but no historical incidents were provided")
    errors += [f"historical: {p}" for p in validate_historical_influence(d, shown)]

    overrides = []
    if needs_review(d, review_threshold) and not d.requires_human_review:
        d = d.model_copy(update={"requires_human_review": True})
        overrides.append(
            f"requires_human_review set to true (confidence {rc.confidence:.2f} < "
            f"{review_threshold} or insufficient_evidence)"
        )
    return ValidationOutcome(
        diagnosis=d, errors=errors, citation=citation, policy_overrides=overrides
    )
