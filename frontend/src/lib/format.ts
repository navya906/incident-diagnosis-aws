export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, "Z");
}

export function fmtClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().slice(11, 19);
}

export function fmtMinutes(m: number | null | undefined): string {
  if (m === null || m === undefined) return "—";
  if (Math.abs(m) >= 60 * 24) return `${(m / 60 / 24).toFixed(1)} d`;
  if (Math.abs(m) >= 60) return `${(m / 60).toFixed(1)} h`;
  return `${m.toFixed(1)} min`;
}

export function shortId(resourceId: string): string {
  return resourceId.split("/").slice(-1)[0] ?? resourceId;
}
