// Thin typed client for the backend API. The API key lives in sessionStorage only (cleared when
// the tab closes) and is sent as X-API-Key; it is never logged or put in a URL.
import type {
  DiagnosisRecord,
  EventItem,
  GraphData,
  Incident,
  Job,
  LifecycleMetrics,
  OfflineIncident,
  Page,
  Series,
  Status,
  TimelineItem,
} from "./types";

const KEY = "clouddiag.apiKey";

export const apiKey = {
  get: (): string | null => {
    try {
      return sessionStorage.getItem(KEY);
    } catch {
      return null;
    }
  },
  set: (k: string) => sessionStorage.setItem(KEY, k),
  clear: () => sessionStorage.removeItem(KEY),
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function describe(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail
      .map((d) => {
        const e = d as { loc?: unknown[]; msg?: string };
        return `${(e.loc ?? []).join(".")}: ${e.msg ?? ""}`;
      })
      .join("; ");
  return "request failed";
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const key = apiKey.get();
  if (key) headers.set("X-API-Key", key);
  if (init.body) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      detail = ((await res.json()) as { detail?: unknown }).detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, describe(detail));
  }
  return (await res.json()) as T;
}

function qs(params: Record<string, string | number | undefined | null | string[]>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => u.append(k, x));
    else u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

export const api = {
  incidents: (status?: Status) =>
    request<Page<Incident>>(`/api/incidents${qs({ status, limit: 200 })}`),
  incident: (id: string) => request<Incident>(`/api/incidents/${encodeURIComponent(id)}`),
  transition: (id: string, to: Status, note = "") =>
    request<Incident>(`/api/incidents/${encodeURIComponent(id)}/transitions`, {
      method: "POST",
      body: JSON.stringify({ to, note }),
    }),
  events: (
    id: string,
    p: { source?: string[]; q?: string; severity?: string; resource_id?: string; limit?: number; offset?: number },
  ) => request<Page<EventItem>>(`/api/incidents/${encodeURIComponent(id)}/events${qs(p)}`),
  logs: (id: string, p: { q?: string; severity?: string; limit?: number; offset?: number }) =>
    request<Page<EventItem>>(`/api/incidents/${encodeURIComponent(id)}/logs${qs(p)}`),
  cloudtrail: (id: string, p: { q?: string; limit?: number; offset?: number }) =>
    request<Page<EventItem>>(`/api/incidents/${encodeURIComponent(id)}/cloudtrail${qs(p)}`),
  metrics: (id: string) =>
    request<{ series: Series[] }>(`/api/incidents/${encodeURIComponent(id)}/metrics`),
  timeline: (id: string) =>
    request<{ items: TimelineItem[] }>(`/api/incidents/${encodeURIComponent(id)}/timeline?limit=2000`),
  graph: (id: string) => request<GraphData>(`/api/incidents/${encodeURIComponent(id)}/graph`),
  diagnosis: (id: string) =>
    request<DiagnosisRecord>(`/api/incidents/${encodeURIComponent(id)}/diagnosis`),
  diagnose: (id: string, condition = "Full") =>
    request<Job>(`/api/incidents/${encodeURIComponent(id)}/diagnose`, {
      method: "POST",
      body: JSON.stringify({ condition }),
    }),
  job: (jobId: string) => request<Job>(`/api/jobs/${encodeURIComponent(jobId)}`),
  lifecycle: () => request<LifecycleMetrics>("/api/metrics/lifecycle"),
  offline: () => request<{ items: OfflineIncident[] }>("/api/offline/incidents?split=dev"),
  importOffline: (offlineId: string) =>
    request<Incident & { created: boolean }>("/api/offline/import", {
      method: "POST",
      body: JSON.stringify({ offline_incident_id: offlineId }),
    }),
};
