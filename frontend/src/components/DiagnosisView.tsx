import type { DiagnosisRecord, EventItem } from "../api/types";
import { assessDiagnosis, evidenceTimeline, pct, type ResolvedClaim } from "../lib/diagnosis";
import { fmtClock, fmtTime } from "../lib/format";
import { Card, ClaimLabel, Empty, SeverityBadge, SyntheticNote } from "./ui";

type TimelineRow =
  | { kind: "marker"; at: string; text: string }
  | { kind: "claim"; at: string; claim: ResolvedClaim };

/** Cited evidence with the onset estimate and the alarm placed at their own times. */
export function withMarkers(
  claims: ResolvedClaim[],
  onsetAt?: string | null,
  alarmAt?: string | null,
): TimelineRow[] {
  const rows: TimelineRow[] = claims.map((c) => ({ kind: "claim", at: c.event!.timestamp, claim: c }));
  if (onsetAt) rows.push({ kind: "marker", at: onsetAt, text: "estimated onset" });
  if (alarmAt) rows.push({ kind: "marker", at: alarmAt, text: "alarm" });
  return rows.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
}

function EventLine({ event }: { event: EventItem }) {
  const body =
    event.source === "cloudwatch_metric"
      ? `${event.metric} = ${event.value}`
      : `${event.event_type}: ${event.message}`;
  return (
    <div className="mt-1 rounded bg-slate-50 px-2 py-1 font-mono text-xs text-slate-700">
      <span className="text-slate-500">{fmtTime(event.timestamp)}</span>{" "}
      <span className="font-semibold">{event.resource_id}</span>{" "}
      <span className="text-slate-500">[{event.source}]</span> {body}
    </div>
  );
}

function Claim({ claim }: { claim: ResolvedClaim }) {
  return (
    <li className="rounded border border-slate-200 p-2" data-testid="evidence-claim">
      <div className="flex items-start gap-2">
        <ClaimLabel label={claim.label} />
        <p className="text-sm">{claim.explanation}</p>
      </div>
      <p className="mt-1 font-mono text-[11px] text-slate-400">{claim.evidence_id}</p>
      {claim.event ? (
        <EventLine event={claim.event} />
      ) : (
        <p className="mt-1 text-xs text-rose-700">Cited evidence not found for this incident.</p>
      )}
    </li>
  );
}

function Header({ rec }: { rec: DiagnosisRecord }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
      <span>
        {rec.model} · prompt {rec.prompt_version} · condition {rec.condition} · {fmtTime(rec.created_at)}
      </span>
      {rec.label === "smoke-test / synthetic" && <SyntheticNote />}
    </div>
  );
}

function Severity({ rec }: { rec: DiagnosisRecord }) {
  const s = rec.severity;
  return (
    <Card title="Severity" testId="severity">
      <div className="flex items-center gap-3">
        <SeverityBadge level={s.level} />
        <span className="text-xs text-slate-500">
          deterministic engine, score {s.score}, {s.criticality} business criticality
        </span>
      </div>
      <table className="mt-2 w-full text-xs">
        <tbody>
          {s.factors.map((f) => (
            <tr key={f.name} className="border-t border-slate-100">
              <td className="py-1 pr-2 font-medium">{f.name}</td>
              <td className="pr-2">{f.known ? `${f.value} ${f.unit}` : "unknown"}</td>
              <td className="pr-2">+{f.points}</td>
              <td className="text-slate-500">{f.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rec.llm_severity_suggestion && (
        <p className="mt-2 text-xs text-slate-500" data-testid="advisory-severity">
          The model suggested {rec.llm_severity_suggestion} (advisory only; the engine decides).
        </p>
      )}
    </Card>
  );
}

export function DiagnosisView({
  rec,
  onsetAt,
  alarmAt,
}: {
  rec: DiagnosisRecord;
  onsetAt?: string | null;
  alarmAt?: string | null;
}) {
  const verdict = assessDiagnosis(rec);

  if (verdict.kind === "invalid") {
    return (
      <div className="space-y-3" data-testid="diagnosis-invalid">
        <Header rec={rec} />
        <Card title="No diagnosis">
          <p className="text-sm text-rose-800">{verdict.reason}</p>
        </Card>
        <Severity rec={rec} />
      </div>
    );
  }
  if (verdict.kind === "withheld") {
    return (
      <div className="space-y-3" data-testid="diagnosis-withheld">
        <Header rec={rec} />
        <Card title="Conclusion withheld">
          <p className="text-sm text-rose-800">{verdict.reason}</p>
          {verdict.unresolved.length > 0 && (
            <p className="mt-1 font-mono text-xs text-slate-500">
              Unresolved: {verdict.unresolved.join(", ")}
            </p>
          )}
        </Card>
        <Severity rec={rec} />
      </div>
    );
  }

  const d = rec.diagnosis!;
  const supporting = verdict.supporting;
  const timeline = evidenceTimeline([...supporting, ...verdict.contradicting]);

  if (verdict.kind === "insufficient") {
    return (
      <div className="space-y-3" data-testid="diagnosis-insufficient">
        <Header rec={rec} />
        <Card title="Insufficient evidence">
          <p className="text-sm">{d.root_cause.description}</p>
          <p className="mt-2 text-sm font-medium">Missing information</p>
          <ul className="ml-5 list-disc text-sm">
            {d.missing_information.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-amber-700">Requires human review.</p>
        </Card>
        {supporting.length > 0 && (
          <Card title="Observed symptoms">
            <ul className="space-y-2">
              {supporting.map((c) => (
                <Claim key={c.evidence_id} claim={c} />
              ))}
            </ul>
          </Card>
        )}
        <Severity rec={rec} />
      </div>
    );
  }

  return (
    <div className="space-y-3" data-testid="diagnosis">
      <Header rec={rec} />
      <p className="text-sm text-slate-700">{d.incident_summary}</p>

      {/* 1. Root cause (only reachable when its evidence resolves) */}
      <Card title="1. Root cause" testId="root-cause">
        <div className="flex flex-wrap items-center gap-2">
          <ClaimLabel label="INFERENCE" />
          <span className="rounded bg-slate-900 px-2 py-0.5 font-mono text-xs text-white">
            {d.root_cause.taxonomy_label}
          </span>
          <span className="text-xs text-slate-500">
            stated confidence {pct(d.root_cause.confidence)} · self-consistency{" "}
            {pct(rec.self_consistency.agreement_with_primary)}
          </span>
          {d.requires_human_review && (
            <span className="rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-800">
              requires human review
            </span>
          )}
        </div>
        <p className="mt-2 text-sm">{d.root_cause.description}</p>
      </Card>

      {/* 2. Supporting evidence */}
      <Card title={`2. Supporting evidence (${supporting.length})`} testId="supporting">
        <ul className="space-y-2">
          {supporting.map((c) => (
            <Claim key={c.evidence_id} claim={c} />
          ))}
        </ul>
      </Card>

      {/* 3. Timeline of the cited evidence */}
      <Card title="3. Evidence timeline" testId="evidence-timeline">
        <ol className="space-y-1 text-xs">
          {withMarkers(timeline, onsetAt, alarmAt).map((row) =>
            row.kind === "marker" ? (
              <li key={`m-${row.text}`} className="text-slate-500" data-testid="timeline-marker">
                {fmtClock(row.at)} — {row.text}
              </li>
            ) : (
              <li key={row.claim.evidence_id} className="flex gap-2">
                <span className="font-mono text-slate-500">{fmtClock(row.claim.event!.timestamp)}</span>
                <ClaimLabel label={row.claim.label} />
                <span>
                  {row.claim.event!.resource_id}: {row.claim.event!.metric ?? row.claim.event!.event_type}
                </span>
              </li>
            ),
          )}
        </ol>
      </Card>

      {/* 4. Resource */}
      <Card title="4. Resource" testId="resource">
        <p className="font-mono text-sm">{d.root_cause.resource_id}</p>
        <p className="mt-1 text-xs text-slate-500">
          Impact: {d.impact_analysis.resources.join(", ") || "—"}
          {d.impact_analysis.user_impact ? ` · ${d.impact_analysis.user_impact}` : ""}
        </p>
      </Card>

      {/* 5. Dependency path */}
      <Card title="5. Dependency path" testId="dependency-path">
        {rec.dependency_path.length > 0 ? (
          <p className="flex flex-wrap items-center gap-1 font-mono text-xs">
            {rec.dependency_path.map((r, i) => (
              <span key={r} className="flex items-center gap-1">
                {i > 0 && <span className="text-slate-400">→</span>}
                <span className="rounded bg-slate-100 px-1.5 py-0.5">{r}</span>
              </span>
            ))}
          </p>
        ) : (
          <Empty>No dependency path from this resource to the alarmed resource in the inventory.</Empty>
        )}
      </Card>

      <Card title="Contradicting evidence" testId="contradicting">
        {verdict.contradicting.length ? (
          <ul className="space-y-2">
            {verdict.contradicting.map((c) => (
              <Claim key={c.evidence_id} claim={c} />
            ))}
          </ul>
        ) : (
          <Empty>None cited.</Empty>
        )}
      </Card>

      <Card title="Alternative hypotheses" testId="alternatives">
        {d.alternative_hypotheses.length ? (
          <ul className="space-y-2">
            {d.alternative_hypotheses.map((a) => (
              <li key={a.taxonomy_label} className="rounded border border-slate-200 p-2 text-sm">
                <div className="flex items-center gap-2">
                  <ClaimLabel label="HYPOTHESIS" />
                  <span className="font-mono text-xs">{a.taxonomy_label}</span>
                  <span className="text-xs text-slate-500">{pct(a.confidence)}</span>
                </div>
                <p className="mt-1">{a.description}</p>
                <p className="mt-1 text-xs text-slate-500">Rejected because: {a.rejected_because}</p>
              </li>
            ))}
          </ul>
        ) : (
          <Empty>None given.</Empty>
        )}
      </Card>

      {d.contributing_factors.length > 0 && (
        <Card title="Contributing factors">
          <ul className="space-y-1 text-sm">
            {d.contributing_factors.map((f) => (
              <li key={f.description} className="flex items-start gap-2">
                <ClaimLabel label={f.label} />
                {f.description}
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card title="Recommendations" testId="recommendations">
        <ul className="space-y-1 text-sm">
          {d.recommendations.map((r) => (
            <li key={r.action} className="flex items-start gap-2">
              <ClaimLabel label="RECOMMENDATION" />
              <span className="text-xs font-semibold text-slate-500">{r.category}</span>
              {r.action}
            </li>
          ))}
        </ul>
      </Card>

      <Card title="Historical incidents" testId="historical">
        {d.historical_influence.used ? (
          <p className="text-sm">
            Used {d.historical_influence.incident_ids.join(", ")}: {d.historical_influence.how}
          </p>
        ) : (
          <Empty>
            Past incidents did not change this diagnosis
            {rec.retrieved_incident_ids.length ? ` (${rec.retrieved_incident_ids.length} retrieved)` : ""}.
          </Empty>
        )}
      </Card>

      <Severity rec={rec} />
    </div>
  );
}
