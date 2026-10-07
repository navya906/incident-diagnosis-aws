"""End-to-end diagnosis smoke run on the dev split with the stub LLM (Phase 6 gate).

    python -m app.evaluation.diagnosis_e2e                  # stub LLM, hashing embedder
    python -m app.evaluation.diagnosis_e2e --samples 5

Measures that the pipeline works, not that it diagnoses well: output validity, repairs and
rejections, citation verification (unsupported-citation rate), context budget use,
self-consistency, deterministic severity, and the same for each ablation condition. The stub's
taxonomy agreement is printed only to show the plumbing; its keyword rules were written against
the simulator's own wording, so it is NOT a result (DECISIONS D75). Real-LLM runs belong to
Phase 7 on the test split.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

from app.ai.llm_clients import build_llm_client
from app.config import Settings, get_settings
from app.diagnosis.engine import DiagnosisEngine, DiagnosisResult
from app.offline.dataset import DEFAULT_SEED, DatasetLoader, generate_dataset
from app.offline.replay import ReplayCollector
from app.rag.corpus import build_corpus, heldout_fingerprints
from app.rag.embedders import build_embedder
from app.rag.knowledge_base import HistoricalRetriever, KnowledgeBase
from app.rag.vector_store import LocalVectorStore

LABEL = "smoke-test / synthetic"
REPO = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = REPO / "data" / "generated" / "synthetic-v1"
DEFAULT_CORPUS = REPO / "data" / "generated" / "historical-v1"
DEFAULT_OUT = REPO / "docs" / "experiments"
CONDITIONS = {
    "Full": {},
    "A1 no RAG": {"use_rag": False},
    "A2 no graph": {"use_graph": False},
    "A3 no anomaly detection": {"use_anomalies": False},
    "A4 no chains": {"use_chains": False},
}


def _summary(name: str, results: list[DiagnosisResult], truth: dict[str, str]) -> dict:
    valid = [r for r in results if r.valid]
    repaired = sum(1 for r in results if any(a.kind == "primary-repair" for a in r.attempts))
    cited = [len(r.citation.cited) for r in valid]
    unsupported = sum(r.citation.unsupported for r in valid)
    sc = [r.self_consistency.agreement_with_primary for r in valid]
    sc = [x for x in sc if x is not None]
    agree = sum(
        1 for r in valid if r.diagnosis.root_cause.taxonomy_label.value == truth[r.incident_id]
    )
    return {
        "condition": name,
        "incidents": len(results),
        "valid": len(valid),
        "repaired": repaired,
        "rejected": len(results) - len(valid),
        "mean_citations": round(statistics.fmean(cited), 2) if cited else 0,
        "unsupported_citation_rate": round(unsupported / sum(cited), 4) if sum(cited) else 0.0,
        "requires_review": sum(1 for r in valid if r.diagnosis.requires_human_review),
        "historical_used": sum(1 for r in valid if r.diagnosis.historical_influence.used),
        "mean_context_tokens": round(statistics.fmean(r.context_tokens for r in results)),
        "items_dropped_by_budget": sum(
            s.items_dropped for r in results for s in r.context_sections
        ),
        "mean_self_consistency": round(statistics.fmean(sc), 3) if sc else None,
        "stub_label_agreement_NOT_A_RESULT": round(agree / len(results), 3) if results else None,
    }


def run(
    dataset: Path,
    corpus_dir: Path,
    out_dir: Path,
    settings: Settings,
    samples: int = 3,
    limit: int | None = None,
    conditions: dict[str, dict] | None = None,
) -> dict:
    loader = DatasetLoader(dataset)
    collector = ReplayCollector(loader)
    kb = KnowledgeBase(
        build_embedder(settings), LocalVectorStore(), "historical", heldout_fingerprints(loader)
    )
    kb.build(build_corpus(corpus_dir, settings.rag.corpus_seed, settings.rag.corpus_version))
    llm = build_llm_client(settings)
    engine = DiagnosisEngine(llm, settings, HistoricalRetriever(kb, settings.rag))
    ids = collector.incident_ids("dev")[:limit]
    truth = {i: loader.load_truth(i).ground_truth.taxonomy_label.value for i in ids}

    rows, full_results = [], []
    for name, kwargs in (conditions or CONDITIONS).items():
        res = [
            engine.diagnose_offline(
                collector, i, condition=name, samples=samples if name == "Full" else 0, **kwargs
            )
            for i in ids
        ]
        rows.append(_summary(name, res, truth))
        if name == "Full":
            full_results = res

    sev = Counter(r.severity.level.value for r in full_results)
    advisory = Counter(
        (r.severity.level.value, r.llm_severity_suggestion) for r in full_results if r.valid
    )
    same = sum(v for (a, b), v in advisory.items() if a == b)
    unknown = Counter(f.name for r in full_results for f in r.severity.factors if not f.known)
    m = loader.manifest
    payload = {
        "meta": {
            "label": LABEL,
            "split": "dev",
            "dataset_version": m.dataset_version,
            "content_sha256": m.content_sha256,
            "model": llm.model_name,
            "prompt_version": full_results[0].prompt_version if full_results else None,
            "prompt_sha": full_results[0].prompt_sha if full_results else None,
            "embedder": kb.embedder.model_name,
            "self_consistency_samples": samples,
            "context_token_budget": settings.diagnosis.context_token_budget,
        },
        "conditions": rows,
        "severity": {
            "levels": dict(sorted(sev.items())),
            "advisory_matches_engine": same,
            "valid_diagnoses": sum(1 for r in full_results if r.valid),
            "factors_unknown": dict(sorted(unknown.items())),
        },
    }
    _write(out_dir, payload)
    return payload


def _write(out_dir: Path, payload: dict) -> None:
    meta, rows, sev = payload["meta"], payload["conditions"], payload["severity"]
    cols = list(rows[0]) if rows else []
    table = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    table += ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows]
    md = [
        f"# Diagnosis engine end-to-end smoke run (dev split) — {LABEL}",
        "",
        f"> **{LABEL.upper()}.** Stub LLM (`{meta['model']}`, LOCAL-ONLY) on simulator data. This "
        "shows that every stage runs and validates, not how well anything diagnoses. The stub's "
        "keyword rules were written against the simulator's wording, so its label agreement is "
        "**not a result** (DECISIONS D75).",
        "",
        f"- Dataset: `{meta['dataset_version']}`, content sha256 `{meta['content_sha256']}`",
        f"- Prompt: `{meta['prompt_version']}` (sha `{meta['prompt_sha']}`); context budget "
        f"{meta['context_token_budget']} tokens; embedder `{meta['embedder']}`; "
        f"{meta['self_consistency_samples']} self-consistency samples (Full only)",
        "- Command: `python -m app.evaluation.diagnosis_e2e`",
        "",
        "## Conditions",
        "",
        *table,
        "",
        "valid = passed the validator (schema, citations exist in the context, quoted values "
        "match, root-cause resource known, historical rules), possibly after the one repair. "
        "unsupported_citation_rate = cited ids that are unknown or misquoted / all cited ids "
        "(0 for every valid answer by construction; the validator rejects the rest).",
        "",
        "## Severity (deterministic engine, Full)",
        "",
        f"- Levels: {sev['levels']}",
        f"- The stub's advisory `severity_suggestion` equals the engine's level in "
        f"{sev['advisory_matches_engine']} of {sev['valid_diagnoses']} valid diagnoses (advisory "
        "only; the engine's level is the one reported).",
        f"- Factors without data (counts over incidents): {sev['factors_unknown'] or 'none'}",
        "",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "phase6-e2e-dev.md").write_text("\n".join(md), encoding="utf-8", newline="\n")
    (out_dir / "phase6-e2e-dev.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8", newline="\n"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="End-to-end diagnosis smoke run on dev (stub LLM).")
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--samples", type=int, default=3)
    args = p.parse_args(argv)
    base = get_settings()
    settings = base.model_copy(
        update={
            "llm": base.llm.model_copy(update={"provider": "stub", "model": "stub-deterministic"}),
            "embeddings": base.embeddings.model_copy(update={"provider": "hashing"}),
        }
    )
    if not (args.dataset / "manifest.json").is_file():
        print(f"dataset not found at {args.dataset}; generating it (seed {DEFAULT_SEED})")
        generate_dataset(args.dataset, seed=DEFAULT_SEED)
    payload = run(args.dataset, args.corpus, args.out, settings, samples=args.samples)
    print(f"[{LABEL}] model={payload['meta']['model']}")
    for r in payload["conditions"]:
        print(
            f"  {r['condition']:24s} valid {r['valid']}/{r['incidents']} repaired={r['repaired']} "
            f"rejected={r['rejected']} unsupported={r['unsupported_citation_rate']}"
        )
    print(f"  severity levels: {payload['severity']['levels']}")
    print(f"wrote {args.out}/phase6-e2e-dev.{{md,json}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
