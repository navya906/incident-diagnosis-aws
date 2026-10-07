"""Phase 7: statistics, metrics, baselines, conditions, experiment runner, reproducibility."""

import json
import math
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.baselines.rules import diagnose_rules
from app.config import DiagnosisSettings, Settings
from app.contracts.evidence import make_evidence_id
from app.db import Base
from app.db.models import EvaluationRun, ExperimentResult
from app.diagnosis.context import EVIDENCE_LINE, ContextBuilder, estimate_tokens
from app.evaluation import stats
from app.evaluation.metrics import rubric_score
from app.evidence.pipeline import InvestigationPipeline
from app.experiments.config import (
    CONDITIONS,
    CORE_CONDITIONS,
    KB_CONDITIONS,
    ExperimentConfig,
    condition_matrix,
)
from app.experiments.runner import (
    DEFAULT_RUNS,
    ExperimentRunner,
    SplitGuardError,
    distractors,
    save_to_db,
    verify,
)
from app.offline.dataset import DatasetLoader, generate_dataset
from app.offline.replay import ReplayCollector
from app.rag.corpus import build_corpus, heldout_fingerprints
from app.rag.embedders import HashingEmbedder
from app.rag.knowledge_base import HistoricalRetriever, KnowledgeBase, LeakageError, build_query
from app.rag.vector_store import LocalVectorStore

REPO = Path(__file__).resolve().parents[2]
SMOKE = REPO / "experiments" / "smoke-dev.yaml"


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("phase7-ds")
    generate_dataset(out, seed=42)
    return out


def smoke_config(**changes) -> ExperimentConfig:
    data = ExperimentConfig.load(SMOKE).model_dump()
    data.update(changes)
    return ExperimentConfig.model_validate(data)


# ------------------------------------------------------------------ statistics
def test_bootstrap_ci_is_deterministic_and_brackets_the_mean():
    x = [0, 1, 1, 0, 1, 1, 1, 0, 1, 1]
    a = stats.bootstrap_ci(x, 2000, seed=3)
    assert a == stats.bootstrap_ci(x, 2000, seed=3)
    mean, lo, hi = a
    assert mean == pytest.approx(0.7) and lo <= mean <= hi and 0 <= lo and hi <= 1
    assert stats.bootstrap_ci([0.4]) == (0.4, 0.4, 0.4)
    assert all(math.isnan(v) for v in stats.bootstrap_ci([]))


def test_paired_bootstrap_and_mcnemar():
    better = [1, 1, 1, 1, 1, 1, 0, 1]
    worse = [0, 0, 1, 0, 0, 1, 0, 0]
    diff, lo, hi, p = stats.paired_bootstrap_diff(better, worse, 2000, 0)
    assert diff == pytest.approx(0.625) and lo > 0 and p < 0.05
    same = stats.paired_bootstrap_diff([1, 0, 1], [1, 0, 1])
    assert same[0] == 0 and same[3] == 1.0
    m = stats.mcnemar_exact([True] * 6 + [False] * 4, [False] * 6 + [False] * 4)
    assert m == {"a_only": 6, "b_only": 0, "p_value": pytest.approx(2 / 64)}
    assert stats.mcnemar_exact([True, False], [True, False])["p_value"] == 1.0


def test_calibration_metrics():
    assert stats.expected_calibration_error([1.0, 1.0, 0.0], [True, True, False]) == 0.0
    assert stats.expected_calibration_error([0.9] * 10, [True] * 5 + [False] * 5) == (
        pytest.approx(0.4)
    )
    assert stats.brier_score([0.9, 0.1], [True, False]) == pytest.approx(0.01)
    assert math.isnan(stats.brier_score([], []))


# ------------------------------------------------------------------ config and matrix
def test_configs_load_and_matrix_is_complete():
    cfg = smoke_config()
    assert cfg.label == "smoke-test / synthetic" and cfg.split == "dev"
    names = [c.name for c in condition_matrix(cfg)]
    assert names[: len(CORE_CONDITIONS)] == list(CORE_CONDITIONS)
    assert set(KB_CONDITIONS) <= set(names)
    assert {f"RQ6-K{k}" for k in (5, 10, 20, 40)} <= set(names)
    assert {f"RQ6-W{w}" for w in (5, 15, 30, 60)} <= set(names)
    assert {"RQ6-Emetrics", "RQ6-E+cloudtrail", "RQ6-E+logs", "RQ6-E+config"} <= set(names)
    assert len(names) == 25 and "Full-no-redaction" in names
    for f in (REPO / "experiments").glob("*.yaml"):
        c = ExperimentConfig.load(f)
        assert "Full" in [x.name for x in condition_matrix(c)]
        if c.split == "test":
            assert c.runs >= 3 and c.label != "smoke-test / synthetic"
    # Full is always added so every condition can be compared against it.
    only = smoke_config(conditions=["B2"], rq6=None, primary_comparisons=[])
    assert [c.name for c in condition_matrix(only)] == ["Full", "B2"]


def test_config_rejects_unknown_conditions_and_secrets():
    with pytest.raises(ValidationError):
        ExperimentConfig(name="x1", conditions=["B9"])
    with pytest.raises(ValidationError):
        ExperimentConfig(name="x1", settings={"llm": {"api_key": "sk-123"}})
    with pytest.raises(ValidationError):
        ExperimentConfig(name="Bad Name")
    ok = ExperimentConfig(name="x1", settings={"evidence": {"top_k": 5}})
    assert ok.settings["evidence"]["top_k"] == 5


def test_ablation_weights_follow_the_brief():
    base = Settings().evidence.weights
    assert CONDITIONS["A2"].weights(base).dependency == 0 and not CONDITIONS["A2"].use_graph
    assert CONDITIONS["A3"].weights(base).anomaly == 0 and not CONDITIONS["A3"].use_anomalies
    assert CONDITIONS["A4"].weights(base).temporal == 0 and not CONDITIONS["A4"].use_chains
    assert CONDITIONS["A5"].ranking_mode == "recency"
    assert not CONDITIONS["A1"].use_rag and CONDITIONS["Full"].weights(base) is None
    b4 = CONDITIONS["B4"]
    assert not b4.use_graph and not b4.use_rag and b4.weights(base).dependency == 0
    assert CONDITIONS["B2"].context_mode == "description"
    assert CONDITIONS["B3"].context_mode == "raw" and CONDITIONS["B1"].kind == "rules"


# ------------------------------------------------------------------ baselines and context modes
def test_b1_rules_produce_valid_cited_diagnoses(dataset):
    loader = DatasetLoader(dataset)
    for iid in loader.incident_ids("dev")[:12]:
        s = loader.load(iid)
        d = diagnose_rules(s.incident, s.events)
        valid_ids = {make_evidence_id(iid, e.event_id) for e in s.events}
        assert {e.evidence_id for e in d.supporting_evidence} <= valid_ids
        if d.root_cause.taxonomy_label.value == "insufficient_evidence":
            assert d.requires_human_review


def test_description_and_raw_contexts(dataset):
    loader = DatasetLoader(dataset)
    s = loader.load(loader.incident_ids("dev")[0])
    builder = ContextBuilder(DiagnosisSettings(context_token_budget=1500))
    b2 = builder.build_description(incident=s.incident, events=s.events)
    assert b2.section_names() == ["incident"]
    assert all(r.source == "alarm" for r in b2.evidence_index.values())
    assert "estimated onset" not in b2.text
    b3 = builder.build_raw(incident=s.incident, events=s.events)
    assert b3.used_tokens <= 1500 + 50
    assert "anomalous" not in b3.text and "## TIMELINE" not in b3.text
    shown = [r.timestamp for r in b3.evidence_index.values() if r.source != "alarm"]
    others = [e.timestamp for e in s.events if e.source.value != "alarm"]
    assert shown and min(shown) >= sorted(others)[-len(shown) - 5]  # the newest events
    lines = [line for line in b3.text.splitlines() if EVIDENCE_LINE.match(line)]
    assert all(estimate_tokens(line) < 200 for line in lines)


def test_recency_ranking_and_label_exclusion(dataset, tmp_path):
    loader = DatasetLoader(dataset)
    collector = ReplayCollector(loader)
    iid = loader.incident_ids("dev")[0]
    p = InvestigationPipeline(Settings())
    inv = p.run_offline(collector, iid, ranking_mode="recency")
    by_id = {e.event_id: e for e in loader.load(iid).events}
    times = [by_id[i.event_id].timestamp for i in inv.ranking.items]
    assert times == sorted(times, reverse=True)
    with pytest.raises(ValueError):
        p.run_offline(collector, iid, ranking_mode="random")
    kb = KnowledgeBase(HashingEmbedder(128), LocalVectorStore(), "h", heldout_fingerprints(loader))
    kb.build(build_corpus(tmp_path / "hist"))
    s = loader.load(iid)
    q = build_query(s.incident, s.events, [i.event_id for i in inv.ranking.items], s.resources)
    label = loader.load_truth(iid).ground_truth.taxonomy_label.value
    out = HistoricalRetriever(kb).retrieve(q, k=10, exclude_labels={label})
    assert out and all(r.taxonomy_label.value != label for r in out)


def test_distractors_are_wrong_label_lookalikes_and_do_not_bypass_the_guard(dataset, tmp_path):
    loader = DatasetLoader(dataset)
    kb = KnowledgeBase(HashingEmbedder(128), LocalVectorStore(), "h", heldout_fingerprints(loader))
    kb.build(build_corpus(tmp_path / "hist"))
    extra = distractors(
        "Title. text", {"metric:X"}, "X", {"rds_instance"}, "deployment_failure", "inc-1", 2
    )
    assert len(extra) == 2 and all(d.taxonomy_label.value != "deployment_failure" for d in extra)
    assert all(d.source == "distractor" and d.fingerprints == [d.incident_id] for d in extra)
    assert extra == distractors(
        "Title. text", {"metric:X"}, "X", {"rds_instance"}, "deployment_failure", "inc-1", 2
    )
    bigger = kb.extended(extra)
    assert bigger.indexed_ids() == kb.indexed_ids() | {d.incident_id for d in extra}
    assert len(kb.indexed_ids()) == 40  # the base knowledge base is untouched
    tid = loader.incident_ids("test")[0]
    bad = extra[0].model_copy(update={"fingerprints": [tid]})
    with pytest.raises(LeakageError):
        kb.extended([bad])


def test_rubric_is_a_bounded_proxy(dataset):
    from app.diagnosis.engine import DiagnosisResult

    loader = DatasetLoader(dataset)
    iid = loader.incident_ids("dev")[0]
    truth = loader.load_truth(iid).ground_truth
    empty = DiagnosisResult.model_construct(diagnosis=None)
    assert rubric_score(empty, truth) == 0.0


# ------------------------------------------------------------------ runner gate
@pytest.fixture(scope="module")
def small_run(dataset, tmp_path_factory):
    out = tmp_path_factory.mktemp("runs")
    corpus = tmp_path_factory.mktemp("corpus")
    cfg = smoke_config(limit=4, bootstrap={"iterations": 200, "seed": 0, "alpha": 0.05})
    run = ExperimentRunner(cfg, dataset, corpus, out).run()
    return run, out, corpus


def test_gate_full_matrix_runs_end_to_end_with_the_stub(small_run):
    run, out, _ = small_run
    conds = {r["condition"] for r in run.records}
    assert len(conds) == 25 and len(run.records) == 25 * 4  # every family runs once here
    assert all(r["metrics"]["valid"] for r in run.records)
    m = run.manifest
    for key in (
        "experiment_id",
        "model",
        "model_versions_reported",
        "prompt_version",
        "prompt_sha",
        "dataset",
        "config",
        "started_at",
        "results_sha256",
        "settings",
    ):
        assert m[key], key
    assert m["label"] == "smoke-test / synthetic" and m["split"] == "dev"
    assert m["model"]["model"] == "stub-deterministic-v1"
    assert "api_key" not in json.dumps(m["settings"])
    rec = run.records[0]
    for key in ("retrieved_evidence", "diagnosis", "ground_truth", "metrics", "run_index"):
        assert key in rec
    files = {p.name for p in run.directory.iterdir()}
    assert {
        "manifest.json",
        "summary.json",
        "summary.csv",
        "comparisons.csv",
        "results.jsonl",
        "results.csv",
        "report.md",
        "spot_check.csv",
        "judge_prompt.txt",
    } <= files
    report = (run.directory / "report.md").read_text(encoding="utf-8")
    assert "SMOKE-TEST / SYNTHETIC" in report and "Exploratory paired comparisons" in report
    full = next(r for r in run.summary if r["condition"] == "Full")
    assert full["label_correct_ci"] and full["hallucination_rate"] == 0.0
    assert {c["condition"] for c in run.comparisons} == conds - {"Full"}
    b1 = [r for r in run.records if r["condition"] == "B1"]
    assert all(r["metrics"]["context_recall"] is None for r in b1)
    assert all(0 <= r["metrics"]["rubric"] <= 1 for r in run.records)


def test_gate_reproducible_by_experiment_id(small_run, dataset):
    run, out, corpus = small_run
    report = verify(run.experiment_id, out, dataset, corpus)
    assert report["reproduced"], report
    assert report["rerun_results_sha256"] == run.manifest["results_sha256"]


def test_experiment_id_changes_with_inputs(dataset, tmp_path):
    a = smoke_config(limit=1, conditions=["B2"], rq6=None, primary_comparisons=[])
    b = a.model_copy(update={"self_consistency_samples": 0})
    ra = ExperimentRunner(a, dataset, tmp_path / "c", tmp_path / "r").run(write=False)
    rb = ExperimentRunner(b, dataset, tmp_path / "c", tmp_path / "r").run(write=False)
    assert ra.experiment_id != rb.experiment_id
    again = ExperimentRunner(a, dataset, tmp_path / "c", tmp_path / "r").run(write=False)
    assert again.experiment_id == ra.experiment_id
    assert again.manifest["results_sha256"] == ra.manifest["results_sha256"]


def test_test_split_needs_final(dataset, tmp_path):
    cfg = smoke_config(split="test", limit=1, conditions=["B2"], rq6=None, primary_comparisons=[])
    runner = ExperimentRunner(cfg, dataset, tmp_path / "c", tmp_path / "r")
    with pytest.raises(SplitGuardError):
        runner.run()
    run = runner.run(final=True, write=False)
    assert run.manifest["split"] == "test"
    test_ids = set(DatasetLoader(dataset).incident_ids("test"))
    assert {r["incident_id"] for r in run.records} <= test_ids


def test_results_can_be_stored_in_the_database(small_run, tmp_path):
    run, _, _ = small_run
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'e.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        save_to_db(run, s)
        assert s.scalar(select(func.count()).select_from(ExperimentResult)) == len(run.records)
        row = s.get(EvaluationRun, run.experiment_id)
        assert row.prompt_version == "diag-v1" and row.dataset_version == "synthetic-v1"
        assert row.config["results_sha256"] == run.manifest["results_sha256"]


def test_cli_runs_lists_and_refuses_test(dataset, tmp_path, capsys):
    from app.experiments.__main__ import main

    cfg = tmp_path / "c.yaml"
    data = yaml.safe_load(SMOKE.read_text(encoding="utf-8"))
    data.update({"conditions": ["B2"], "rq6": None, "limit": 1, "primary_comparisons": []})
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    common = [
        "--dataset",
        str(dataset),
        "--corpus",
        str(tmp_path / "corpus"),
        "--out",
        str(tmp_path / "runs"),
    ]
    assert main(["run", "--config", str(cfg), *common]) == 0
    assert main(["run", "--config", str(cfg), "--split", "test", *common]) == 2
    assert main(["list", "--out", str(tmp_path / "runs")]) == 0
    assert "smoke-dev" in capsys.readouterr().out


# ------------------------------------------------------------------ committed artefacts
def test_committed_smoke_run_is_complete_and_labelled():
    manifests = [json.loads(p.read_text("utf-8")) for p in DEFAULT_RUNS.glob("*/manifest.json")]
    smoke = [m for m in manifests if m["name"] == "smoke-dev" and m["config"]["limit"] is None]
    assert smoke, (
        "commit the gate run: python -m app.experiments run --config ../experiments/smoke-dev.yaml"
    )
    m = smoke[-1]
    assert m["label"] == "smoke-test / synthetic" and m["split"] == "dev"
    assert len(m["conditions"]) == 25
    assert m["records"] == sum(m["runs_per_condition"].values()) * m["incidents"]
    assert (DEFAULT_RUNS / m["experiment_id"] / "strata.csv").is_file()
    report = (DEFAULT_RUNS / m["experiment_id"] / "report.md").read_text("utf-8")
    assert "SMOKE-TEST / SYNTHETIC" in report


def test_runbook_gives_exact_real_llm_commands_for_the_test_split():
    text = (REPO / "docs" / "experiments" / "RUNBOOK.md").read_text(encoding="utf-8")
    for needle in (
        "CLOUDDIAG_LLM__API_KEY",
        "--config ../experiments/real-test-openai.yaml --final",
        "--config ../experiments/real-test-gemini.yaml --final",
        "python -m app.experiments verify",
        "real-dev-pilot.yaml",
        "spot_check.csv",
    ):
        assert needle in text, needle
    assert "sk-" not in text


# ------------------------------------------------------------------ hardening (D85-D90)
def test_cluster_bootstrap_and_holm():
    values = [1, 1, 1, 0, 0, 0, 1, 0]
    clusters = ["a", "a", "a", "b", "b", "b", "c", "c"]
    mean, lo, hi = stats.cluster_bootstrap_ci(values, clusters, 2000, 0)
    plain = stats.bootstrap_ci(values, 2000, 0)
    assert mean == plain[0] and (hi - lo) >= (plain[2] - plain[1])  # clustering widens the CI
    assert stats.cluster_bootstrap_ci([1, 0], ["a", "a"]) == (0.5, 0.5, 0.5)
    d = stats.paired_cluster_bootstrap_diff([1, 1, 1, 1], [0, 0, 1, 0], ["a", "a", "b", "b"])
    assert d[0] == pytest.approx(0.75) and 0 <= d[3] <= 1
    assert stats.holm([0.01, 0.04, 0.03, 0.2]) == pytest.approx([0.04, 0.09, 0.09, 0.2])
    assert stats.holm([0.5]) == [0.5]
    adjusted = stats.holm([0.001, float("nan")])
    assert adjusted[0] == 0.001 and math.isnan(adjusted[1])


def test_primary_comparisons_are_predeclared_and_holm_corrected(small_run):
    run, _, _ = small_run
    primary = [c for c in run.comparisons if c["primary"]]
    declared = {(c["condition"], c["vs"], c["metric"]) for c in run.manifest["primary_comparisons"]}
    assert {(c["condition"], c["vs"], c["metric"]) for c in primary} == declared
    assert len(primary) == 6 and all("holm_p" in c and "cluster_p" in c for c in primary)
    assert all(c["holm_p"] >= c["cluster_p"] for c in primary)
    assert all("holm_p" not in c for c in run.comparisons if not c["primary"])
    report = (run.directory / "report.md").read_text(encoding="utf-8")
    assert "Primary comparisons (pre-declared, Holm-corrected)" in report
    assert "Exploratory paired comparisons" in report
    with pytest.raises(ValidationError):
        smoke_config(conditions=["B2"], rq6=None)  # declared primaries (B3, B4, ...) not in run


def test_results_are_stratified_by_case_type(small_run):
    run, _, _ = small_run
    types = {r["case_type"] for r in run.strata}
    assert types <= {"clean", "red-herring", "compound", "insufficient-evidence"}
    full = [r for r in run.strata if r["condition"] == "Full"]
    assert sum(r["n"] for r in full) == run.manifest["incidents"]
    full_summary = next(r for r in run.summary if r["condition"] == "Full")
    assert full_summary["label_correct_ci_cluster"]


def test_case_types_cover_all_four_on_dev(dataset, tmp_path):
    cats = {
        DatasetLoader(dataset).load_truth(i).meta.category
        for i in DatasetLoader(dataset).incident_ids("dev")
    }
    assert cats == {"standard", "red_herring", "compound", "insufficient_evidence"}


def test_full_without_redaction_runs_offline_and_fails_closed_in_aws_mode(dataset, tmp_path):
    from app.ai.clients import RedactionPolicyError

    cfg = smoke_config(limit=1, conditions=["Full-no-redaction"], rq6=None, primary_comparisons=[])
    run = ExperimentRunner(cfg, dataset, tmp_path / "c", tmp_path / "r").run(write=False)
    assert {r["condition"] for r in run.records} == {"Full", "Full-no-redaction"}
    aws = Settings(data_mode="aws")
    runner = ExperimentRunner(cfg, dataset, tmp_path / "c", tmp_path / "r", base_settings=aws)
    real = runner.settings.model_copy(
        update={
            "llm": runner.settings.llm.model_copy(
                update={
                    "provider": "openai_compatible",
                    "api_key": None,
                    "base_url": "http://localhost:1",
                }
            )
        }
    )
    runner.settings = real
    with pytest.raises(RedactionPolicyError):
        runner.run(write=False)


def test_cheaper_plan_and_cost_estimate():
    from app.experiments.runner import plan

    cfg = ExperimentConfig.load(REPO / "experiments" / "real-test-openai.yaml")
    assert cfg.runs == 3 and cfg.runs_for("ablation") == 1 and cfg.runs_for("sweep") == 1
    assert cfg.samples_for("Full") == 5 and cfg.samples_for("B4") == 5
    assert cfg.samples_for("A1") == 0 and cfg.samples_for("RQ6-K5") == 0
    result = plan(cfg, 38, 6500, 1300, 0.0025, 0.01)
    per = {r["condition"]: r["calls_per_incident"] for r in result["per_condition"]}
    assert per["Full"] == 18 and per["B4"] == 18 and per["B2"] == 3 and per["B1"] == 0
    assert per["A1"] == 1 and per["RQ6-K5"] == 1 and per["Full-no-redaction"] == 1
    assert result["calls"] == 38 * sum(per.values()) == 2356
    old = cfg.model_copy(
        update={"runs_ablations": None, "runs_sweeps": None, "self_consistency_conditions": None}
    )
    assert plan(old, 38)["calls"] > 6 * result["calls"]
    runbook = (REPO / "docs" / "experiments" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "2,356" in runbook and "python -m app.experiments plan" in runbook


def test_judge_must_be_a_different_model_family():
    from app.experiments.config import model_family

    assert model_family("openai_compatible", "gpt-4o-2024-08-06") == "openai"
    assert model_family("gemini", "gemini-1.5-pro-002") == "google"
    assert model_family("openai_compatible", "llama-3.1-70b") == "meta"
    base = {"name": "j1", "settings": {"llm": {"provider": "gemini", "model": "gemini-1.5-pro"}}}
    with pytest.raises(ValidationError, match="must differ"):
        ExperimentConfig(
            **base, judge={"enabled": True, "provider": "gemini", "model": "gemini-1.5-flash"}
        )
    ok = ExperimentConfig(
        **base, judge={"enabled": True, "provider": "openai_compatible", "model": "gpt-4o"}
    )
    assert "different model family" in ok.judge.note
    for f in (REPO / "experiments").glob("*.yaml"):
        text = f.read_text(encoding="utf-8")
        assert "different model family" in text, f.name
        ExperimentConfig.load(f)


def test_verifier_audit_detects_injected_faults(dataset, tmp_path):
    """Fabricated ids, historical ids, wrong values, unsupported claims, unknown resources and
    uncited conclusions are injected into valid answers; detection rates are reported."""
    from app.evaluation import verifier_audit

    payload = verifier_audit.run(dataset, tmp_path / "c", tmp_path / "out", Settings(), limit=25)
    rates = {r["kind"]: r for r in payload["injections"]}
    assert payload["clean"]["false_positives"] == 0
    for kind in (
        "fabricated_id",
        "historical_id",
        "wrong_value",
        "wrong_resource",
        "uncited_conclusion",
    ):
        assert rates[kind]["injected"] > 0 and rates[kind]["detection_rate"] == 1.0, kind
    assert rates["unsupported_claim"]["detection_rate"] >= 0.8
    md = (tmp_path / "out" / "phase7-verifier-audit.md").read_text(encoding="utf-8")
    assert "SMOKE-TEST / SYNTHETIC" in md and "detection rate" in md


def test_committed_verifier_audit_is_labelled():
    data = json.loads((REPO / "docs" / "experiments" / "phase7-verifier-audit.json").read_text())
    assert data["meta"]["label"] == "smoke-test / synthetic"
    assert data["clean"]["false_positive_rate"] == 0.0
