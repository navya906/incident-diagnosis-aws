"""Diagnosis engine: investigation -> retrieval -> context -> prompt -> LLM -> validate/repair ->
self-consistency -> deterministic severity.

- One primary answer at `llm.temperature`; an invalid answer gets exactly one repair attempt
  (temperature 0) and is otherwise rejected with the recorded failure.
- Self-consistency: `diagnosis.self_consistency_samples` extra answers at
  `self_consistency_temperature` with seeds `llm.seed + 1 ...`. Self-consistency confidence =
  share of all samples (invalid ones count as disagreeing) whose taxonomy label equals the
  primary answer's (DECISIONS D77).
- One `Redactor` per incident across all calls, so pseudonyms stay stable between the primary
  answer, its repair and the samples when the client is external.
- Severity comes from `SeverityEngine`; the model's `severity_suggestion` is kept as advisory.
"""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, Field

from app.ai.redaction import RedactingLLMClient, Redactor
from app.config import Settings, get_settings
from app.contracts.diagnosis import Diagnosis
from app.contracts.events import CanonicalEvent
from app.contracts.historical import RetrievedIncident
from app.diagnosis.context import ContextBuilder, ContextBundle, SectionStats
from app.diagnosis.prompts import (
    PROMPT_VERSION,
    build_repair_request,
    build_request,
    prompt_sha,
)
from app.diagnosis.validator import CitationReport, ValidationOutcome, validate_output
from app.evidence.pipeline import Investigation, InvestigationPipeline
from app.interfaces.llm_client import LLMClient, LLMRequest
from app.offline.models import IncidentRecord, RelationshipRecord, ResourceRecord
from app.rag.knowledge_base import HistoricalRetriever, build_query
from app.severity.engine import SeverityAssessment, SeverityEngine

LOCAL_LABEL = "smoke-test / synthetic"


class AttemptRecord(BaseModel):
    kind: str  # primary | repair | sample-<n> | sample-<n>-repair
    valid: bool
    errors: list[str] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0


class SelfConsistency(BaseModel):
    samples: int = 0
    valid_samples: int = 0
    labels: list[str | None] = Field(default_factory=list)
    majority_label: str | None = None
    agreement_with_primary: float | None = None  # the self-consistency confidence


class DiagnosisResult(BaseModel):
    incident_id: str
    condition: str
    model: str
    prompt_version: str
    prompt_sha: str
    valid: bool
    diagnosis: Diagnosis | None = None
    failure: dict | None = None
    policy_overrides: list[str] = Field(default_factory=list)
    attempts: list[AttemptRecord] = Field(default_factory=list)
    citation: CitationReport = Field(default_factory=CitationReport)
    verbalized_confidence: float | None = None
    self_consistency: SelfConsistency = Field(default_factory=SelfConsistency)
    severity: SeverityAssessment
    llm_severity_suggestion: str | None = None  # advisory only
    retrieved_incident_ids: list[str] = Field(default_factory=list)
    context_sections: list[SectionStats] = Field(default_factory=list)
    context_evidence_ids: list[str] = Field(default_factory=list)
    context_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    label: str = LOCAL_LABEL


class DiagnosisEngine:
    def __init__(
        self,
        llm: LLMClient,
        settings: Settings | None = None,
        retriever: HistoricalRetriever | None = None,
        pipeline: InvestigationPipeline | None = None,
        severity: SeverityEngine | None = None,
    ):
        self.settings = settings or get_settings()
        self.llm = llm
        self.retriever = retriever
        self.pipeline = pipeline or InvestigationPipeline(self.settings)
        self.context = ContextBuilder(self.settings.diagnosis)
        self.severity = severity or SeverityEngine(self.settings.severity)

    # ------------------------------------------------------------------ one call + repair
    def _call(self, request: LLMRequest, redactor: Redactor | None):
        if isinstance(self.llm, RedactingLLMClient):
            return self.llm.complete(request, redactor=redactor)
        return self.llm.complete(request)

    def _answer(
        self,
        request: LLMRequest,
        ctx: ContextBundle,
        retrieved: list[RetrievedIncident] | None,
        kind: str,
        redactor: Redactor | None,
        attempts: list[AttemptRecord],
    ) -> tuple[ValidationOutcome, str]:
        st = self.settings.diagnosis
        resp = self._call(request, redactor)
        out = validate_output(
            resp.text, ctx, retrieved, st.review_confidence_threshold, st.quote_tolerance
        )
        attempts.append(self._record(kind, out, resp))
        if out.valid:
            return out, resp.text
        repair = build_repair_request(request, resp.text, out.errors)
        resp2 = self._call(repair, redactor)
        out2 = validate_output(
            resp2.text, ctx, retrieved, st.review_confidence_threshold, st.quote_tolerance
        )
        attempts.append(self._record(f"{kind}-repair", out2, resp2))
        if not out2.valid:
            out2.errors = [f"after repair: {e}" for e in out2.errors] + [
                f"first attempt: {e}" for e in out.errors
            ]
        return out2, resp2.text

    @staticmethod
    def _record(kind: str, out: ValidationOutcome, resp) -> AttemptRecord:
        return AttemptRecord(
            kind=kind,
            valid=out.valid,
            errors=out.errors,
            prompt_tokens=resp.prompt_tokens,
            completion_tokens=resp.completion_tokens,
            latency_ms=resp.latency_ms,
            cost_usd=resp.estimated_cost_usd,
        )

    # ------------------------------------------------------------------ main entry
    def diagnose(
        self,
        *,
        incident: IncidentRecord,
        events: list[CanonicalEvent],
        resources: list[ResourceRecord],
        relationships: list[RelationshipRecord],
        condition: str = "Full",
        use_graph: bool = True,
        use_anomalies: bool = True,
        use_chains: bool = True,
        use_rag: bool = True,
        samples: int | None = None,
        investigation: Investigation | None = None,
    ) -> DiagnosisResult:
        st = self.settings
        inv = investigation or self.pipeline.run(
            incident=incident,
            events=events,
            resources=resources,
            relationships=relationships,
            use_graph=use_graph,
            use_anomalies=use_anomalies,
            use_chains=use_chains,
        )
        retrieved = None
        if use_rag and self.retriever is not None and st.diagnosis.use_historical:
            extra = (
                [c.rationale for c in inv.correlation.candidate_causes[:3]]
                if inv.correlation
                else []
            )
            query = build_query(
                incident,
                events,
                [i.event_id for i in inv.ranking.items],
                resources=resources,
                extra_lines=extra,
            )
            retrieved = self.retriever.retrieve(query)
        ctx = self.context.build(
            incident=incident,
            events=events,
            investigation=inv,
            retrieved=retrieved,
            include_graph=use_graph,
            include_historical=retrieved is not None,
        )
        redactor = Redactor(st.redaction) if isinstance(self.llm, RedactingLLMClient) else None
        attempts: list[AttemptRecord] = []
        primary_req = build_request(
            ctx.text, st.llm.temperature, st.llm.max_output_tokens, st.llm.seed
        )
        primary, raw = self._answer(primary_req, ctx, retrieved, "primary", redactor, attempts)

        n = st.diagnosis.self_consistency_samples if samples is None else samples
        labels: list[str | None] = []
        for i in range(1, n + 1):
            req = build_request(
                ctx.text,
                st.diagnosis.self_consistency_temperature,
                st.llm.max_output_tokens,
                st.llm.seed + i,
            )
            out, _ = self._answer(req, ctx, retrieved, f"sample-{i}", redactor, attempts)
            labels.append(out.diagnosis.root_cause.taxonomy_label.value if out.valid else None)
        sc = SelfConsistency(samples=n, valid_samples=sum(1 for x in labels if x), labels=labels)
        if any(labels):
            sc.majority_label = Counter(x for x in labels if x).most_common(1)[0][0]
        if primary.valid and n:
            mine = primary.diagnosis.root_cause.taxonomy_label.value
            sc.agreement_with_primary = round(sum(1 for x in labels if x == mine) / n, 4)

        severity = self.severity.assess(
            incident=incident,
            events=events,
            onset=inv.onset,
            anomalies=inv.anomalies,
            resources=resources,
            graph=inv.graph,
        )
        d = primary.diagnosis if primary.valid else None
        failure = None
        if not primary.valid:
            failure = {"stage": "validation", "errors": primary.errors, "raw_output": raw[:4000]}
        return DiagnosisResult(
            incident_id=incident.incident_id,
            condition=condition,
            model=self.llm.model_name,
            prompt_version=PROMPT_VERSION,
            prompt_sha=prompt_sha(),
            valid=primary.valid,
            diagnosis=d,
            failure=failure,
            policy_overrides=primary.policy_overrides,
            attempts=attempts,
            citation=primary.citation,
            verbalized_confidence=d.root_cause.confidence if d else None,
            self_consistency=sc,
            severity=severity,
            llm_severity_suggestion=d.severity_suggestion.value if d else None,
            retrieved_incident_ids=ctx.retrieved_ids,
            context_sections=ctx.sections,
            context_evidence_ids=sorted(ctx.evidence_index),
            context_tokens=ctx.used_tokens,
            prompt_tokens=sum(a.prompt_tokens for a in attempts),
            completion_tokens=sum(a.completion_tokens for a in attempts),
            latency_ms=round(sum(a.latency_ms for a in attempts), 1),
            cost_usd=round(sum(a.cost_usd for a in attempts), 6),
            label=LOCAL_LABEL if not getattr(self.llm, "is_external", True) else "llm",
        )

    def diagnose_offline(self, collector, incident_id: str, **kwargs) -> DiagnosisResult:
        incident = collector.incident(incident_id)
        resources, relationships = collector.inventory(incident_id)
        events = collector.collect(collector.default_request(incident_id))
        return self.diagnose(
            incident=incident,
            events=events,
            resources=resources,
            relationships=relationships,
            **kwargs,
        )
