import { NavLink, Navigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Incident, Status } from "../api/types";
import { DependencyGraph } from "../components/DependencyGraph";
import { DiagnosisPanel } from "../components/DiagnosisPanel";
import { EventSearch } from "../components/EventSearch";
import { LifecyclePanel } from "../components/LifecyclePanel";
import { MetricsPanel } from "../components/MetricsPanel";
import { Timeline } from "../components/Timeline";
import { Card, ErrorBox, Loading, SeverityBadge, StatusBadge } from "../components/ui";
import { fmtTime } from "../lib/format";
import { useAsync } from "../lib/useAsync";

export const TABS = [
  "overview",
  "diagnosis",
  "timeline",
  "metrics",
  "logs",
  "cloudtrail",
  "graph",
] as const;
type Tab = (typeof TABS)[number];

function TabBody({ tab, id, inc, refresh }: { tab: Tab; id: string; inc: Incident; refresh: () => void }) {
  const timeline = useAsync(() => (tab === "timeline" ? api.timeline(id) : Promise.resolve(null)), [id, tab]);
  const metrics = useAsync(() => (tab === "metrics" ? api.metrics(id) : Promise.resolve(null)), [id, tab]);
  const graph = useAsync(() => (tab === "graph" ? api.graph(id) : Promise.resolve(null)), [id, tab]);

  switch (tab) {
    case "overview":
      return (
        <div className="grid gap-3 lg:grid-cols-2">
          <Card title="Incident">
            <p className="text-sm">{inc.description}</p>
            <dl className="mt-3 grid grid-cols-2 gap-1 text-xs">
              <dt className="text-slate-500">alarm</dt>
              <dd className="font-mono">{fmtTime(inc.alarm_time)}</dd>
              <dt className="text-slate-500">estimated onset</dt>
              <dd className="font-mono">{fmtTime(inc.timings.onset_at)}</dd>
              <dt className="text-slate-500">window</dt>
              <dd className="font-mono">
                {fmtTime(inc.window_start)} → {fmtTime(inc.window_end)}
              </dd>
              <dt className="text-slate-500">affected</dt>
              <dd className="font-mono">{inc.affected_resources.join(", ")}</dd>
              <dt className="text-slate-500">source</dt>
              <dd>{inc.source}</dd>
              <dt className="text-slate-500">events / diagnoses</dt>
              <dd>
                {inc.counts?.events ?? 0} / {inc.counts?.diagnoses ?? 0}
              </dd>
            </dl>
            <p className="mt-3 text-xs text-slate-500">
              Conclusions are shown only on the Diagnosis tab, next to the evidence they rest on.
            </p>
          </Card>
          <LifecyclePanel
            incident={inc}
            onTransition={async (to: Status, note: string) => {
              await api.transition(id, to, note);
              refresh();
            }}
          />
        </div>
      );
    case "diagnosis":
      return <DiagnosisPanel incident={inc} onChanged={refresh} />;
    case "timeline":
      return (
        <Card title="Timeline">
          <ErrorBox error={timeline.error} />
          {timeline.data ? (
            <Timeline items={timeline.data.items} alarmAt={inc.alarm_time} onsetAt={inc.timings.onset_at} />
          ) : (
            <Loading />
          )}
        </Card>
      );
    case "metrics":
      return (
        <Card title="Metrics around the incident window">
          <ErrorBox error={metrics.error} />
          {metrics.data ? (
            <MetricsPanel
              series={metrics.data.series}
              alarmAt={inc.alarm_time}
              onsetAt={inc.timings.onset_at}
              affected={inc.affected_resources}
            />
          ) : (
            <Loading />
          )}
        </Card>
      );
    case "logs":
      return (
        <Card title="Logs">
          <EventSearch kind="logs" fetchPage={(p) => api.logs(id, p)} />
        </Card>
      );
    case "cloudtrail":
      return (
        <Card title="CloudTrail and configuration changes">
          <EventSearch kind="cloudtrail" fetchPage={(p) => api.cloudtrail(id, p)} />
        </Card>
      );
    case "graph":
      return (
        <Card title="Dependency graph">
          <ErrorBox error={graph.error} />
          {graph.data ? <DependencyGraph data={graph.data} /> : <Loading />}
        </Card>
      );
  }
}

export function IncidentPage() {
  const { id = "", tab = "overview" } = useParams();
  const header = useAsync(() => api.incident(id), [id]);
  if (!(TABS as readonly string[]).includes(tab)) return <Navigate to={`/incidents/${id}/overview`} replace />;
  const inc = header.data;
  return (
    <div className="space-y-4">
      <div>
        <NavLink to="/incidents" className="text-xs text-sky-700 hover:underline">
          ← all incidents
        </NavLink>
        <div className="mt-1 flex flex-wrap items-center gap-2" data-testid="incident-header">
          <h1 className="text-xl font-semibold">{inc?.title ?? id}</h1>
          {inc && <StatusBadge status={inc.status} />}
          {inc && <SeverityBadge level={inc.severity} />}
        </div>
        <p className="font-mono text-xs text-slate-400">{id}</p>
      </div>
      <nav className="flex flex-wrap gap-1 border-b border-slate-200" aria-label="incident sections">
        {TABS.map((t) => (
          <NavLink
            key={t}
            to={`/incidents/${id}/${t}`}
            className={({ isActive }) =>
              `rounded-t px-3 py-1.5 text-sm ${isActive ? "border border-b-white border-slate-200 bg-white font-medium" : "text-slate-600 hover:text-slate-900"}`
            }
          >
            {t === "cloudtrail" ? "CloudTrail" : t[0]!.toUpperCase() + t.slice(1)}
          </NavLink>
        ))}
      </nav>
      <ErrorBox error={header.error} />
      {inc ? <TabBody tab={tab as Tab} id={id} inc={inc} refresh={header.reload} /> : !header.error && <Loading />}
    </div>
  );
}
