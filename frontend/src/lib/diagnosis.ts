// "Never display a conclusion without its evidence" (BRIEF Phase 9) as a pure, tested rule.
import type { DiagnosisRecord, EventItem } from "../api/types";

export interface ResolvedClaim {
  evidence_id: string;
  explanation: string;
  label: "FACT" | "INFERENCE" | "CONTRADICTS";
  event: EventItem | undefined;
}

export type Verdict =
  | { kind: "show"; supporting: ResolvedClaim[]; contradicting: ResolvedClaim[] }
  | { kind: "insufficient"; supporting: ResolvedClaim[]; contradicting: ResolvedClaim[] }
  | { kind: "withheld"; reason: string; unresolved: string[] }
  | { kind: "invalid"; reason: string };

export function assessDiagnosis(rec: DiagnosisRecord): Verdict {
  const d = rec.diagnosis;
  if (!rec.valid || !d) {
    const errors = rec.failure?.errors ?? [];
    return {
      kind: "invalid",
      reason: errors.length
        ? `The model's answer was rejected by the validator: ${errors.slice(0, 3).join("; ")}`
        : "No valid diagnosis was produced.",
    };
  }
  const resolve = (
    items: { evidence_id: string; explanation: string; label?: "FACT" | "INFERENCE" }[],
    label?: "CONTRADICTS",
  ): ResolvedClaim[] =>
    items.map((e) => ({
      evidence_id: e.evidence_id,
      explanation: e.explanation,
      label: label ?? e.label ?? "FACT",
      event: rec.evidence_events[e.evidence_id],
    }));
  const supporting = resolve(d.supporting_evidence);
  const contradicting = resolve(d.contradicting_evidence, "CONTRADICTS");
  if (d.root_cause.taxonomy_label === "insufficient_evidence") {
    return { kind: "insufficient", supporting, contradicting };
  }
  const unresolved = supporting.filter((c) => !c.event).map((c) => c.evidence_id);
  if (supporting.length === 0) {
    return {
      kind: "withheld",
      reason: "The diagnosis names a root cause but cites no supporting evidence.",
      unresolved,
    };
  }
  if (unresolved.length > 0) {
    return {
      kind: "withheld",
      reason: "Some cited evidence could not be found for this incident, so the conclusion is not shown.",
      unresolved,
    };
  }
  return { kind: "show", supporting, contradicting };
}

/** Cited events in time order, for the diagnosis timeline. */
export function evidenceTimeline(claims: ResolvedClaim[]): ResolvedClaim[] {
  return claims
    .filter((c) => c.event)
    .slice()
    .sort((a, b) => (a.event!.timestamp < b.event!.timestamp ? -1 : 1));
}

export function pct(x: number | null | undefined): string {
  return x === null || x === undefined ? "n/a" : `${Math.round(x * 100)}%`;
}
