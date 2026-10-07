import { useMemo, useState } from "react";
import {
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Series } from "../api/types";
import { fmtClock } from "../lib/format";
import { Empty } from "./ui";

export function resourcesOf(series: Series[]): string[] {
  return [...new Set(series.map((s) => s.resource_id))].sort();
}

/** Points of one series, with anomaly flags merged in by timestamp. */
export function chartData(s: Series) {
  const flagged = new Map(s.anomalies.map((a) => [a.t, a]));
  return s.points.map((p) => ({
    t: new Date(p.t).getTime(),
    v: p.v,
    anomaly: flagged.has(p.t) ? p.v : null,
  }));
}

function SeriesChart({
  s,
  alarmAt,
  onsetAt,
  windowStart,
  windowEnd,
}: {
  s: Series;
  alarmAt?: string | null;
  onsetAt?: string | null;
  windowStart?: number;
  windowEnd?: number;
}) {
  const data = chartData(s);
  return (
    <div className="rounded border border-slate-200 bg-white p-2" data-testid="metric-chart">
      <p className="mb-1 text-xs font-medium">
        {s.metric} <span className="text-slate-400">({s.anomalies.length} anomalous points)</span>
      </p>
      <div className="h-40">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} margin={{ top: 5, right: 10, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis
              dataKey="t"
              type="number"
              domain={[windowStart ?? "dataMin", windowEnd ?? "dataMax"]}
              tickFormatter={(v: number) => fmtClock(new Date(v).toISOString())}
              fontSize={10}
            />
            <YAxis fontSize={10} width={50} />
            <Tooltip labelFormatter={(v) => fmtClock(new Date(Number(v)).toISOString())} />
            {onsetAt && alarmAt && (
              <ReferenceArea
                x1={new Date(onsetAt).getTime()}
                x2={new Date(alarmAt).getTime()}
                fill="#fef3c7"
                fillOpacity={0.6}
              />
            )}
            {alarmAt && <ReferenceLine x={new Date(alarmAt).getTime()} stroke="#dc2626" />}
            <Line type="linear" dataKey="v" dot={false} stroke="#334155" isAnimationActive={false} />
            <Scatter dataKey="anomaly" fill="#dc2626" isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export function MetricsPanel({
  series,
  alarmAt,
  onsetAt,
  affected,
}: {
  series: Series[];
  alarmAt?: string | null;
  onsetAt?: string | null;
  affected: string[];
}) {
  const resources = useMemo(() => resourcesOf(series), [series]);
  const [resource, setResource] = useState<string>(
    resources.find((r) => affected.includes(r)) ?? resources[0] ?? "",
  );
  const [aroundMin, setAroundMin] = useState<number | null>(30);
  if (series.length === 0) return <Empty>No metrics stored for this incident.</Empty>;
  const a = alarmAt ? new Date(alarmAt).getTime() : undefined;
  const windowStart = a !== undefined && aroundMin !== null ? a - aroundMin * 60_000 : undefined;
  const windowEnd = a !== undefined && aroundMin !== null ? a + aroundMin * 60_000 : undefined;
  const shown = series
    .filter((s) => s.resource_id === resource)
    .map((s) =>
      windowStart === undefined
        ? s
        : {
            ...s,
            points: s.points.filter((p) => {
              const t = new Date(p.t).getTime();
              return t >= windowStart && t <= windowEnd!;
            }),
          },
    );

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <label htmlFor="metric-resource">Resource</label>
        <select
          id="metric-resource"
          className="rounded border border-slate-300 px-2 py-1"
          value={resource}
          onChange={(e) => setResource(e.target.value)}
        >
          {resources.map((r) => (
            <option key={r} value={r}>
              {r}
              {affected.includes(r) ? " (alarmed)" : ""}
            </option>
          ))}
        </select>
        <select
          aria-label="window around the alarm"
          className="rounded border border-slate-300 px-2 py-1"
          value={aroundMin ?? "all"}
          onChange={(e) => setAroundMin(e.target.value === "all" ? null : Number(e.target.value))}
        >
          <option value="15">±15 min around the alarm</option>
          <option value="30">±30 min</option>
          <option value="60">±60 min</option>
          <option value="all">whole collection window</option>
        </select>
        <span className="text-xs text-slate-500">
          red dots: anomalous points; shaded: estimated onset → alarm
        </span>
      </div>
      <div className="grid gap-3 md:grid-cols-2" data-testid="metric-grid">
        {shown.map((s) => (
          <SeriesChart
            key={`${s.resource_id}/${s.metric}`}
            s={s}
            alarmAt={alarmAt}
            onsetAt={onsetAt}
            windowStart={windowStart}
            windowEnd={windowEnd}
          />
        ))}
      </div>
    </div>
  );
}
