import { useMemo, useState } from "react";
import {
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TimelineItem } from "../api/types";
import { fmtClock, fmtTime } from "../lib/format";
import { Empty } from "./ui";

export const CATEGORIES = [
  "log",
  "cloudtrail",
  "config",
  "alarm",
  "anomaly",
  "lifecycle",
] as const;
export type Category = (typeof CATEGORIES)[number];

export function categoryOf(item: TimelineItem): Category {
  if (item.kind === "anomaly") return "anomaly";
  if (item.kind === "transition") return "lifecycle";
  switch (item.source) {
    case "cloudtrail":
      return "cloudtrail";
    case "aws_config":
      return "config";
    case "alarm":
      return "alarm";
    default:
      return "log";
  }
}

export function describe(item: TimelineItem): string {
  if (item.kind === "anomaly")
    return `${item.metric} anomalous on ${item.resource_id}: ${item.observed} vs baseline ${item.baseline.toFixed(3)} (${item.method} ${item.score.toFixed(1)})`;
  if (item.kind === "transition")
    return `${item.from ?? "—"} → ${item.to} by ${item.actor}${item.note ? `: ${item.note}` : ""}`;
  return `${item.resource_id} ${item.event_type}: ${item.message}`;
}

export function filterItems(
  items: TimelineItem[],
  enabled: Set<Category>,
  windowMin: number | null,
  anchor: string | null | undefined,
): TimelineItem[] {
  const a = anchor ? new Date(anchor).getTime() : null;
  return items.filter((it) => {
    if (!enabled.has(categoryOf(it))) return false;
    // Lifecycle transitions happen in real time, not in the telemetry window: always kept.
    if (windowMin === null || a === null || it.kind === "transition") return true;
    return Math.abs(new Date(it.at).getTime() - a) <= windowMin * 60_000;
  });
}

const LANE: Record<Category, number> = { lifecycle: 0, alarm: 1, anomaly: 2, cloudtrail: 3, config: 4, log: 5 };
const COLOR: Record<Category, string> = {
  lifecycle: "#0f172a",
  alarm: "#dc2626",
  anomaly: "#d97706",
  cloudtrail: "#2563eb",
  config: "#7c3aed",
  log: "#64748b",
};

export function Timeline({
  items,
  alarmAt,
  onsetAt,
}: {
  items: TimelineItem[];
  alarmAt?: string | null;
  onsetAt?: string | null;
}) {
  const [enabled, setEnabled] = useState<Set<Category>>(
    new Set(CATEGORIES.filter((c) => c !== "lifecycle")),
  );
  const [windowMin, setWindowMin] = useState<number | null>(30);
  const [selected, setSelected] = useState<number | null>(null);
  const shown = useMemo(
    () => filterItems(items, enabled, windowMin, alarmAt),
    [items, enabled, windowMin, alarmAt],
  );
  const telemetry = shown.filter((it) => it.kind !== "transition");
  const points = telemetry.map((it) => ({
    x: new Date(it.at).getTime(),
    y: LANE[categoryOf(it)],
    idx: shown.indexOf(it),
    category: categoryOf(it),
  }));

  const toggle = (c: Category) =>
    setEnabled((s) => {
      const n = new Set(s);
      if (n.has(c)) n.delete(c);
      else n.add(c);
      return n;
    });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {CATEGORIES.map((c) => (
          <label key={c} className="flex items-center gap-1 rounded border border-slate-200 px-2 py-1">
            <input type="checkbox" checked={enabled.has(c)} onChange={() => toggle(c)} aria-label={`show ${c}`} />
            <span style={{ color: COLOR[c] }}>{c}</span>
          </label>
        ))}
        <select
          aria-label="time window"
          className="rounded border border-slate-300 px-2 py-1"
          value={windowMin ?? "all"}
          onChange={(e) => setWindowMin(e.target.value === "all" ? null : Number(e.target.value))}
        >
          <option value="5">±5 min around the alarm</option>
          <option value="15">±15 min</option>
          <option value="30">±30 min</option>
          <option value="all">whole collection window</option>
        </select>
        <span className="text-slate-500" data-testid="timeline-count">
          {shown.length} items
        </span>
      </div>

      {points.length > 0 && (
        <div className="h-44 rounded border border-slate-200 bg-white">
          <ResponsiveContainer width="100%" height="100%">
            <ScatterChart margin={{ top: 10, right: 20, bottom: 10, left: 10 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="x"
                type="number"
                domain={["dataMin", "dataMax"]}
                tickFormatter={(v: number) => fmtClock(new Date(v).toISOString())}
                fontSize={10}
              />
              <YAxis
                dataKey="y"
                type="number"
                domain={[0, 5]}
                ticks={[1, 2, 3, 4, 5]}
                tickFormatter={(v: number) =>
                  (Object.keys(LANE) as Category[]).find((k) => LANE[k] === v) ?? ""
                }
                fontSize={10}
                width={70}
              />
              <Tooltip
                formatter={() => ""}
                labelFormatter={() => ""}
                content={({ payload }) => {
                  const p = payload?.[0]?.payload as { idx: number } | undefined;
                  const it = p ? shown[p.idx] : undefined;
                  return it ? (
                    <div className="max-w-xs rounded bg-white p-2 text-xs shadow">{describe(it)}</div>
                  ) : null;
                }}
              />
              {onsetAt && <ReferenceLine x={new Date(onsetAt).getTime()} stroke="#d97706" label="onset" />}
              {alarmAt && <ReferenceLine x={new Date(alarmAt).getTime()} stroke="#dc2626" label="alarm" />}
              {CATEGORIES.filter((c) => c !== "lifecycle").map((c) => (
                <Scatter
                  key={c}
                  name={c}
                  data={points.filter((p) => p.category === c)}
                  fill={COLOR[c]}
                  isAnimationActive={false}
                  onClick={(p: { payload?: { idx: number } }) =>
                    p.payload && setSelected(p.payload.idx)
                  }
                />
              ))}
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      )}

      {shown.length === 0 ? (
        <Empty>Nothing matches the filters.</Empty>
      ) : (
        <ol className="max-h-[480px] space-y-1 overflow-auto text-xs" data-testid="timeline-list">
          {shown.map((it, i) => (
            <li
              key={`${it.kind}-${it.at}-${i}`}
              className={`cursor-pointer rounded px-2 py-1 ${selected === i ? "bg-sky-50 ring-1 ring-sky-300" : "hover:bg-slate-50"}`}
              onClick={() => setSelected(selected === i ? null : i)}
            >
              <span className="font-mono text-slate-500">{fmtClock(it.at)}</span>{" "}
              <span className="font-semibold" style={{ color: COLOR[categoryOf(it)] }}>
                {categoryOf(it)}
              </span>{" "}
              {describe(it)}
              {selected === i && (
                <pre className="mt-1 whitespace-pre-wrap rounded bg-slate-50 p-2 text-[11px]">
                  {fmtTime(it.at)}
                  {"\n"}
                  {JSON.stringify(it.kind === "event" ? it.metadata : it, null, 2)}
                </pre>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
