import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { diagnosisRecord, EVD_5XX } from "../test/fixtures";
import { DiagnosisView } from "./DiagnosisView";

describe("DiagnosisView", () => {
  it("renders root cause -> evidence -> timeline -> resource -> dependency path, in that order", () => {
    render(<DiagnosisView rec={diagnosisRecord()} onsetAt="2025-03-01T11:59:00Z" alarmAt="2025-03-01T12:03:00Z" />);
    const order = ["root-cause", "supporting", "evidence-timeline", "resource", "dependency-path"].map(
      (id) => screen.getByTestId(id),
    );
    for (let i = 1; i < order.length; i++) {
      expect(order[i - 1]!.compareDocumentPosition(order[i]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(within(order[0]!).getByText("deployment_failure")).toBeInTheDocument();
    expect(within(order[0]!).getByText(/stated confidence 72%/)).toBeInTheDocument();
    const path = within(order[4]!);
    expect(path.getByText("ecs/service/web")).toBeInTheDocument();
    expect(path.getByText("alb/web-lb")).toBeInTheDocument();
  });

  it("labels every claim and shows the event behind each citation", () => {
    render(<DiagnosisView rec={diagnosisRecord()} />);
    const claims = within(screen.getByTestId("supporting")).getAllByTestId("evidence-claim");
    expect(claims).toHaveLength(2);
    expect(within(claims[0]!).getByText("FACT")).toBeInTheDocument();
    expect(within(claims[1]!).getByText("INFERENCE")).toBeInTheDocument();
    expect(within(claims[1]!).getByText(/HTTPCode_Target_5XX_Count = 412/)).toBeInTheDocument();
    const labels = screen.getAllByTestId("claim-label").map((e) => e.textContent);
    for (const l of ["FACT", "INFERENCE", "HYPOTHESIS", "RECOMMENDATION", "CONTRADICTS"]) {
      expect(labels).toContain(l);
    }
  });

  it("shows contradicting evidence and ranked alternatives with reasons", () => {
    render(<DiagnosisView rec={diagnosisRecord()} />);
    const contra = screen.getByTestId("contradicting");
    expect(within(contra).getByText("DB CPU stayed normal")).toBeInTheDocument();
    expect(within(contra).getByText(/CPUUtilization = 21/)).toBeInTheDocument();
    const alt = screen.getByTestId("alternatives");
    expect(within(alt).getByText("db_latency_increase")).toBeInTheDocument();
    expect(within(alt).getByText(/Rejected because: DB latency stayed flat/)).toBeInTheDocument();
  });

  it("treats the engine severity as official and the model's as advisory", () => {
    render(<DiagnosisView rec={diagnosisRecord()} />);
    expect(within(screen.getByTestId("severity")).getByText("HIGH")).toBeInTheDocument();
    expect(screen.getByTestId("advisory-severity")).toHaveTextContent("advisory only");
  });

  it("withholds the conclusion when evidence is missing", () => {
    const rec = diagnosisRecord();
    delete rec.evidence_events[EVD_5XX];
    render(<DiagnosisView rec={rec} />);
    expect(screen.getByTestId("diagnosis-withheld")).toBeInTheDocument();
    expect(screen.queryByText("deployment_failure")).not.toBeInTheDocument();
    expect(screen.queryByText(/Task definition web:42/)).not.toBeInTheDocument();
    expect(screen.getByText(new RegExp(EVD_5XX))).toBeInTheDocument();
  });

  it("withholds a root cause that cites nothing", () => {
    const rec = diagnosisRecord();
    rec.diagnosis!.supporting_evidence = [];
    render(<DiagnosisView rec={rec} />);
    expect(screen.getByText("Conclusion withheld")).toBeInTheDocument();
    expect(screen.queryByTestId("root-cause")).not.toBeInTheDocument();
  });

  it("explains rejected answers and insufficient evidence", () => {
    const { unmount } = render(
      <DiagnosisView rec={diagnosisRecord({ valid: false, diagnosis: null, failure: { stage: "validation", errors: ["citation: evd_x unknown"] } })} />,
    );
    expect(screen.getByTestId("diagnosis-invalid")).toHaveTextContent("citation: evd_x unknown");
    unmount();
    const rec = diagnosisRecord();
    rec.diagnosis!.root_cause.taxonomy_label = "insufficient_evidence";
    rec.diagnosis!.missing_information = ["application logs"];
    render(<DiagnosisView rec={rec} />);
    expect(screen.getByTestId("diagnosis-insufficient")).toHaveTextContent("application logs");
    expect(screen.queryByTestId("root-cause")).not.toBeInTheDocument();
  });

  it("labels synthetic results", () => {
    render(<DiagnosisView rec={diagnosisRecord()} />);
    expect(screen.getByText("smoke-test / synthetic")).toBeInTheDocument();
  });
});

describe("evidence timeline markers", () => {
  it("places the onset and alarm markers at their own times", async () => {
    const { withMarkers } = await import("./DiagnosisView");
    const { assessDiagnosis } = await import("../lib/diagnosis");
    const v = assessDiagnosis(diagnosisRecord());
    if (v.kind !== "show") throw new Error("expected show");
    // Onset 12:00:30 falls between the 11:58 deploy and the 12:01 metric.
    const rows = withMarkers(v.supporting, "2025-03-01T12:00:30Z", "2025-03-01T12:03:00Z");
    expect(rows.map((r) => (r.kind === "marker" ? r.text : r.claim.evidence_id))).toEqual([
      "evd_aaaaaaaaaaaaaaaa",
      "estimated onset",
      "evd_bbbbbbbbbbbbbbbb",
      "alarm",
    ]);
  });
});
