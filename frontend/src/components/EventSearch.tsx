import { useState, type FormEvent } from "react";
import type { EventItem, Page } from "../api/types";
import { fmtTime } from "../lib/format";
import { useAsync } from "../lib/useAsync";
import { Button, Empty, ErrorBox, Loading } from "./ui";

const PAGE = 100;

export interface SearchParams {
  q?: string;
  severity?: string;
  limit: number;
  offset: number;
}

/** Searchable, paged table used for logs and for CloudTrail/Config changes. */
export function EventSearch({
  fetchPage,
  kind,
}: {
  fetchPage: (p: SearchParams) => Promise<Page<EventItem>>;
  kind: "logs" | "cloudtrail";
}) {
  const [draft, setDraft] = useState("");
  const [q, setQ] = useState("");
  const [severity, setSeverity] = useState("");
  const [offset, setOffset] = useState(0);
  const { data, error, loading } = useAsync(
    () => fetchPage({ q: q || undefined, severity: severity || undefined, limit: PAGE, offset }),
    [q, severity, offset],
  );

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setOffset(0);
    setQ(draft.trim());
  };

  return (
    <div className="space-y-3">
      <form onSubmit={submit} className="flex flex-wrap items-center gap-2" role="search">
        <input
          aria-label={`search ${kind}`}
          placeholder={kind === "logs" ? "Search log messages…" : "Search API calls and changes…"}
          className="w-72 rounded border border-slate-300 px-2 py-1 text-sm"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        {kind === "logs" && (
          <select
            aria-label="severity"
            className="rounded border border-slate-300 px-2 py-1 text-sm"
            value={severity}
            onChange={(e) => {
              setOffset(0);
              setSeverity(e.target.value);
            }}
          >
            <option value="">all severities</option>
            <option>CRITICAL</option>
            <option>ERROR</option>
            <option>WARNING</option>
            <option>INFO</option>
          </select>
        )}
        <Button type="submit" kind="secondary">
          Search
        </Button>
        {data && (
          <span className="text-xs text-slate-500" data-testid="result-count">
            {data.total ?? data.items.length} results
          </span>
        )}
      </form>
      <ErrorBox error={error} />
      {loading && <Loading />}
      {data && data.items.length === 0 && <Empty>No matching events.</Empty>}
      {data && data.items.length > 0 && (
        <div className="overflow-auto rounded border border-slate-200 bg-white">
          <table className="w-full text-xs">
            <thead className="bg-slate-50 text-left text-slate-500">
              <tr>
                <th className="px-2 py-1">Time</th>
                <th className="px-2 py-1">Resource</th>
                {kind === "logs" ? (
                  <th className="px-2 py-1">Severity</th>
                ) : (
                  <>
                    <th className="px-2 py-1">Call / change</th>
                    <th className="px-2 py-1">By</th>
                  </>
                )}
                <th className="px-2 py-1">Message</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((e) => (
                <tr key={e.event_id} className="border-t border-slate-100 align-top" data-testid="event-row">
                  <td className="whitespace-nowrap px-2 py-1 font-mono">{fmtTime(e.timestamp)}</td>
                  <td className="px-2 py-1 font-mono">{e.resource_id}</td>
                  {kind === "logs" ? (
                    <td className="px-2 py-1">{e.severity}</td>
                  ) : (
                    <>
                      <td className="px-2 py-1">
                        {e.source === "aws_config" ? "Config change" : e.event_type}
                        {typeof e.metadata.errorCode === "string" && (
                          <span className="ml-1 rounded bg-rose-100 px-1 text-rose-800">
                            {e.metadata.errorCode}
                          </span>
                        )}
                      </td>
                      <td className="px-2 py-1 font-mono">{String(e.metadata.userIdentity ?? "—")}</td>
                    </>
                  )}
                  <td className="px-2 py-1">{e.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && (data.total ?? 0) > PAGE && (
        <div className="flex gap-2">
          <Button kind="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            Previous
          </Button>
          <Button
            kind="secondary"
            disabled={offset + PAGE >= (data.total ?? 0)}
            onClick={() => setOffset(offset + PAGE)}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  );
}
