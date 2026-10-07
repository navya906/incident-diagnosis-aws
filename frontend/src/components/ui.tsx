import type { ReactNode } from "react";

export function Card({
  title,
  children,
  actions,
  testId,
}: {
  title?: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
  testId?: string;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm" data-testid={testId}>
      {(title || actions) && (
        <div className="mb-3 flex items-center justify-between gap-2">
          {title && <h2 className="text-sm font-semibold text-slate-700">{title}</h2>}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

const LABEL_STYLE: Record<string, string> = {
  FACT: "bg-emerald-100 text-emerald-800 ring-emerald-300",
  INFERENCE: "bg-sky-100 text-sky-800 ring-sky-300",
  HYPOTHESIS: "bg-amber-100 text-amber-800 ring-amber-300",
  RECOMMENDATION: "bg-violet-100 text-violet-800 ring-violet-300",
  CONTRADICTS: "bg-rose-100 text-rose-800 ring-rose-300",
};

/** Claim label shown next to every statement of the diagnosis. */
export function ClaimLabel({ label }: { label: string }) {
  return (
    <span
      data-testid="claim-label"
      className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-bold tracking-wide ring-1 ${
        LABEL_STYLE[label] ?? "bg-slate-100 text-slate-700 ring-slate-300"
      }`}
    >
      {label}
    </span>
  );
}

const STATUS_STYLE: Record<string, string> = {
  DETECTED: "bg-rose-100 text-rose-800",
  INVESTIGATING: "bg-amber-100 text-amber-800",
  DIAGNOSED: "bg-sky-100 text-sky-800",
  MITIGATING: "bg-violet-100 text-violet-800",
  RESOLVED: "bg-emerald-100 text-emerald-800",
  CLOSED: "bg-slate-200 text-slate-700",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLE[status] ?? ""}`}>
      {status}
    </span>
  );
}

const SEV_STYLE: Record<string, string> = {
  LOW: "bg-slate-100 text-slate-700",
  MEDIUM: "bg-yellow-100 text-yellow-800",
  HIGH: "bg-orange-100 text-orange-800",
  CRITICAL: "bg-red-600 text-white",
};

export function SeverityBadge({ level }: { level: string | null | undefined }) {
  if (!level) return <span className="text-xs text-slate-400">no severity yet</span>;
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-semibold ${SEV_STYLE[level] ?? ""}`}>{level}</span>
  );
}

export function ErrorBox({ error }: { error: Error | undefined }) {
  if (!error) return null;
  return (
    <div role="alert" className="rounded border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
      {error.message}
    </div>
  );
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <p className="text-sm text-slate-500">{what}…</p>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="text-sm italic text-slate-500">{children}</p>;
}

export function Button({
  children,
  onClick,
  disabled,
  kind = "primary",
  type = "button",
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  kind?: "primary" | "secondary";
  type?: "button" | "submit";
}) {
  const style =
    kind === "primary"
      ? "bg-slate-900 text-white hover:bg-slate-700"
      : "border border-slate-300 bg-white text-slate-800 hover:bg-slate-100";
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`rounded px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50 ${style}`}
    >
      {children}
    </button>
  );
}

export function SyntheticNote() {
  return (
    <span className="rounded bg-yellow-50 px-2 py-0.5 text-[11px] text-yellow-800 ring-1 ring-yellow-300">
      smoke-test / synthetic
    </span>
  );
}
