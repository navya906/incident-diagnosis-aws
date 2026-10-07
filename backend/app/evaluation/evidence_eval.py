"""Evidence ranking and candidate-cause evaluation against ground-truth evidence (dev split).

    python -m app.evaluation.evidence_eval            # dev split, writes docs/experiments/
    python -m app.evaluation.evidence_eval --tune     # plus the dev-only weight grid

Metrics (all on smoke-test / synthetic data), macro-averaged over incidents that have
ground-truth evidence (insufficient-evidence cases have none and are excluded):
- precision@K: ground-truth evidence events in the returned top-K / items returned
- recall@K: ground-truth evidence events in the returned top-K / ground-truth evidence events
- window coverage: ground-truth evidence events inside the investigation window / all of them
  (an upper bound on recall for that window)
- red-herring rate: red-herring cases with a red-herring event in the top-K / red-herring cases
- candidate causes: hit@n = the true primary resource is the resource of one of the first n
  candidate causes; red-herring causes = candidate causes built on a red-herring event

The test split is reserved for the final experiment runner (BRIEF Section 3) and needs --final.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from app.config import Settings, get_settings
from app.contracts.anomaly import Anomaly
from app.contracts.events import CanonicalEvent
from app.contracts.evidence import ScoreWeights
from app.correlation.events import event_kind
from app.correlation.windows import STANDARD_WINDOWS, events_in_window, investigation_window
from app.evidence.pipeline import InvestigationPipeline, incident_onset
from app.evidence.ranker import ScoredEvent, rank_by_recency
from app.graph.builder import build_graph
from app.graph.store import NetworkXGraphStore
from app.offline.dataset import DEFAULT_SEED, DatasetLoader, generate_dataset
from app.offline.models import IncidentRecord, TruthRecord

LABEL = "smoke-test / synthetic"
REPO = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = REPO / "data" / "generated" / "synthetic-v1"
DEFAULT_OUT = REPO / "docs" / "experiments"
K_VALUES = (5, 10, 20, 40)
COMPONENTS = ("temporal", "resource", "anomaly", "semantic", "dependency")
TUNE_GRID = (0.0, 0.15, 0.3)


@dataclass
class Case:
    incident: IncidentRecord
    truth: TruthRecord
    events: list[CanonicalEvent]
    known: set[str]
    graph: NetworkXGraphStore
    anomalies: list[Anomaly]
    onset: datetime
    gt: set[str] = field(default_factory=set)
    red_herrings: set[str] = field(default_factory=set)

    @property
    def category(self) -> str:
        return self.truth.meta.category


def load_cases(
    loader: DatasetLoader, split: str, pipeline: InvestigationPipeline, limit: int | None = None
) -> list[Case]:
    cases = []
    for iid in loader.incident_ids(split)[:limit]:
        s = loader.load(iid)
        t = loader.load_truth(iid)
        anomalies = pipeline.detect(s.events)
        graph = build_graph(s.resources, s.relationships)
        cases.append(
            Case(
                incident=s.incident,
                truth=t,
                events=s.events,
                known={r.resource_id for r in s.resources},
                graph=graph,
                anomalies=anomalies,
                onset=incident_onset(s.incident, s.events, anomalies, graph, pipeline.settings),
                gt=set(t.ground_truth.evidence_event_ids),
                red_herrings=set(t.meta.red_herring_event_ids),
            )
        )
    return cases


class Evaluator:
    def __init__(self, cases: list[Case], pipeline: InvestigationPipeline):
        self.cases = cases
        self.pipeline = pipeline
        self._scored: dict[tuple, list[ScoredEvent]] = {}

    def scored(self, case: Case, window: int, use_graph: bool, use_anomalies: bool):
        """Score components once per (incident, window, ablation); weights are applied later."""
        key = (case.incident.incident_id, window, use_graph, use_anomalies)
        if key not in self._scored:
            st = self.pipeline.settings.evidence
            w = investigation_window(case.incident.alarm_time, window, st.post_alarm_minutes)
            candidates = [e for e in events_in_window(case.events, w) if event_kind(e) != "alarm"]
            onset = case.onset if use_anomalies else case.incident.alarm_time
            self._scored[key] = self.pipeline.ranker.score_events(
                candidates=candidates,
                affected_resources=case.incident.affected_resources,
                description=case.incident.description,
                onset=onset,
                anomalies=case.anomalies if use_anomalies else None,
                graph=case.graph if use_graph else None,
                known_resources=case.known,
            )
        return self._scored[key]

    def top_ids(
        self,
        case: Case,
        window: int,
        k: int,
        weights: ScoreWeights,
        use_graph: bool = True,
        use_anomalies: bool = True,
    ) -> list[str]:
        scored = [
            replace(s, score=weights.combine(s.components))
            for s in self.scored(case, window, use_graph, use_anomalies)
        ]
        return [s.event.event_id for s in self.pipeline.ranker.select(scored, k)]

    def evaluate(self, name: str, window: int, k: int, ids_for) -> dict:
        p, r, cov, rh, rh_n, gt_sizes = [], [], [], 0, 0, []
        st = self.pipeline.settings.evidence
        for c in self.cases:
            ids = ids_for(c)
            if c.red_herrings:
                rh_n += 1
                rh += bool(c.red_herrings & set(ids))
            if not c.gt:
                continue
            hits = len(c.gt & set(ids))
            p.append(hits / len(ids) if ids else 0.0)
            r.append(hits / len(c.gt))
            w = investigation_window(c.incident.alarm_time, window, st.post_alarm_minutes)
            inside = sum(1 for e in c.events if e.event_id in c.gt and w.contains(e.timestamp))
            cov.append(inside / len(c.gt))
            gt_sizes.append(len(c.gt))
        return {
            "config": name,
            "window_min": window,
            "k": k,
            "precision": round(statistics.fmean(p), 3) if p else None,
            "recall": round(statistics.fmean(r), 3) if r else None,
            "window_coverage": round(statistics.fmean(cov), 3) if cov else None,
            "red_herring_rate": round(rh / rh_n, 3) if rh_n else None,
            "incidents": len(p),
            "mean_gt_items": round(statistics.fmean(gt_sizes), 2) if gt_sizes else None,
        }

    def full(self, window: int, k: int, weights: ScoreWeights, name: str = "Full", **kw) -> dict:
        return self.evaluate(name, window, k, lambda c: self.top_ids(c, window, k, weights, **kw))

    def causes(self, window: int) -> dict:
        st = self.pipeline.settings
        hits = {1: 0, 3: 0, 5: 0}
        n = rh_causes = rh_chain_events = with_rh = 0
        counts, chain_lens = [], []
        evidence_hit = 0
        for c in self.cases:
            w = investigation_window(c.incident.alarm_time, window, st.evidence.post_alarm_minutes)
            res = self.pipeline.correlator.correlate(
                incident_id=c.incident.incident_id,
                affected_resources=c.incident.affected_resources,
                events=c.events,
                anomalies=c.anomalies,
                window=w,
                onset=c.onset,
                graph=c.graph,
            )
            causes = res.candidate_causes
            if c.red_herrings:
                with_rh += 1
                rh_causes += sum(1 for x in causes if set(x.signal.event_ids) & c.red_herrings)
                rh_chain_events += sum(len(set(ch.event_ids) & c.red_herrings) for ch in res.chains)
            if not c.gt:
                continue
            n += 1
            counts.append(len(causes))
            chain_lens += [len(x.chain.signal_ids) for x in causes]
            resources = [x.signal.resource_id for x in causes]
            for k in hits:
                hits[k] += c.truth.ground_truth.primary_resource_id in resources[:k]
            evidence_hit += bool(causes and set(causes[0].signal.event_ids) & c.gt)
        return {
            "window_min": window,
            "incidents": n,
            "hit@1": round(hits[1] / n, 3) if n else None,
            "hit@3": round(hits[3] / n, 3) if n else None,
            "hit@5": round(hits[5] / n, 3) if n else None,
            "top_cause_is_gt_evidence": round(evidence_hit / n, 3) if n else None,
            "mean_candidates": round(statistics.fmean(counts), 2) if counts else None,
            "mean_chain_signals": round(statistics.fmean(chain_lens), 2) if chain_lens else None,
            "red_herring_cases": with_rh,
            "red_herring_candidate_causes": rh_causes,
            "red_herring_chain_events": rh_chain_events,
        }


def _only(component: str) -> ScoreWeights:
    return ScoreWeights(**{c: (1.0 if c == component else 0.0) for c in COMPONENTS})


def _without(weights: ScoreWeights, component: str) -> ScoreWeights:
    return weights.model_copy(update={component: 0.0})


def _md_table(rows: list[dict], cols: list[str]) -> str:
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += [
        "| " + " | ".join("n/a" if r.get(c) is None else str(r.get(c)) for c in cols) + " |"
        for r in rows
    ]
    return "\n".join(lines)


def run(
    dataset: Path,
    split: str,
    out_dir: Path,
    settings: Settings,
    tune: bool = False,
    limit: int | None = None,
) -> dict:
    loader = DatasetLoader(dataset)
    pipeline = InvestigationPipeline(settings)
    cases = load_cases(loader, split, pipeline, limit)
    ev = Evaluator(cases, pipeline)
    st = settings.evidence
    W, K, weights = st.window_minutes, st.top_k, st.weights

    grid = [ev.full(w, k, weights) for w in STANDARD_WINDOWS for k in K_VALUES]
    ablations = [
        ev.full(W, K, weights, "Full"),
        ev.full(W, K, _without(weights, "dependency"), "A2 no dependency graph", use_graph=False),
        ev.full(W, K, _without(weights, "anomaly"), "A3 no anomaly detection", use_anomalies=False),
        ev.full(W, K, _without(weights, "temporal"), "A4 no temporal (weight 0)"),
        ev.evaluate(
            "A5 recency (no ranking)",
            W,
            K,
            lambda c: [
                i.event_id
                for i in rank_by_recency(
                    c.incident.incident_id,
                    c.events,
                    c.incident.alarm_time,
                    W,
                    K,
                    st.post_alarm_minutes,
                )
            ],
        ),
        ev.full(W, K, _without(weights, "resource"), "minus resource"),
        ev.full(W, K, _without(weights, "semantic"), "minus semantic"),
    ]
    ablations += [ev.full(W, K, _only(c), f"only {c}") for c in COMPONENTS]
    by_category = []
    for cat in ("standard", "red_herring", "compound"):
        sub = Evaluator([c for c in cases if c.category == cat], pipeline)
        sub._scored = ev._scored
        by_category.append({"category": cat, **sub.full(W, K, weights)})
    causes = [ev.causes(w) for w in STANDARD_WINDOWS]

    tune_rows: list[dict] = []
    if tune:
        for combo in itertools.product(TUNE_GRID, repeat=len(COMPONENTS)):
            if not any(combo):
                continue
            w = ScoreWeights(**dict(zip(COMPONENTS, combo, strict=True)))
            row = ev.full(W, K, w, "grid")
            tune_rows.append({**dict(zip(COMPONENTS, combo, strict=True)), **row})
        tune_rows.sort(key=lambda r: (-(r["recall"] or 0), -(r["precision"] or 0)))

    m = loader.manifest
    meta = {
        "label": LABEL,
        "split": split,
        "incidents": len(cases),
        "incidents_with_gt": sum(1 for c in cases if c.gt),
        "dataset_version": m.dataset_version,
        "generator_version": m.generator_version,
        "seed": m.seed,
        "content_sha256": m.content_sha256,
        "evidence_config": st.model_dump(),
        "correlation_config": settings.correlation.model_dump(),
    }
    payload = {
        "meta": meta,
        "grid": grid,
        "ablations": ablations,
        "by_category": by_category,
        "candidate_causes": causes,
        "tune": tune_rows[:20],
    }
    _write(out_dir, split, payload, tune)
    return payload


def _write(out_dir: Path, split: str, payload: dict, tune: bool) -> None:
    meta = payload["meta"]
    st = meta["evidence_config"]
    cols = ["config", "window_min", "k", "precision", "recall", "window_coverage"]
    cols += ["red_herring_rate"]
    cause_cols = [
        "window_min",
        "hit@1",
        "hit@3",
        "hit@5",
        "top_cause_is_gt_evidence",
        "mean_candidates",
        "mean_chain_signals",
        "red_herring_cases",
        "red_herring_candidate_causes",
        "red_herring_chain_events",
    ]
    md = [
        f"# Evidence ranking and candidate causes ({split} split) — {LABEL}",
        "",
        f"> **{LABEL.upper()}.** Simulator data with injected faults, not real AWS telemetry. "
        "Ground-truth evidence is defined by the simulator (DECISIONS D23), so these numbers "
        "measure agreement with the simulator's assumptions, not real-world accuracy.",
        "",
        f"- Dataset: `{meta['dataset_version']}`, generator `{meta['generator_version']}`, "
        f"seed {meta['seed']}, content sha256 `{meta['content_sha256']}`",
        f"- Split: `{split}` — {meta['incidents']} incidents, {meta['incidents_with_gt']} with "
        "ground-truth evidence (insufficient-evidence cases have none and are excluded from P/R)",
        "- Command: `python -m app.evaluation.evidence_eval"
        + (" --tune" if tune else "")
        + (f" --split {split}" if split != "dev" else "")
        + "`",
        f"- Defaults: window {st['window_minutes']} min before the alarm + "
        f"{st['post_alarm_minutes']} min after, K = {st['top_k']}, weights "
        + ", ".join(f"{k} {v}" for k, v in st["weights"].items())
        + f", anomaly method `{st['anomaly_method']}` (threshold "
        f"{st['anomaly_threshold'] or 'default'}), max {st['max_per_group']} items per group",
        "",
        "Precision@K = ground-truth items in the top-K / items returned. Recall@K = ground-truth "
        "items in the top-K / ground-truth items. Window coverage = ground-truth items inside the "
        "window (upper bound on recall). Red-herring rate = red-herring cases with a red-herring "
        "event in the top-K. "
        f"Ground truth averages {payload['ablations'][0]['mean_gt_items']} items per incident, "
        "so precision@K cannot exceed that number / K.",
        "",
        "## Ablations (default window and K)",
        "",
        _md_table(payload["ablations"], cols),
        "",
        "A2 also removes the graph (dependency component 0); A3 removes detector output, so "
        "the onset falls back to the alarm time and metric points get no anomaly score; A4 sets "
        "the temporal weight to 0 (chains do not feed the score, see DECISIONS D52).",
        "",
        "## By category (Full, default window and K)",
        "",
        _md_table(payload["by_category"], ["category", *cols[1:]]),
        "",
        "## Window x K (Full)",
        "",
        _md_table(payload["grid"], cols),
        "",
        "## Candidate causes and event chains",
        "",
        "hit@n: the true primary resource is the resource of one of the first n candidate causes. "
        "top_cause_is_gt_evidence: the first candidate cause is built on a ground-truth evidence "
        "event. Red-herring columns count red-herring events among candidate causes and in the "
        "reported chains (red-herring cases only).",
        "",
        _md_table(payload["candidate_causes"], cause_cols),
        "",
    ]
    if payload["tune"]:
        md += [
            "## Weight grid (dev split only; top 20 by recall, then precision)",
            "",
            _md_table(payload["tune"], [*COMPONENTS, "precision", "recall", "red_herring_rate"]),
            "",
        ]
    md += [
        "## Caveats",
        "",
        "- In-sample: the default weights, detector threshold and other evidence settings were "
        "chosen on this dev split (DECISIONS D55), so the Full row is optimistic. An unbiased "
        "estimate comes only from the test split in the Phase 7 experiment runner.",
        "- Ground-truth evidence and the ranking's signals come from one author's assumptions "
        "about how faults look (circularity). Real captures are needed to confirm any ranking.",
        "- Ground truth lists only the first occurrences of each key signal, so other correct "
        "but unlisted items (later datapoints, secondary metrics) count as false positives.",
        "- Small sample: differences of a few points between configurations are within noise; "
        "Phase 7 adds bootstrap confidence intervals.",
        "",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"phase4-evidence-ranking-{split}"
    (out_dir / f"{stem}.md").write_text("\n".join(md), encoding="utf-8", newline="\n")
    with open(out_dir / f"{stem}.csv", "w", newline="", encoding="utf-8") as f:
        fields = ["table", "category", *cols, "incidents", "mean_gt_items"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for table in ("ablations", "by_category", "grid"):
            for r in payload[table]:
                w.writerow({"table": table, **r})
    (out_dir / f"{stem}.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8", newline="\n"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Evaluate evidence ranking on the synthetic dataset.")
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--split", choices=["dev", "test"], default="dev")
    p.add_argument("--final", action="store_true", help="required to touch the test split")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--tune", action="store_true", help="add the dev-only weight grid")
    args = p.parse_args(argv)
    if args.split == "test" and not args.final:
        p.error("the test split is reserved for the final experiment runner; pass --final")
    if args.split == "test" and args.tune:
        p.error("weights are tuned on the dev split only")
    if not (args.dataset / "manifest.json").is_file():
        print(f"dataset not found at {args.dataset}; generating it (seed {DEFAULT_SEED})")
        generate_dataset(args.dataset, seed=DEFAULT_SEED)
    payload = run(args.dataset, args.split, args.out, get_settings(), tune=args.tune)
    print(f"[{LABEL}] {payload['meta']['incidents']} incidents, split={args.split}")
    for r in payload["ablations"]:
        print(f"  {r['config']:28s} P@{r['k']}={r['precision']} R@{r['k']}={r['recall']}")
    for r in payload["candidate_causes"]:
        print(
            f"  causes W={r['window_min']:2d}  hit@1={r['hit@1']} hit@3={r['hit@3']} "
            f"red-herring causes={r['red_herring_candidate_causes']}"
        )
    print(f"wrote {args.out}/phase4-evidence-ranking-{args.split}.{{md,csv,json}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
