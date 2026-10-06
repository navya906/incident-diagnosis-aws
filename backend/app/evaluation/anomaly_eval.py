"""Compare anomaly detectors against the simulator's injected-fault windows (dev split).

    python -m app.evaluation.anomaly_eval            # dev split, writes docs/experiments/
    python -m app.evaluation.anomaly_eval --sweep    # plus a threshold sensitivity table

Metrics (all on smoke-test / synthetic data):
- precision: flagged points inside a labelled window of the same series / all flagged points
  (flags before the injected onset, or on unaffected series, are false positives)
- recall: labelled windows with >= 1 flagged point / labelled windows that have data
- F1 of the two
- delay: minutes from the labelled window start to the first flag inside it (detected windows)
- series false-alarm rate: unaffected series with >= 1 flag / unaffected series

The test split is reserved for the final experiment runner (BRIEF Section 3) and needs --final.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.anomaly.detectors import METHODS, build_detector
from app.anomaly.series import series_from_events
from app.config import AnomalySettings, get_settings
from app.contracts.anomaly import MetricSeries
from app.interfaces.detector import Detector
from app.offline.dataset import DEFAULT_SEED, DatasetLoader, generate_dataset
from app.offline.models import AnomalyLabel

LABEL = "smoke-test / synthetic"
REPO = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = REPO / "data" / "generated" / "synthetic-v1"
DEFAULT_OUT = REPO / "docs" / "experiments"
SWEEP_GRID: dict[str, list[float]] = {
    "zscore": [2.0, 3.0, 4.0, 6.0],
    "mad": [2.5, 3.5, 5.0, 7.0],
    "moving_average": [0.25, 0.5, 1.0, 2.0],
    "rolling_std": [2.0, 3.0, 5.0, 8.0],
    "isolation_forest": [-0.05, 0.0, 0.05, 0.1],
}


@dataclass
class Counts:
    tp_points: int = 0
    fp_points: int = 0
    windows: int = 0
    detected: int = 0
    delays_min: list[float] = field(default_factory=list)
    clean_series: int = 0
    clean_series_flagged: int = 0

    def add(self, other: Counts) -> None:
        self.tp_points += other.tp_points
        self.fp_points += other.fp_points
        self.windows += other.windows
        self.detected += other.detected
        self.delays_min += other.delays_min
        self.clean_series += other.clean_series
        self.clean_series_flagged += other.clean_series_flagged

    def row(self) -> dict[str, float | int | None]:
        flagged = self.tp_points + self.fp_points
        precision = self.tp_points / flagged if flagged else 0.0
        recall = self.detected / self.windows if self.windows else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return {
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
            "median_delay_min": round(statistics.median(self.delays_min), 1)
            if self.delays_min
            else None,
            "mean_delay_min": round(statistics.fmean(self.delays_min), 1)
            if self.delays_min
            else None,
            "series_false_alarm_rate": round(self.clean_series_flagged / self.clean_series, 3)
            if self.clean_series
            else 0.0,
            "flagged_points": flagged,
            "windows": self.windows,
            "detected_windows": self.detected,
        }


def score_series(series: MetricSeries, labels: list[AnomalyLabel], detector: Detector) -> Counts:
    c = Counts()
    flags = sorted(a.timestamp for a in detector.detect(series))
    times = [p.timestamp for p in series.points]
    if not labels:
        c.clean_series = 1
        c.clean_series_flagged = int(bool(flags))
        c.fp_points = len(flags)
        return c

    def inside(t: datetime) -> bool:
        return any(lab.start <= t <= lab.end for lab in labels)

    c.tp_points = sum(1 for t in flags if inside(t))
    c.fp_points = len(flags) - c.tp_points
    for lab in labels:
        if not any(lab.start <= t <= lab.end for t in times):
            continue  # no data in the window (e.g. stripped telemetry): not evaluable
        c.windows += 1
        hits = [t for t in flags if lab.start <= t <= lab.end]
        if hits:
            c.detected += 1
            c.delays_min.append((hits[0] - lab.start).total_seconds() / 60)
    return c


@dataclass
class Case:
    incident_id: str
    category: str
    series: list[MetricSeries]
    labels: dict[tuple[str, str], list[AnomalyLabel]]


def load_cases(loader: DatasetLoader, split: str, max_incidents: int | None = None) -> list[Case]:
    cases = []
    for iid in loader.incident_ids(split)[:max_incidents]:
        obs, truth = loader.load(iid), loader.load_truth(iid)
        labels: dict[tuple[str, str], list[AnomalyLabel]] = defaultdict(list)
        for lab in truth.anomaly_labels:
            labels[(lab.resource_id, lab.metric)].append(lab)
        cases.append(Case(iid, truth.meta.category, series_from_events(obs.events), dict(labels)))
    return cases


def evaluate(cases: list[Case], detector: Detector) -> dict[str, Counts]:
    """Counts overall and per metric resolution ('all', '60s', '300s')."""
    out: dict[str, Counts] = defaultdict(Counts)
    for case in cases:
        for s in case.series:
            c = score_series(s, case.labels.get((s.resource_id, s.metric), []), detector)
            out["all"].add(c)
            out[f"{s.period_seconds}s"].add(c)
    return dict(out)


def _md_table(rows: list[dict], cols: list[str]) -> str:
    def fmt(v):
        return "n/a" if v is None else str(v)

    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(fmt(r[c]) for c in cols) + " |" for r in rows]
    return "\n".join(lines)


def run(
    dataset: Path,
    split: str,
    out_dir: Path,
    settings: AnomalySettings,
    sweep: bool = False,
    methods: tuple[str, ...] = METHODS,
    max_incidents: int | None = None,
) -> dict:
    loader = DatasetLoader(dataset)
    cases = load_cases(loader, split, max_incidents)
    main_rows, res_rows, sweep_rows = [], [], []
    for m in methods:
        result = evaluate(cases, build_detector(m, settings))
        main_rows.append({"method": m, **result["all"].row()})
        for res in sorted(k for k in result if k != "all"):
            res_rows.append({"method": m, "resolution": res, **result[res].row()})
    if sweep:
        for m in methods:
            for thr in SWEEP_GRID[m]:
                tuned = settings.model_copy(deep=True)
                getattr(tuned, m).threshold = thr
                r = evaluate(cases, build_detector(m, tuned))["all"].row()
                sweep_rows.append({"method": m, "threshold": thr, **r})

    m = loader.manifest
    meta = {
        "label": LABEL,
        "split": split,
        "incidents": len(cases),
        "series": sum(len(c.series) for c in cases),
        "dataset_version": m.dataset_version,
        "generator_version": m.generator_version,
        "seed": m.seed,
        "content_sha256": m.content_sha256,
        "config": settings.model_dump(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"phase3-anomaly-detectors-{split}"
    cols = [
        "method",
        "precision",
        "recall",
        "f1",
        "median_delay_min",
        "mean_delay_min",
        "series_false_alarm_rate",
        "flagged_points",
        "detected_windows",
        "windows",
    ]
    md = [
        f"# Anomaly detector comparison ({split} split) — {LABEL}",
        "",
        f"> **{LABEL.upper()}.** Simulator data with injected faults, not real AWS telemetry. "
        "These numbers show how the detectors behave on the simulator's assumptions; they are not "
        "evidence of real-world accuracy.",
        "",
        f"- Dataset: `{m.dataset_version}`, generator `{m.generator_version}`, seed {m.seed}, "
        f"content sha256 `{m.content_sha256}`",
        f"- Split: `{split}` — {meta['incidents']} incidents, {meta['series']} metric series",
        "- Labels: injected-fault windows from the truth files (`anomaly_labels`)",
        "- Command: `python -m app.evaluation.anomaly_eval"
        + (" --sweep" if sweep else "")
        + (f" --split {split}" if split != "dev" else "")
        + "`",
        "- Config: `anomaly` section of `config/default.yaml` (thresholds below are the defaults)",
        "",
        "Precision = flagged points inside an injected window / all flagged points. "
        "Recall = injected windows with at least one flag / injected windows with data. "
        "Delay = minutes from window start to first flag. Series false-alarm rate = unaffected "
        "series with any flag / unaffected series.",
        "",
        "## All resolutions",
        "",
        _md_table(main_rows, cols),
        "",
        "## By metric resolution",
        "",
        _md_table(res_rows, ["resolution", *cols]),
        "",
    ]
    if sweep_rows:
        md += [
            "## Threshold sensitivity (dev split only)",
            "",
            _md_table(sweep_rows, ["threshold", *cols]),
            "",
        ]
    md += [
        "## Caveats",
        "",
        "- Labels mark the whole post-onset window of every injected effect, including the ramp, "
        "so early ramp points that look normal count against recall/delay, and a fault's effects "
        "are assumed to persist to the end of the window.",
        "- Ground-truth windows come from the same simulator assumptions the detectors are tested "
        "on (see PROGRESS.md, circularity risk). Real captures are needed to confirm any ranking.",
        "- Delay is quantised by the metric period (1 or 5 minutes).",
        "",
    ]
    (out_dir / f"{stem}.md").write_text("\n".join(md), encoding="utf-8", newline="\n")
    with open(out_dir / f"{stem}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["table", "method", "resolution", "threshold", *cols[1:]])
        w.writeheader()
        for table, rows in (("all", main_rows), ("resolution", res_rows), ("sweep", sweep_rows)):
            for r in rows:
                w.writerow({"table": table, **r})
    payload = {"meta": meta, "all": main_rows, "by_resolution": res_rows, "sweep": sweep_rows}
    (out_dir / f"{stem}.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8", newline="\n"
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compare anomaly detectors on the synthetic dataset.")
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--split", choices=["dev", "test"], default="dev")
    p.add_argument("--final", action="store_true", help="required to touch the test split")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--sweep", action="store_true", help="add a threshold sensitivity table")
    args = p.parse_args(argv)
    if args.split == "test" and not args.final:
        p.error("the test split is reserved for the final experiment runner; pass --final")
    if not (args.dataset / "manifest.json").is_file():
        print(f"dataset not found at {args.dataset}; generating it (seed {DEFAULT_SEED})")
        generate_dataset(args.dataset, seed=DEFAULT_SEED)
    payload = run(args.dataset, args.split, args.out, get_settings().anomaly, sweep=args.sweep)
    print(f"[{LABEL}] {payload['meta']['incidents']} incidents, split={args.split}")
    for r in payload["all"]:
        print(
            f"  {r['method']:17s} P={r['precision']:.3f} R={r['recall']:.3f} F1={r['f1']:.3f} "
            f"median delay={r['median_delay_min']} min  FA-series={r['series_false_alarm_rate']}"
        )
    print(f"wrote {args.out}/phase3-anomaly-detectors-{args.split}.{{md,csv,json}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
