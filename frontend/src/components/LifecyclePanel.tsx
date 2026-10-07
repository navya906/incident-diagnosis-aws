import { useState } from "react";
import type { Incident, Status } from "../api/types";
import { fmtMinutes, fmtTime } from "../lib/format";
import { Button, Card, StatusBadge } from "./ui";

export function LifecyclePanel({
  incident,
  onTransition,
}: {
  incident: Incident;
  onTransition: (to: Status, note: string) => Promise<void>;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const t = incident.timings;

  const go = async (to: Status) => {
    setBusy(true);
    setError(undefined);
    try {
      await onTransition(to, note);
      setNote("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Lifecycle" testId="lifecycle">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm">Status</span>
        <StatusBadge status={incident.status} />
      </div>
      <dl className="mt-3 grid grid-cols-3 gap-2 text-xs">
        <div>
          <dt className="text-slate-500">time to detect</dt>
          <dd data-testid="ttd">{fmtMinutes(t.time_to_detect_min)}</dd>
        </div>
        <div>
          <dt className="text-slate-500">time to diagnose</dt>
          <dd data-testid="ttdiag">{fmtMinutes(t.time_to_diagnose_min)}</dd>
        </div>
        <div>
          <dt className="text-slate-500">time to resolve</dt>
          <dd data-testid="ttr">{fmtMinutes(t.time_to_resolve_min)}</dd>
        </div>
      </dl>
      {incident.allowed_transitions.length > 0 && (
        <div className="mt-3 space-y-2">
          <input
            aria-label="transition note"
            placeholder="Note (required to close an unresolved incident)"
            className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <div className="flex flex-wrap gap-2">
            {incident.allowed_transitions.map((s) => (
              <Button key={s} kind="secondary" disabled={busy} onClick={() => go(s)}>
                Move to {s}
              </Button>
            ))}
          </div>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-2 text-sm text-rose-700">
          {error}
        </p>
      )}
      {incident.transitions && (
        <ol className="mt-3 space-y-1 border-t border-slate-100 pt-2 text-xs" data-testid="transitions">
          {incident.transitions.map((tr, i) => (
            <li key={i}>
              <span className="font-mono text-slate-500">{fmtTime(tr.at)}</span> {tr.from ?? "—"} →{" "}
              <strong>{tr.to}</strong> <span className="text-slate-500">by {tr.actor}</span>
              {tr.note && <span className="text-slate-600"> · {tr.note}</span>}
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
