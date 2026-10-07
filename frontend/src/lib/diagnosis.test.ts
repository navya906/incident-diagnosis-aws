import { describe, expect, it } from "vitest";
import { diagnosisRecord, EVD_5XX, EVD_DEPLOY } from "../test/fixtures";
import { assessDiagnosis, evidenceTimeline } from "./diagnosis";

describe("assessDiagnosis: never a conclusion without its evidence", () => {
  it("shows a diagnosis whose every supporting citation resolves", () => {
    const v = assessDiagnosis(diagnosisRecord());
    expect(v.kind).toBe("show");
    if (v.kind === "show") {
      expect(v.supporting.map((c) => c.evidence_id)).toEqual([EVD_DEPLOY, EVD_5XX]);
      expect(v.supporting.every((c) => c.event)).toBe(true);
      expect(v.contradicting[0]!.label).toBe("CONTRADICTS");
    }
  });

  it("withholds a root cause with no supporting evidence", () => {
    const rec = diagnosisRecord();
    rec.diagnosis!.supporting_evidence = [];
    expect(assessDiagnosis(rec).kind).toBe("withheld");
  });

  it("withholds when any supporting citation does not resolve", () => {
    const rec = diagnosisRecord();
    delete rec.evidence_events[EVD_5XX];
    const v = assessDiagnosis(rec);
    expect(v.kind).toBe("withheld");
    if (v.kind === "withheld") expect(v.unresolved).toEqual([EVD_5XX]);
  });

  it("treats rejected answers as invalid and shows the reason", () => {
    const v = assessDiagnosis(
      diagnosisRecord({ valid: false, diagnosis: null, failure: { stage: "validation", errors: ["bad json"] } }),
    );
    expect(v.kind).toBe("invalid");
    if (v.kind === "invalid") expect(v.reason).toContain("bad json");
  });

  it("presents insufficient evidence as such, not as a cause", () => {
    const rec = diagnosisRecord();
    rec.diagnosis!.root_cause.taxonomy_label = "insufficient_evidence";
    rec.diagnosis!.supporting_evidence = [];
    expect(assessDiagnosis(rec).kind).toBe("insufficient");
  });

  it("orders cited evidence by time", () => {
    const v = assessDiagnosis(diagnosisRecord());
    if (v.kind !== "show") throw new Error("expected show");
    const t = evidenceTimeline([...v.supporting].reverse());
    expect(t.map((c) => c.evidence_id)).toEqual([EVD_DEPLOY, EVD_5XX]);
  });
});
