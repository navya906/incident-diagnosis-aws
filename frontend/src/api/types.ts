// Shapes returned by the backend API (docs/api.md). Only the fields the UI uses are typed.

export type Status =
  | "DETECTED"
  | "INVESTIGATING"
  | "DIAGNOSED"
  | "MITIGATING"
  | "RESOLVED"
  | "CLOSED";

export interface Timings {
  detected_at: string | null;
  onset_at: string | null;
  diagnosed_at: string | null;
  resolved_at: string | null;
  closed_at: string | null;
  time_to_detect_min: number | null;
  time_to_diagnose_min: number | null;
  time_to_resolve_min: number | null;
}

export interface Transition {
  from: Status | null;
  to: Status;
  at: string;
  actor: string;
  note: string;
}

export interface Incident {
  id: string;
  title: string;
  description: string;
  status: Status;
  severity: string | null;
  source: string;
  affected_resources: string[];
  alarm_time: string | null;
  window_start: string | null;
  window_end: string | null;
  created_at: string;
  timings: Timings;
  allowed_transitions: Status[];
  transitions?: Transition[];
  counts?: { events: number; diagnoses: number };
}

export interface EventItem {
  event_id: string;
  timestamp: string;
  source: "cloudwatch_metric" | "cloudwatch_log" | "cloudtrail" | "aws_config" | "alarm";
  service: string;
  resource_id: string;
  event_type: string;
  metric: string | null;
  value: number | null;
  severity: string;
  message: string;
  metadata: Record<string, unknown>;
}

export interface Page<T> {
  items: T[];
  total?: number;
  limit?: number;
  offset?: number;
}

export interface Series {
  resource_id: string;
  metric: string;
  points: { t: string; v: number }[];
  anomalies: { t: string; score: number; baseline: number }[];
}

export type TimelineItem =
  | ({ kind: "event"; at: string } & EventItem)
  | {
      kind: "anomaly";
      at: string;
      resource_id: string;
      metric: string;
      score: number;
      baseline: number;
      observed: number;
      method: string;
    }
  | { kind: "transition"; at: string; from: Status | null; to: Status; actor: string; note: string };

export interface GraphNode {
  id: string;
  node_type: string;
  affected: boolean;
  upstream: boolean;
  downstream: boolean;
  can_cause: boolean;
  [k: string]: unknown;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: { source: string; target: string; edge_type: string }[];
}

export type ClaimLabel = "FACT" | "INFERENCE" | "HYPOTHESIS" | "RECOMMENDATION";

export interface Diagnosis {
  incident_summary: string;
  root_cause: {
    taxonomy_label: string;
    description: string;
    confidence: number;
    resource_id: string;
    label: "INFERENCE";
  };
  supporting_evidence: { evidence_id: string; explanation: string; label: "FACT" | "INFERENCE" }[];
  contradicting_evidence: { evidence_id: string; explanation: string }[];
  contributing_factors: { description: string; label: "FACT" | "INFERENCE" | "HYPOTHESIS" }[];
  alternative_hypotheses: {
    taxonomy_label: string;
    description: string;
    confidence: number;
    rejected_because: string;
    label: "HYPOTHESIS";
  }[];
  historical_influence: { used: boolean; incident_ids: string[]; how: string };
  impact_analysis: {
    services: string[];
    resources: string[];
    blast_radius: string;
    user_impact: string;
  };
  severity_suggestion: string;
  recommendations: { category: string; action: string; label: "RECOMMENDATION" }[];
  missing_information: string[];
  requires_human_review: boolean;
}

export interface SeverityFactor {
  name: string;
  value: number | null;
  unit: string;
  points: number;
  known: boolean;
  detail: string;
}

export interface DiagnosisRecord {
  id: number;
  incident_id: string;
  condition: string;
  model: string;
  prompt_version: string;
  valid: boolean;
  created_at: string;
  diagnosis: Diagnosis | null;
  severity: {
    level: string;
    score: number;
    criticality: string;
    factors: SeverityFactor[];
    rationale: string;
  };
  llm_severity_suggestion: string | null;
  self_consistency: { samples: number; agreement_with_primary: number | null };
  verbalized_confidence: number | null;
  evidence_events: Record<string, EventItem>;
  dependency_path: string[];
  retrieved_incident_ids: string[];
  label: string;
  failure: { stage: string; errors: string[] } | null;
}

export interface Job {
  job_id: string;
  incident_id: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  diagnosis_id: number | null;
  error: string | null;
}

export interface LifecycleMetrics {
  incidents: number;
  by_status: Record<string, number>;
  mean_time_to_detect_min: number | null;
  mean_time_to_diagnose_min: number | null;
  mean_time_to_resolve_min: number | null;
}

export interface OfflineIncident {
  incident_id: string;
  split: "dev" | "test";
  title: string;
  alarm_time: string;
}
