import type { DiagnosisRecord, EventItem, Incident } from "../api/types";

export const EVD_DEPLOY = "evd_aaaaaaaaaaaaaaaa";
export const EVD_5XX = "evd_bbbbbbbbbbbbbbbb";
export const EVD_CONTRA = "evd_cccccccccccccccc";

export function event(id: string, over: Partial<EventItem> = {}): EventItem {
  return {
    event_id: `ev_${id}`,
    timestamp: "2025-03-01T12:00:00Z",
    source: "cloudtrail",
    service: "ecs",
    resource_id: "ecs/service/web",
    event_type: "UpdateService",
    metric: null,
    value: null,
    severity: "INFO",
    message: "UpdateService: web now uses web:42",
    metadata: { userIdentity: "deployer" },
    ...over,
  };
}

export function diagnosisRecord(over: Partial<DiagnosisRecord> = {}): DiagnosisRecord {
  return {
    id: 7,
    incident_id: "inc-0123456789",
    condition: "Full",
    model: "stub-deterministic-v1",
    prompt_version: "diag-v1",
    valid: true,
    created_at: "2026-10-07T12:00:00Z",
    label: "smoke-test / synthetic",
    llm_severity_suggestion: "HIGH",
    verbalized_confidence: 0.72,
    self_consistency: { samples: 2, agreement_with_primary: 1 },
    retrieved_incident_ids: ["inc-past000001"],
    dependency_path: ["ecs/service/web", "alb/web-lb"],
    failure: null,
    severity: {
      level: "HIGH",
      score: 7,
      criticality: "MEDIUM",
      rationale: "score 7",
      factors: [
        { name: "error_rate", value: 0.31, unit: "fraction", points: 3, known: true, detail: "peak" },
        { name: "latency", value: null, unit: "x", points: 0, known: false, detail: "no latency metrics" },
      ],
    },
    evidence_events: {
      [EVD_DEPLOY]: event("deploy", { timestamp: "2025-03-01T11:58:00Z" }),
      [EVD_5XX]: event("5xx", {
        source: "cloudwatch_metric",
        resource_id: "alb/web-lb",
        event_type: "metric_datapoint",
        metric: "HTTPCode_Target_5XX_Count",
        value: 412,
        message: "",
        timestamp: "2025-03-01T12:01:00Z",
      }),
      [EVD_CONTRA]: event("contra", {
        source: "cloudwatch_metric",
        resource_id: "rds/db-main",
        event_type: "metric_datapoint",
        metric: "CPUUtilization",
        value: 21,
        message: "",
      }),
    },
    diagnosis: {
      incident_summary: "5XX errors after a deployment.",
      root_cause: {
        taxonomy_label: "deployment_failure",
        description: "Task definition web:42 fails at start-up.",
        confidence: 0.72,
        resource_id: "ecs/service/web",
        label: "INFERENCE",
      },
      supporting_evidence: [
        { evidence_id: EVD_DEPLOY, explanation: "UpdateService rolled out web:42", label: "FACT" },
        { evidence_id: EVD_5XX, explanation: "5XX rose right after the rollout", label: "INFERENCE" },
      ],
      contradicting_evidence: [{ evidence_id: EVD_CONTRA, explanation: "DB CPU stayed normal" }],
      contributing_factors: [{ description: "No canary deployment", label: "HYPOTHESIS" }],
      alternative_hypotheses: [
        {
          taxonomy_label: "db_latency_increase",
          description: "Slow database",
          confidence: 0.15,
          rejected_because: "DB latency stayed flat",
          label: "HYPOTHESIS",
        },
      ],
      historical_influence: { used: false, incident_ids: [], how: "" },
      impact_analysis: {
        services: ["ecs", "alb"],
        resources: ["ecs/service/web", "alb/web-lb"],
        blast_radius: "web tier",
        user_impact: "Requests fail",
      },
      severity_suggestion: "HIGH",
      recommendations: [
        { category: "IMMEDIATE", action: "Roll back to web:41", label: "RECOMMENDATION" },
      ],
      missing_information: [],
      requires_human_review: false,
    },
    ...over,
  };
}

export function incident(over: Partial<Incident> = {}): Incident {
  return {
    id: "inc-0123456789",
    title: "ALARM: 5XX on alb/web-lb",
    description: "Users see errors",
    status: "DETECTED",
    severity: null,
    source: "replay",
    affected_resources: ["alb/web-lb"],
    alarm_time: "2025-03-01T12:03:00Z",
    window_start: "2025-03-01T11:00:00Z",
    window_end: "2025-03-01T12:30:00Z",
    created_at: "2026-10-07T12:00:00Z",
    allowed_transitions: ["INVESTIGATING", "CLOSED"],
    timings: {
      detected_at: "2025-03-01T12:03:00Z",
      onset_at: "2025-03-01T11:59:00Z",
      diagnosed_at: null,
      resolved_at: null,
      closed_at: null,
      time_to_detect_min: 4,
      time_to_diagnose_min: null,
      time_to_resolve_min: null,
    },
    transitions: [
      { from: null, to: "DETECTED", at: "2026-10-07T12:00:00Z", actor: "key-abc", note: "incident created" },
    ],
    counts: { events: 900, diagnoses: 0 },
    ...over,
  };
}
