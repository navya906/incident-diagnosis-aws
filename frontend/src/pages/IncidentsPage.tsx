import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { Button, Card, Empty, ErrorBox, Loading, SeverityBadge, StatusBadge } from "../components/ui";
import { fmtMinutes, fmtTime } from "../lib/format";
import { useAsync } from "../lib/useAsync";

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-slate-200 bg-white p-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p className="text-lg font-semibold">{value}</p>
    </div>
  );
}

export function IncidentsPage() {
  const incidents = useAsync(() => api.incidents(), []);
  const metrics = useAsync(() => api.lifecycle(), []);
  const offline = useAsync(() => api.offline(), []);
  const [choice, setChoice] = useState("");
  const [importError, setImportError] = useState<Error>();
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  const importIncident = async () => {
    const id = choice || offline.data?.items[0]?.incident_id;
    if (!id) return;
    setBusy(true);
    setImportError(undefined);
    try {
      const inc = await api.importOffline(id);
      navigate(`/incidents/${inc.id}/overview`);
    } catch (e) {
      setImportError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setBusy(false);
    }
  };

  const m = metrics.data;
  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-4" data-testid="lifecycle-metrics">
        <Metric label="incidents" value={m ? String(m.incidents) : "…"} />
        <Metric label="mean time to detect" value={fmtMinutes(m?.mean_time_to_detect_min)} />
        <Metric label="mean time to diagnose" value={fmtMinutes(m?.mean_time_to_diagnose_min)} />
        <Metric label="mean time to resolve" value={fmtMinutes(m?.mean_time_to_resolve_min)} />
      </div>

      <Card title="Import an offline incident (synthetic dataset, dev split)">
        <div className="flex flex-wrap items-center gap-2">
          <select
            aria-label="offline incident"
            className="min-w-[24rem] rounded border border-slate-300 px-2 py-1 text-sm"
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
          >
            {(offline.data?.items ?? []).map((o) => (
              <option key={o.incident_id} value={o.incident_id}>
                {o.incident_id} — {o.title}
              </option>
            ))}
          </select>
          <Button onClick={importIncident} disabled={busy || !offline.data?.items.length}>
            Import
          </Button>
        </div>
        <ErrorBox error={importError ?? offline.error} />
      </Card>

      <Card title="Incidents">
        <ErrorBox error={incidents.error} />
        {incidents.loading && <Loading />}
        {incidents.data && incidents.data.items.length === 0 && (
          <Empty>No incidents yet. Import one above or send a CloudWatch alarm to the webhook.</Empty>
        )}
        {incidents.data && incidents.data.items.length > 0 && (
          <table className="w-full text-sm" data-testid="incident-table">
            <thead className="text-left text-xs text-slate-500">
              <tr>
                <th className="py-1">Incident</th>
                <th>Status</th>
                <th>Severity</th>
                <th>Alarm</th>
                <th>Resources</th>
                <th>To diagnose</th>
              </tr>
            </thead>
            <tbody>
              {incidents.data.items.map((i) => (
                <tr key={i.id} className="border-t border-slate-100">
                  <td className="py-1.5">
                    <Link className="text-sky-700 hover:underline" to={`/incidents/${i.id}/overview`}>
                      {i.title}
                    </Link>
                    <div className="font-mono text-[11px] text-slate-400">{i.id}</div>
                  </td>
                  <td>
                    <StatusBadge status={i.status} />
                  </td>
                  <td>
                    <SeverityBadge level={i.severity} />
                  </td>
                  <td className="font-mono text-xs">{fmtTime(i.alarm_time)}</td>
                  <td className="font-mono text-xs">{i.affected_resources.join(", ")}</td>
                  <td className="text-xs">{fmtMinutes(i.timings.time_to_diagnose_min)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
