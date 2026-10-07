"""Experiment runner: condition matrix x incidents x runs -> stored, reproducible results.

Experiment id = "exp-" + first 12 hex of sha256 over: the config (canonical JSON), the dataset
content hash, the historical-corpus content hash, the prompt hash, the configured model, and a
hash of the application source (`backend/app/**/*.py`). Same id => same inputs and code; with a
deterministic model (the stub) the results are byte-identical, which `verify` checks by re-running
and comparing `results_sha256` (DECISIONS D82).

Output directory `docs/experiments/runs/<id>/`:
  manifest.json        id, label, split, timestamp, model + provider-reported versions, prompt
                       version/hash, dataset/corpus versions and hashes, config, effective
                       settings (no secrets), source hash, git commit, library versions,
                       results_sha256
  summary.json/.csv    per condition: metrics with bootstrap CIs
  comparisons.csv      every condition vs Full: paired bootstrap differences, exact McNemar
  report.md            the tables above, labelled
  spot_check.csv       seeded sample of diagnoses for human review of the rubric
  results.jsonl        one record per condition x incident x run: retrieved evidence, diagnosis,
                       ground truth, metrics (git-ignored: large and regenerable from the id)
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import random
import statistics
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from sqlalchemy.orm import Session

from app.ai.llm_clients import build_llm_client
from app.baselines.rules import MODEL as RULES_MODEL
from app.baselines.rules import diagnose_rules
from app.config import Settings, get_settings
from app.contracts.historical import HistoricalRecord
from app.contracts.taxonomy import FAULT_TYPES
from app.diagnosis.engine import DiagnosisEngine, DiagnosisResult, SelfConsistency
from app.diagnosis.prompts import PROMPT_VERSION, prompt_sha
from app.diagnosis.validator import CitationReport
from app.evaluation import stats
from app.evaluation.metrics import JUDGE_PROMPT, RUBRIC_VERSION, record_metrics
from app.experiments.config import (
    EVIDENCE_TYPES,
    Condition,
    ExperimentConfig,
    condition_matrix,
)
from app.offline.dataset import DatasetLoader
from app.offline.replay import ReplayCollector
from app.rag.corpus import build_corpus, heldout_fingerprints
from app.rag.embedders import build_embedder
from app.rag.knowledge_base import HistoricalRetriever, KnowledgeBase, build_query
from app.rag.vector_store import LocalVectorStore

REPO = Path(__file__).resolve().parents[3]
APP_DIR = REPO / "backend" / "app"
DEFAULT_DATASET = REPO / "data" / "generated" / "synthetic-v1"
DEFAULT_CORPUS = REPO / "data" / "generated" / "historical-v1"
DEFAULT_RUNS = REPO / "docs" / "experiments" / "runs"
#: Fields that legitimately differ between identical runs and are excluded from the results hash.
VOLATILE = ("latency_ms",)

CATEGORIES = ("standard", "red_herring", "compound", "insufficient_evidence")
CASE_TYPE = {
    "standard": "clean",
    "red_herring": "red-herring",
    "compound": "compound",
    "insufficient_evidence": "insufficient-evidence",
}
#: Metrics that also get a cluster-bootstrap interval (clusters = ground-truth fault type).
CLUSTERED = ("label_correct", "root_cause_correct", "cited_recall")
BINARY = ("label_correct", "resource_correct", "root_cause_correct", "top3_correct", "valid")
CONTINUOUS = (
    "cited_precision",
    "cited_recall",
    "context_recall",
    "context_precision",
    "rubric",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
    "latency_ms",
)


class SplitGuardError(RuntimeError):
    """The test split was requested without the explicit --final confirmation."""


def source_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(APP_DIR.rglob("*.py")):
        h.update(p.relative_to(APP_DIR).as_posix().encode())
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def effective_settings(config: ExperimentConfig, base: Settings | None = None) -> Settings:
    base = base or get_settings()
    data = base.model_dump()
    for section, values in config.settings.items():
        if isinstance(values, dict) and isinstance(data.get(section), dict):
            data[section] = {**data[section], **values}
        else:
            data[section] = values
    data["diagnosis"]["self_consistency_samples"] = config.self_consistency_samples
    return Settings.model_validate(data)


def _public_settings(settings: Settings) -> dict:
    d = json.loads(settings.model_dump_json())
    for section in ("llm", "embeddings"):
        d.get(section, {}).pop("api_key", None)
    d.get("api", {}).pop("api_keys", None)
    d.pop("database_url", None)
    return d


def _corpus_sha(records: list[HistoricalRecord]) -> str:
    body = _canonical([r.model_dump(mode="json") for r in records])
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def experiment_id(
    config: ExperimentConfig, settings: Settings, dataset_sha: str, corpus_sha: str, src: str
) -> str:
    key = {
        "config": config.model_dump(mode="json"),
        "dataset": dataset_sha,
        "corpus": corpus_sha,
        "prompt": prompt_sha(),
        "model": {"provider": settings.llm.provider, "model": settings.llm.model},
        "embedder": {"provider": settings.embeddings.provider, "model": settings.embeddings.model},
        "source": src,
    }
    return "exp-" + hashlib.sha256(_canonical(key).encode()).hexdigest()[:12]


def distractors(
    query_text: str,
    signals,
    alarm_metric,
    resource_types,
    true_label: str,
    incident_id: str,
    n: int,
) -> list[HistoricalRecord]:
    """Entries that look like the incident (same text and signals) but carry another root cause.
    Built from the observable query only, never from ground truth beyond excluding its label."""
    others = sorted(
        (f.value for f in FAULT_TYPES if f.value != true_label),
        key=lambda lab: hashlib.sha256(f"{incident_id}:{lab}".encode()).hexdigest(),
    )[:n]
    out = []
    for lab in others:
        did = "distractor-" + hashlib.sha256(f"{incident_id}:{lab}".encode()).hexdigest()[:10]
        phrase = lab.replace("_", " ")
        out.append(
            HistoricalRecord(
                incident_id=did,
                corpus_version="distractors",
                source="distractor",
                title=query_text.split(". ", 1)[0],
                search_text=query_text,
                summary=(
                    f"{query_text}. Root cause ({lab}): {phrase}. Resolution: fix the {phrase}."
                ),
                taxonomy_label=lab,
                root_cause_text=f"The incident was caused by {phrase}.",
                resolution=f"Fix the {phrase}.",
                alarm_metric=alarm_metric,
                signals=sorted(signals),
                resource_types=sorted(resource_types),
                fingerprints=[did],
            )
        )
    return out


@dataclass
class ExperimentRun:
    experiment_id: str
    directory: Path
    manifest: dict
    summary: list[dict]
    comparisons: list[dict]
    records: list[dict]
    strata: list[dict] = field(default_factory=list)


class ExperimentRunner:
    def __init__(
        self,
        config: ExperimentConfig,
        dataset: Path = DEFAULT_DATASET,
        corpus_dir: Path = DEFAULT_CORPUS,
        out_root: Path = DEFAULT_RUNS,
        base_settings: Settings | None = None,
    ):
        self.config = config
        self.dataset = Path(dataset)
        self.corpus_dir = Path(corpus_dir)
        self.out_root = Path(out_root)
        self.settings = effective_settings(config, base_settings)

    # ------------------------------------------------------------------ setup
    def _prepare(self, final: bool):
        cfg = self.config
        if cfg.split == "test" and not final:
            raise SplitGuardError(
                "the test split is reserved for final runs: pass --final (and never tune on it)"
            )
        loader = DatasetLoader(self.dataset)
        corpus = build_corpus(
            self.corpus_dir, self.settings.rag.corpus_seed, self.settings.rag.corpus_version
        )
        embedder = build_embedder(self.settings)
        kb = KnowledgeBase(embedder, LocalVectorStore(), "historical", heldout_fingerprints(loader))
        kb.build(corpus)
        exp_id = experiment_id(
            cfg, self.settings, loader.manifest.content_sha256, _corpus_sha(corpus), source_hash()
        )
        return loader, corpus, kb, exp_id

    def _result_for(
        self,
        cond: Condition,
        engine: DiagnosisEngine,
        kb: KnowledgeBase,
        collector: ReplayCollector,
        iid: str,
        truth,
        run: int,
    ) -> DiagnosisResult:
        incident = collector.incident(iid)
        resources, relationships = collector.inventory(iid)
        events = collector.collect(collector.default_request(iid))
        if cond.evidence_types:
            keep = set(EVIDENCE_TYPES[cond.evidence_types])
            events = [e for e in events if e.source in keep or e.source.value == "alarm"]
        if cond.kind == "rules":
            d = diagnose_rules(incident, events)
            inv = engine.pipeline.run(
                incident=incident, events=events, resources=resources, relationships=relationships
            )
            severity = engine.severity.assess(
                incident=incident,
                events=events,
                onset=inv.onset,
                anomalies=inv.anomalies,
                resources=resources,
                graph=inv.graph,
            )
            return DiagnosisResult(
                incident_id=iid,
                condition=cond.name,
                model=RULES_MODEL,
                prompt_version="n/a",
                prompt_sha="n/a",
                valid=True,
                diagnosis=d,
                citation=CitationReport(cited=[e.evidence_id for e in d.supporting_evidence]),
                verbalized_confidence=d.root_cause.confidence,
                self_consistency=SelfConsistency(),
                severity=severity,
                llm_severity_suggestion=d.severity_suggestion.value,
                ranked_event_ids=[],
                context_mode="rules",
            )
        retriever = None
        exclude = None
        if cond.use_rag and cond.kb_mode == "fault_removed":
            exclude = {truth.taxonomy_label.value}
        elif cond.use_rag and cond.kb_mode == "distractors":
            inv = engine.pipeline.run(
                incident=incident, events=events, resources=resources, relationships=relationships
            )
            q = build_query(
                incident, events, [i.event_id for i in inv.ranking.items], resources=resources
            )
            extra = distractors(
                q.text,
                q.signals,
                q.alarm_metric,
                q.resource_types,
                truth.taxonomy_label.value,
                iid,
                self.config.distractors_per_incident,
            )
            retriever = HistoricalRetriever(kb.extended(extra), self.settings.rag)
        base_seed = self.settings.llm.seed
        engine.settings = engine.settings.model_copy(
            update={"llm": engine.settings.llm.model_copy(update={"seed": base_seed + 1000 * run})}
        )
        try:
            return engine.diagnose(
                incident=incident,
                events=events,
                resources=resources,
                relationships=relationships,
                condition=cond.name,
                use_graph=cond.use_graph,
                use_anomalies=cond.use_anomalies,
                use_chains=cond.use_chains,
                use_rag=cond.use_rag,
                context_mode=cond.context_mode,
                ranking_mode=cond.ranking_mode,
                weights=cond.weights(self.settings.evidence.weights),
                window_minutes=cond.window_minutes,
                top_k=cond.top_k,
                retriever=retriever,
                exclude_labels=exclude,
                samples=self.config.samples_for(cond.name),
            )
        finally:
            engine.settings = engine.settings.model_copy(
                update={"llm": engine.settings.llm.model_copy(update={"seed": base_seed})}
            )

    # ------------------------------------------------------------------ main
    def run(self, final: bool = False, write: bool = True) -> ExperimentRun:
        cfg = self.config
        loader, corpus, kb, exp_id = self._prepare(final)
        started = datetime.now(UTC)
        collector = ReplayCollector(loader)
        llm = build_llm_client(self.settings)
        engines = {
            True: DiagnosisEngine(llm, self.settings, HistoricalRetriever(kb, self.settings.rag))
        }
        ids = loader.incident_ids(cfg.split)[: cfg.limit]
        matrix = condition_matrix(cfg)
        if any(not c.redaction for c in matrix):
            # "Full, redaction off": a second client built with redaction disabled. In aws mode
            # the factory refuses this (RedactionPolicyError): the run fails closed (D87).
            off = self.settings.model_copy(
                update={"redaction": self.settings.redaction.model_copy(update={"enabled": False})}
            )
            engines[False] = DiagnosisEngine(
                build_llm_client(off), off, HistoricalRetriever(kb, off.rag)
            )
        records = []
        for cond in matrix:
            engine = engines[cond.redaction]
            for iid in ids:
                truth_rec = loader.load_truth(iid)
                truth = truth_rec.ground_truth
                events = loader.load(iid).events
                for run in range(cfg.runs_for(cond.family)):
                    res = self._result_for(cond, engine, kb, collector, iid, truth, run)
                    records.append(
                        {
                            "experiment_id": exp_id,
                            "condition": cond.name,
                            "family": cond.family,
                            "incident_id": iid,
                            "category": truth_rec.meta.category,
                            "fault_type": truth.taxonomy_label.value,
                            "run_index": run,
                            "retrieved_evidence": res.ranked_event_ids,
                            "retrieved_incidents": res.retrieved_incident_ids,
                            "diagnosis": res.diagnosis.model_dump(mode="json")
                            if res.diagnosis
                            else None,
                            "failure": res.failure,
                            "ground_truth": truth.model_dump(mode="json"),
                            "metrics": record_metrics(res, truth, events),
                            "self_consistency": res.self_consistency.model_dump(),
                            "severity": res.severity.model_dump(mode="json"),
                            "response_models": sorted({a.model for a in res.attempts if a.model}),
                            "model": res.model,
                        }
                    )
        finished = datetime.now(UTC)
        summary = self.summarise(records, matrix)
        comparisons = self.compare(records, matrix)
        strata = self.stratify(records, matrix)
        manifest = {
            "experiment_id": exp_id,
            "name": cfg.name,
            "label": cfg.label,
            "split": cfg.split,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "model": {"provider": self.settings.llm.provider, "model": llm.model_name},
            "model_versions_reported": sorted({m for r in records for m in r["response_models"]}),
            "prompt_version": PROMPT_VERSION,
            "prompt_sha": prompt_sha(),
            "rubric_version": RUBRIC_VERSION,
            "dataset": {
                "version": loader.manifest.dataset_version,
                "content_sha256": loader.manifest.content_sha256,
                "generator_version": loader.manifest.generator_version,
                "seed": loader.manifest.seed,
            },
            "corpus": {
                "version": self.settings.rag.corpus_version,
                "sha": _corpus_sha(corpus),
                "size": len(corpus),
            },
            "embedder": kb.embedder.model_name,
            "incidents": len(ids),
            "conditions": [c.name for c in matrix],
            "runs_per_condition": {c.name: cfg.runs_for(c.family) for c in matrix},
            "self_consistency_per_condition": {
                c.name: cfg.samples_for(c.name) for c in matrix if c.kind == "llm"
            },
            "primary_comparisons": [c.model_dump() for c in cfg.primary_comparisons],
            "judge": cfg.judge.model_dump(),
            "config": cfg.model_dump(mode="json"),
            "settings": _public_settings(self.settings),
            "source_sha": source_hash(),
            "git_commit": _git_commit(),
            "versions": {"python": platform.python_version(), "numpy": np.__version__},
            "records": len(records),
            "results_sha256": results_hash(records),
            "totals": {
                "prompt_tokens": sum(r["metrics"]["prompt_tokens"] for r in records),
                "completion_tokens": sum(r["metrics"]["completion_tokens"] for r in records),
                "cost_usd": round(sum(r["metrics"]["cost_usd"] for r in records), 4),
            },
        }
        run_dir = self.out_root / exp_id
        if write:
            self.write(run_dir, manifest, summary, comparisons, records, strata)
        return ExperimentRun(exp_id, run_dir, manifest, summary, comparisons, records, strata)

    # ------------------------------------------------------------------ aggregation
    @staticmethod
    def _per_incident(records: list[dict], condition: str, metric: str) -> dict[str, float]:
        """Average over runs per incident (None values skipped)."""
        vals = defaultdict(list)
        for r in records:
            if r["condition"] == condition:
                v = r["metrics"].get(metric)
                if v is not None:
                    vals[r["incident_id"]].append(float(v))
        return {k: statistics.fmean(v) for k, v in vals.items() if v}

    def summarise(self, records: list[dict], matrix: list[Condition]) -> list[dict]:
        b = self.config.bootstrap
        faults = {r["incident_id"]: r["fault_type"] for r in records}
        rows = []
        for cond in matrix:
            recs = [r for r in records if r["condition"] == cond.name]
            row = {
                "condition": cond.name,
                "family": cond.family,
                "description": cond.description,
                "n_incidents": len({r["incident_id"] for r in recs}),
                "n_records": len(recs),
            }
            for metric in (*BINARY, *CONTINUOUS):
                per = self._per_incident(records, cond.name, metric)
                mean, lo, hi = stats.bootstrap_ci(list(per.values()), b.iterations, b.seed, b.alpha)
                row[metric] = None if per == {} else round(mean, 4)
                row[f"{metric}_ci"] = None if per == {} else [round(lo, 4), round(hi, 4)]
                if metric in CLUSTERED and per:
                    keys = sorted(per)
                    _, clo, chi = stats.cluster_bootstrap_ci(
                        [per[k] for k in keys],
                        [faults[k] for k in keys],
                        b.iterations,
                        b.seed,
                        b.alpha,
                    )
                    row[f"{metric}_ci_cluster"] = [round(clo, 4), round(chi, 4)]
            cited = sum(r["metrics"]["first_cited"] for r in recs)
            unsupported = sum(r["metrics"]["first_unsupported"] for r in recs)
            row["hallucination_rate"] = round(unsupported / cited, 4) if cited else 0.0
            row["rejected"] = sum(1 for r in recs if not r["metrics"]["valid"])
            row["repaired"] = sum(1 for r in recs if r["metrics"]["repaired"])
            valid = [r for r in recs if r["metrics"]["valid"]]
            for name in ("verbalized_confidence", "sc_confidence"):
                pairs = [
                    (r["metrics"][name], r["metrics"]["label_correct"])
                    for r in valid
                    if r["metrics"][name] is not None
                ]
                key = "verbalized" if name.startswith("verbal") else "self_consistency"
                if pairs:
                    conf, ok = zip(*pairs, strict=True)
                    row[f"ece_{key}"] = round(stats.expected_calibration_error(conf, ok), 4)
                    row[f"brier_{key}"] = round(stats.brier_score(conf, ok), 4)
                else:
                    row[f"ece_{key}"] = row[f"brier_{key}"] = None
            rows.append(row)
        return rows

    def compare(self, records: list[dict], matrix: list[Condition]) -> list[dict]:
        """Paired comparisons. Pre-declared primary comparisons (config) form one family whose
        cluster-bootstrap p-values are Holm-corrected; all other rows are exploratory and
        unadjusted (D85)."""
        b = self.config.bootstrap
        faults = {r["incident_id"]: r["fault_type"] for r in records}
        names = {c.name for c in matrix}
        primary = {
            (c.condition, c.vs, c.metric)
            for c in self.config.primary_comparisons
            if c.condition in names and c.vs in names
        }
        wanted = [
            (cond.name, "Full", metric)
            for cond in matrix
            if cond.name != "Full"
            for metric in ("label_correct", "root_cause_correct", "cited_recall", "rubric")
        ]
        wanted += sorted(primary - set(wanted))
        out = []
        for name, vs, metric in wanted:
            base = self._per_incident(records, vs, metric)
            other = self._per_incident(records, name, metric)
            common = sorted(set(base) & set(other))
            if not common:
                continue
            a, bb = [other[i] for i in common], [base[i] for i in common]
            diff, lo, hi, p = stats.paired_bootstrap_diff(a, bb, b.iterations, b.seed, b.alpha)
            _, clo, chi, cp = stats.paired_cluster_bootstrap_diff(
                a, bb, [faults[i] for i in common], b.iterations, b.seed, b.alpha
            )
            row = {
                "condition": name,
                "vs": vs,
                "metric": metric,
                "primary": (name, vs, metric) in primary,
                "n": len(common),
                "n_clusters": len({faults[i] for i in common}),
                "diff": round(diff, 4),
                "ci": [round(lo, 4), round(hi, 4)],
                "bootstrap_p": round(p, 4),
                "ci_cluster": [round(clo, 4), round(chi, 4)],
                "cluster_p": round(cp, 4),
            }
            if metric in BINARY:
                m = stats.mcnemar_exact([x >= 0.5 for x in a], [x >= 0.5 for x in bb])
                row.update(
                    {
                        "only_condition_correct": m["a_only"],
                        "only_baseline_correct": m["b_only"],
                        "mcnemar_p": round(m["p_value"], 4),
                    }
                )
            out.append(row)
        prim = [r for r in out if r["primary"]]
        for r, adj in zip(prim, stats.holm([r["cluster_p"] for r in prim]), strict=True):
            r["holm_p"] = round(adj, 4)
        return out

    def stratify(self, records: list[dict], matrix: list[Condition]) -> list[dict]:
        """Per condition and case type (clean = standard, red herring, compound, insufficient
        evidence): run-averaged per incident, then averaged (D90)."""
        rows = []
        for cond in matrix:
            for cat in CATEGORIES:
                ids = {
                    r["incident_id"]
                    for r in records
                    if r["condition"] == cond.name and r["category"] == cat
                }
                if not ids:
                    continue
                row = {"condition": cond.name, "case_type": CASE_TYPE[cat], "n": len(ids)}
                for metric in ("label_correct", "root_cause_correct", "cited_recall", "valid"):
                    per = self._per_incident(
                        [r for r in records if r["incident_id"] in ids], cond.name, metric
                    )
                    row[metric] = round(statistics.fmean(per.values()), 4) if per else None
                rows.append(row)
        return rows

    # ------------------------------------------------------------------ storage
    def write(
        self,
        run_dir: Path,
        manifest: dict,
        summary: list[dict],
        comparisons: list[dict],
        records: list[dict],
        strata: list[dict] | None = None,
    ) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        strata = strata or []

        def dump(name, obj):
            (run_dir / name).write_text(
                json.dumps(obj, indent=2, default=str) + "\n", encoding="utf-8", newline="\n"
            )

        dump("manifest.json", manifest)
        dump("summary.json", summary)
        dump("comparisons.json", comparisons)
        dump("strata.json", strata)
        if strata:
            with open(run_dir / "strata.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(strata[0]))
                w.writeheader()
                w.writerows(strata)
        with open(run_dir / "results.jsonl", "w", encoding="utf-8", newline="\n") as f:
            for r in records:
                f.write(_canonical(r) + "\n")
        flat_cols = ["condition", *(k for k in summary[0] if k not in ("condition",))]
        with open(run_dir / "summary.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=flat_cols)
            w.writeheader()
            for row in summary:
                w.writerow(
                    {k: (json.dumps(v) if isinstance(v, list) else v) for k, v in row.items()}
                )
        if comparisons:
            with open(run_dir / "comparisons.csv", "w", newline="", encoding="utf-8") as f:
                cols = list(dict.fromkeys(k for row in comparisons for k in row))
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                for row in comparisons:
                    w.writerow(
                        {k: (json.dumps(v) if isinstance(v, list) else v) for k, v in row.items()}
                    )
        with open(run_dir / "results.csv", "w", newline="", encoding="utf-8") as f:
            metric_cols = sorted(records[0]["metrics"]) if records else []
            w = csv.DictWriter(
                f, fieldnames=["condition", "incident_id", "run_index", "category", *metric_cols]
            )
            w.writeheader()
            for r in records:
                w.writerow(
                    {
                        "condition": r["condition"],
                        "incident_id": r["incident_id"],
                        "run_index": r["run_index"],
                        "category": r["category"],
                        **r["metrics"],
                    }
                )
        self._spot_check(run_dir, records)
        (run_dir / "report.md").write_text(
            render_report(manifest, summary, comparisons, strata), encoding="utf-8", newline="\n"
        )

    def _spot_check(self, run_dir: Path, records: list[dict]) -> None:
        frac = self.config.spot_check_fraction
        rng = random.Random(f"{self.config.seed}:spot-check")
        pool = [r for r in records if r["condition"] == "Full" and r["run_index"] == 0]
        sample = sorted(
            rng.sample(pool, max(1, round(len(pool) * frac)) if pool else 0),
            key=lambda r: r["incident_id"],
        )
        with open(run_dir / "spot_check.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "incident_id",
                    "reference_root_cause",
                    "reference_resolution",
                    "diagnosis_root_cause",
                    "recommendations",
                    "rubric_auto",
                    "human_score_0_4",
                    "notes",
                ]
            )
            for r in sample:
                d = r["diagnosis"] or {}
                w.writerow(
                    [
                        r["incident_id"],
                        r["ground_truth"]["root_cause_text"],
                        r["ground_truth"]["resolution"],
                        (d.get("root_cause") or {}).get("description", "REJECTED"),
                        " | ".join(x["action"] for x in d.get("recommendations", [])),
                        r["metrics"]["rubric"],
                        "",
                        "",
                    ]
                )
        (run_dir / "judge_prompt.txt").write_text(
            JUDGE_PROMPT + "\n", encoding="utf-8", newline="\n"
        )


def plan(
    config: ExperimentConfig,
    n_incidents: int,
    input_tokens: int = 6500,
    output_tokens: int = 1300,
    input_cost_per_1k: float = 0.0,
    output_cost_per_1k: float = 0.0,
) -> dict:
    """LLM calls and an approximate cost for a config, before running it (repairs excluded;
    they added 0-5% in pilots). Token figures default to the smoke run's per-call averages."""
    rows, calls = [], 0
    for cond in condition_matrix(config):
        runs = config.runs_for(cond.family)
        per = 0 if cond.kind == "rules" else runs * (1 + config.samples_for(cond.name))
        rows.append(
            {"condition": cond.name, "family": cond.family, "runs": runs, "calls_per_incident": per}
        )
        calls += per * n_incidents
    tin, tout = calls * input_tokens, calls * output_tokens
    return {
        "incidents": n_incidents,
        "calls": calls,
        "input_tokens": tin,
        "output_tokens": tout,
        "cost_usd": round(tin / 1000 * input_cost_per_1k + tout / 1000 * output_cost_per_1k, 2),
        "per_condition": rows,
    }


def results_hash(records: list[dict]) -> str:
    def strip(r):
        r = json.loads(_canonical(r))
        for k in VOLATILE:
            r["metrics"].pop(k, None)
        return r

    body = "\n".join(_canonical(strip(r)) for r in records)
    return hashlib.sha256(body.encode()).hexdigest()


def _ci(ci) -> str:
    return "n/a" if not ci else f"[{ci[0]:+.3f}, {ci[1]:+.3f}]"


def _fmt(v, ci=None) -> str:
    if v is None:
        return "n/a"
    s = f"{v:.3f}" if isinstance(v, float) else str(v)
    return f"{s} [{ci[0]:.3f}, {ci[1]:.3f}]" if ci else s


def render_report(
    manifest: dict, summary: list[dict], comparisons: list[dict], strata: list[dict] | None = None
) -> str:
    label = manifest["label"]
    smoke = label == "smoke-test / synthetic"
    lines = [
        f"# Experiment {manifest['experiment_id']} ({manifest['name']}, {manifest['split']} split)"
        f" — {label}",
        "",
    ]
    if smoke:
        lines += [
            f"> **{label.upper()}.** Stub LLM (LOCAL-ONLY) on simulator data. The numbers show "
            "that the matrix runs end to end and is reproducible; they say nothing about real "
            "diagnostic quality. The stub and B1 share keyword rules written against the "
            "simulator's wording (DECISIONS D75, D79).",
            "",
        ]
    lines += [
        f"- Model: `{manifest['model']['provider']}` / `{manifest['model']['model']}`; reported "
        f"versions: {', '.join(manifest['model_versions_reported']) or 'n/a'}",
        f"- Prompt `{manifest['prompt_version']}` (sha `{manifest['prompt_sha']}`); rubric "
        f"`{manifest['rubric_version']}`; embedder `{manifest['embedder']}`",
        f"- Dataset `{manifest['dataset']['version']}` "
        f"(sha `{manifest['dataset']['content_sha256']}`);"
        f" corpus `{manifest['corpus']['version']}` (sha `{manifest['corpus']['sha']}`)",
        f"- {manifest['incidents']} incidents x {len(manifest['conditions'])} conditions; runs: "
        f"{manifest['config']['runs']} (baselines, Full), "
        f"{manifest['config']['runs_ablations'] or manifest['config']['runs']} (ablations), "
        f"{manifest['config']['runs_sweeps'] or manifest['config']['runs']} (sweeps) = "
        f"{manifest['records']} diagnoses; "
        f"source `{manifest['source_sha']}`; results sha256 `{manifest['results_sha256'][:16]}...`",
        f"- Reproduce: `python -m app.experiments verify {manifest['experiment_id']}`",
        "",
        "Accuracy = taxonomy label; root cause = label and resource; top-3 = label among the root "
        "cause and the first two alternatives. Evidence precision/recall compare cited events "
        "with ground-truth evidence; context recall = ground-truth evidence shown to the model. "
        "Hallucination rate = unsupported citations in first answers / citations in first "
        "answers. Brackets: 95% bootstrap interval over incidents; `cl.` = cluster bootstrap "
        "that resamples whole fault types (incidents of one fault type are correlated). ECE "
        "uses 10 bins; with fewer than a few hundred diagnoses per condition it is noisy.",
        "",
        "## Primary comparisons (pre-declared, Holm-corrected)",
        "",
        "Paired over the same incidents on the primary metric; p-values from the cluster "
        "bootstrap, Holm-corrected across this family. Everything below this table is "
        "exploratory and unadjusted.",
        "",
        "| comparison | metric | n (clusters) | diff | 95% CI (cluster) | cluster p | Holm p |",
        "|---|---|---|---|---|---|---|",
        *[
            f"| {c['condition']} vs {c['vs']} | {c['metric']} | {c['n']} ({c['n_clusters']}) | "
            f"{c['diff']:+.3f} | [{c['ci_cluster'][0]:+.3f}, {c['ci_cluster'][1]:+.3f}] | "
            f"{c['cluster_p']:.3f} | {c['holm_p']:.3f} |"
            for c in comparisons
            if c.get("primary")
        ],
        "",
        "## Conditions",
        "",
        "| condition | accuracy | root cause | top-3 | evid. P | evid. R | context R | halluc. | "
        "rubric | rejected | ECE verb. | ECE SC | tokens (in/out) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in summary:
        lines.append(
            f"| {r['condition']} | {_fmt(r['label_correct'], r['label_correct_ci'])} "
            f"cl.{_ci(r.get('label_correct_ci_cluster'))} | "
            f"{_fmt(r['root_cause_correct'], r['root_cause_correct_ci'])} "
            f"cl.{_ci(r.get('root_cause_correct_ci_cluster'))} | "
            f"{_fmt(r['top3_correct'])} | {_fmt(r['cited_precision'])} | "
            f"{_fmt(r['cited_recall'])} | {_fmt(r['context_recall'])} | "
            f"{_fmt(r['hallucination_rate'])} | {_fmt(r['rubric'])} | {r['rejected']} | "
            f"{_fmt(r['ece_verbalized'])} | {_fmt(r['ece_self_consistency'])} | "
            f"{_fmt(r['prompt_tokens'])}/{_fmt(r['completion_tokens'])} |"
        )
    lines += [
        "",
        "## By case type",
        "",
        "| condition | case type | n | accuracy | root cause | evid. R | valid |",
        "|---|---|---|---|---|---|---|",
        *[
            f"| {r['condition']} | {r['case_type']} | {r['n']} | {_fmt(r['label_correct'])} | "
            f"{_fmt(r['root_cause_correct'])} | {_fmt(r['cited_recall'])} | {_fmt(r['valid'])} |"
            for r in strata or []
        ],
        "",
        "Insufficient-evidence cases are correct only when the diagnosis says "
        "`insufficient_evidence`; compound cases are scored on the primary (earlier) fault.",
        "",
        "## Exploratory paired comparisons against Full (unadjusted)",
        "",
        "| condition | metric | n | diff (cond - Full) | 95% CI | 95% CI (cluster) | bootstrap p "
        "| McNemar p |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in comparisons:
        if c["vs"] != "Full":
            continue
        lines.append(
            f"| {c['condition']} | {c['metric']} | {c['n']} | {c['diff']:+.3f} | "
            f"[{c['ci'][0]:+.3f}, {c['ci'][1]:+.3f}] | {_ci(c['ci_cluster'])} | "
            f"{c['bootstrap_p']:.3f} | {c.get('mcnemar_p', 'n/a')} |"
        )
    lines += ["", "## Caveats", ""]
    lines += [
        "- Synthetic incidents; ground truth and fault signatures come from one simulator "
        "(circularity). Real captures are needed before any claim.",
        "- Only the pre-declared primary comparisons are Holm-corrected; the exploratory table "
        "is not, so treat its p-values as descriptive.",
        "- With few fault types (clusters), cluster intervals are wide and coarse; they are the "
        "honest ones when incidents of a fault type share templates.",
        "- The rubric score is a deterministic proxy; real runs add the LLM-judge rubric "
        "(`judge_prompt.txt`) and a human spot check (`spot_check.csv`).",
        "",
    ]
    return "\n".join(lines)


def verify(
    experiment: str | Path,
    out_root: Path = DEFAULT_RUNS,
    dataset: Path = DEFAULT_DATASET,
    corpus_dir: Path = DEFAULT_CORPUS,
    base_settings: Settings | None = None,
) -> dict:
    """Re-run an experiment from its stored manifest and compare. Returns a report dict."""
    run_dir = Path(experiment) if Path(experiment).is_dir() else Path(out_root) / str(experiment)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    config = ExperimentConfig.model_validate(manifest["config"])
    runner = ExperimentRunner(config, dataset, corpus_dir, out_root, base_settings)
    problems = []
    if source_hash() != manifest["source_sha"]:
        problems.append("application source changed since the experiment was run")
    rerun = runner.run(final=config.split == "test", write=False)
    if rerun.experiment_id != manifest["experiment_id"]:
        problems.append(f"inputs give experiment id {rerun.experiment_id}")
    same = rerun.manifest["results_sha256"] == manifest["results_sha256"]
    if not same:
        problems.append("results differ")
    return {
        "experiment_id": manifest["experiment_id"],
        "reproduced": same and not problems,
        "problems": problems,
        "stored_results_sha256": manifest["results_sha256"],
        "rerun_results_sha256": rerun.manifest["results_sha256"],
        "records": len(rerun.records),
    }


def save_to_db(run: ExperimentRun, session: Session) -> None:
    """Persist into evaluation_runs / experiment_results (migration 0001 tables)."""
    from app.db.models import EvaluationRun, ExperimentResult

    m = run.manifest
    session.merge(
        EvaluationRun(
            experiment_id=run.experiment_id,
            split=m["split"],
            label=m["label"],
            dataset_version=m["dataset"]["version"],
            model=m["model"]["model"],
            model_version=", ".join(m["model_versions_reported"]) or None,
            prompt_version=m["prompt_version"],
            config={
                "experiment": m["config"],
                "settings": m["settings"],
                "source_sha": m["source_sha"],
                "results_sha256": m["results_sha256"],
            },
            started_at=datetime.fromisoformat(m["started_at"]),
            finished_at=datetime.fromisoformat(m["finished_at"]),
        )
    )
    session.flush()
    for r in run.records:
        session.add(
            ExperimentResult(
                experiment_id=run.experiment_id,
                incident_id=r["incident_id"],
                condition=r["condition"],
                run_index=r["run_index"],
                retrieved_evidence=r["retrieved_evidence"],
                diagnosis=r["diagnosis"],
                ground_truth=r["ground_truth"],
                metrics=r["metrics"],
            )
        )
    session.commit()
