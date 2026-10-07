import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { DiagnosisRecord, Incident, Job } from "../api/types";
import { DiagnosisView } from "./DiagnosisView";
import { Button, Card, Empty, ErrorBox, Loading } from "./ui";

const POLL_MS = 1000;

/** Latest diagnosis plus a button to start a new async job and poll it to completion. */
export function DiagnosisPanel({
  incident,
  onChanged,
  pollMs = POLL_MS,
}: {
  incident: Incident;
  onChanged: () => void;
  pollMs?: number;
}) {
  const [rec, setRec] = useState<DiagnosisRecord | null>();
  const [error, setError] = useState<Error>();
  const [job, setJob] = useState<Job>();

  const load = () =>
    api.diagnosis(incident.id).then(
      (r) => setRec(r),
      (e: unknown) => {
        if (e instanceof ApiError && e.status === 404) setRec(null);
        else setError(e instanceof Error ? e : new Error(String(e)));
      },
    );

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incident.id]);

  useEffect(() => {
    if (!job || job.status === "SUCCEEDED" || job.status === "FAILED") return;
    const t = setTimeout(async () => {
      try {
        const j = await api.job(job.job_id);
        setJob(j);
        if (j.status === "SUCCEEDED" || j.status === "FAILED") {
          await load();
          onChanged();
        }
      } catch (e) {
        setError(e instanceof Error ? e : new Error(String(e)));
      }
    }, pollMs);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job]);

  const start = async () => {
    setError(undefined);
    try {
      setJob(await api.diagnose(incident.id));
    } catch (e) {
      setError(e instanceof Error ? e : new Error(String(e)));
    }
  };
  const running = job && (job.status === "PENDING" || job.status === "RUNNING");

  return (
    <div className="space-y-3">
      <Card
        title="AI diagnosis"
        actions={
          <Button onClick={start} disabled={!!running || incident.status === "CLOSED"}>
            {running ? "Diagnosing…" : rec ? "Re-run diagnosis" : "Run diagnosis"}
          </Button>
        }
      >
        {job && (
          <p className="text-xs text-slate-600" data-testid="job-status">
            Job {job.job_id}: {job.status}
            {job.error ? ` — ${job.error}` : ""}
          </p>
        )}
        <ErrorBox error={error} />
        {rec === undefined && !error && <Loading what="Loading diagnosis" />}
        {rec === null && !running && <Empty>No diagnosis yet. Run one to analyse this incident.</Empty>}
      </Card>
      {rec && (
        <DiagnosisView rec={rec} onsetAt={incident.timings.onset_at} alarmAt={incident.alarm_time} />
      )}
    </div>
  );
}
