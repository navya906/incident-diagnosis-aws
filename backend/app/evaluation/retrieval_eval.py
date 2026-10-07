"""Historical retrieval evaluation (dev split) and leakage report.

    python -m app.evaluation.retrieval_eval                    # embeddings provider from config
    python -m app.evaluation.retrieval_eval --embedder hashing # LOCAL-ONLY, no model download

Queries are dev incidents with ground-truth evidence. Each query is built only from what is
known at diagnosis time: the blinded description plus the Phase 4 ranked evidence (top-K) and
candidate-cause summaries. A retrieved past incident is "relevant" when its taxonomy label
equals the query's primary label (the only relevance judgement the synthetic data supports).

Knowledge bases:
- corpus: the separate historical corpus (`historical-v1`, seed 7), built with the test-split
  fingerprints as forbidden values (the leakage guard raises if any matches);
- leave-one-out: the other dev incidents (the query incident is excluded at search time).

Metrics: label@1, label@3 (any of the top 3), MRR over the top 10, all smoke-test / synthetic.
The test split is never queried or indexed here.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

from app.config import Settings, get_settings
from app.evidence.pipeline import InvestigationPipeline
from app.offline.dataset import DEFAULT_SEED, DatasetLoader, generate_dataset
from app.offline.replay import ReplayCollector
from app.rag.corpus import build_corpus, heldout_fingerprints, records_from_dataset
from app.rag.embedders import build_embedder
from app.rag.knowledge_base import HistoricalRetriever, KnowledgeBase, RetrievalQuery, build_query
from app.rag.vector_store import LocalVectorStore

LABEL = "smoke-test / synthetic"
REPO = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = REPO / "data" / "generated" / "synthetic-v1"
DEFAULT_CORPUS = REPO / "data" / "generated" / "historical-v1"
DEFAULT_OUT = REPO / "docs" / "experiments"


def _queries(loader: DatasetLoader, settings: Settings, limit: int | None = None):
    collector = ReplayCollector(loader)
    pipeline = InvestigationPipeline(settings)
    out = []
    for iid in loader.incident_ids("dev")[:limit]:
        truth = loader.load_truth(iid)
        if not truth.ground_truth.evidence_event_ids:
            continue
        inv = pipeline.run_offline(collector, iid)
        s = loader.load(iid)
        causes = [c.rationale for c in inv.correlation.candidate_causes[:3]]
        full = build_query(
            s.incident,
            s.events,
            [i.event_id for i in inv.ranking.items],
            resources=s.resources,
            extra_lines=causes,
        )
        desc = RetrievalQuery(
            text=f"{s.incident.title}. {s.incident.description}",
            alarm_metric=full.alarm_metric,
            resource_types=full.resource_types,
            incident_id=iid,
        )
        out.append((iid, truth.ground_truth.taxonomy_label.value, full, desc))
    return out


def _score(retriever: HistoricalRetriever, queries, use_desc: bool, rerank: bool) -> dict:
    at1, at3, rr, sims, leaked_self = [], [], [], [], 0
    for iid, label, full, desc in queries:
        q = desc if use_desc else full
        hits = retriever.retrieve(q, k=10, rerank=rerank)
        labels = [h.taxonomy_label.value for h in hits]
        leaked_self += any(h.incident_id == iid for h in hits)
        at1.append(float(labels[:1] == [label]))
        at3.append(float(label in labels[:3]))
        rr.append(next((1.0 / (i + 1) for i, x in enumerate(labels) if x == label), 0.0))
        sims.append(hits[0].similarity if hits else 0.0)
    return {
        "label@1": round(statistics.fmean(at1), 3),
        "label@3": round(statistics.fmean(at3), 3),
        "mrr@10": round(statistics.fmean(rr), 3),
        "mean_top1_similarity": round(statistics.fmean(sims), 3),
        "query_returned_itself": leaked_self,
        "queries": len(queries),
    }


def run(
    dataset: Path,
    corpus_dir: Path,
    out_dir: Path,
    settings: Settings,
    limit: int | None = None,
) -> dict:
    loader = DatasetLoader(dataset)
    corpus = build_corpus(corpus_dir, settings.rag.corpus_seed, settings.rag.corpus_version)
    forbidden = heldout_fingerprints(loader)
    embedder = build_embedder(settings)

    corpus_kb = KnowledgeBase(embedder, LocalVectorStore(), "historical", forbidden)
    corpus_kb.build(corpus)
    loo_kb = KnowledgeBase(embedder, LocalVectorStore(), "dev-loo", forbidden)
    loo_kb.build(records_from_dataset(loader, "dev"))

    test_ids = set(loader.incident_ids("test"))
    leakage = {
        "test_incidents": len(test_ids),
        "test_fingerprints": len(forbidden),
        "corpus_indexed": len(corpus_kb.indexed_ids()),
        "test_ids_in_corpus_index": len(test_ids & corpus_kb.indexed_ids()),
        "test_ids_in_loo_index": len(test_ids & loo_kb.indexed_ids()),
        "fingerprint_overlap_corpus": sum(
            len(forbidden & set(r.fingerprints)) for r in corpus_kb.records.values()
        ),
    }

    queries = _queries(loader, settings, limit)
    rows = []
    for kb_name, kb in (("corpus", corpus_kb), ("leave-one-out dev", loo_kb)):
        retriever = HistoricalRetriever(kb, settings.rag)
        for query_name, use_desc in (("description + evidence", False), ("description only", True)):
            for rerank in (False, True):
                rows.append(
                    {
                        "knowledge_base": kb_name,
                        "query": query_name,
                        "ranking": "re-ranked" if rerank else "cosine",
                        **_score(retriever, queries, use_desc, rerank),
                    }
                )
    labels = [r.taxonomy_label.value for r in corpus]
    chance = max(labels.count(x) for x in set(labels)) / len(labels)

    m = loader.manifest
    meta = {
        "label": LABEL,
        "split": "dev",
        "dataset_version": m.dataset_version,
        "content_sha256": m.content_sha256,
        "corpus_version": settings.rag.corpus_version,
        "corpus_seed": settings.rag.corpus_seed,
        "corpus_size": len(corpus),
        "embedder": embedder.model_name,
        "embedder_dimension": embedder.dimension,
        "rag_config": settings.rag.model_dump(),
        "majority_label_rate": round(chance, 3),
    }
    payload = {"meta": meta, "leakage": leakage, "results": rows}
    _write(out_dir, payload)
    return payload


def _write(out_dir: Path, payload: dict) -> None:
    meta, leak, rows = payload["meta"], payload["leakage"], payload["results"]
    cols = [
        "knowledge_base",
        "query",
        "ranking",
        "label@1",
        "label@3",
        "mrr@10",
        "mean_top1_similarity",
        "query_returned_itself",
        "queries",
    ]
    table = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    table += ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows]
    w = meta["rag_config"]["rerank"]
    md = [
        f"# Historical retrieval (dev split) — {LABEL}",
        "",
        f"> **{LABEL.upper()}.** Queries, past incidents and labels all come from the simulator. "
        "These numbers show whether retrieval finds past incidents of the same fault type under "
        "the simulator's assumptions, not real-world usefulness.",
        "",
        f"- Dataset: `{meta['dataset_version']}`, content sha256 `{meta['content_sha256']}`",
        f"- Knowledge base: `{meta['corpus_version']}` (seed {meta['corpus_seed']}, "
        f"{meta['corpus_size']} past incidents, separate from the dataset) and a leave-one-out "
        "knowledge base of the other dev incidents",
        f"- Embedder: `{meta['embedder']}` (dimension {meta['embedder_dimension']})",
        f"- Re-ranking weights: similarity {w['similarity']}, signals {w['signals']}, "
        f"context {w['context']}; {meta['rag_config']['candidates']} candidates",
        "- Command: `python -m app.evaluation.retrieval_eval --embedder hashing`",
        "",
        "label@1: the top past incident has the query's primary taxonomy label. label@3: one of "
        "the top 3 does. MRR@10: mean reciprocal rank of the first same-label incident. "
        f"Majority-label rate in the corpus (what always guessing one label would score): "
        f"{meta['majority_label_rate']}.",
        "",
        "## Results",
        "",
        *table,
        "",
        "## Leakage check",
        "",
        f"- Test incidents: {leak['test_incidents']} ({leak['test_fingerprints']} fingerprints: "
        "incident ids, event ids, description hashes)",
        f"- Test incident ids in the corpus index: **{leak['test_ids_in_corpus_index']}**; "
        f"in the leave-one-out index: **{leak['test_ids_in_loo_index']}**",
        f"- Fingerprint overlap between indexed records and the test split: "
        f"**{leak['fingerprint_overlap_corpus']}**",
        "- Leave-one-out: queries that retrieved themselves: see `query_returned_itself` "
        "(must be 0).",
        "",
        "## Caveats",
        "",
        "- Past incidents and queries share the simulator's templates (log lines, API names, "
        "root-cause wording per fault type), so same-label retrieval is easier than on real "
        "incident history.",
        "- The hashing embedder is a LOCAL-ONLY lexical stand-in; SentenceTransformers or an API "
        "embedder should be re-run with `--embedder` set accordingly before drawing conclusions.",
        "- Taxonomy-label agreement is a proxy for usefulness; Phase 7 measures whether retrieval "
        "changes diagnosis accuracy (Full vs A1).",
        "",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "phase5-retrieval-dev"
    (out_dir / f"{stem}.md").write_text("\n".join(md), encoding="utf-8", newline="\n")
    with open(out_dir / f"{stem}.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=cols)
        wr.writeheader()
        wr.writerows(rows)
    (out_dir / f"{stem}.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8", newline="\n"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Evaluate historical retrieval on the dev split.")
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--embedder", choices=["hashing", "sentence_transformers", "openai", "gemini"])
    args = p.parse_args(argv)
    settings = get_settings()
    if args.embedder:
        settings = settings.model_copy(
            update={
                "embeddings": settings.embeddings.model_copy(update={"provider": args.embedder})
            }
        )
    if not (args.dataset / "manifest.json").is_file():
        print(f"dataset not found at {args.dataset}; generating it (seed {DEFAULT_SEED})")
        generate_dataset(args.dataset, seed=DEFAULT_SEED)
    payload = run(args.dataset, args.corpus, args.out, settings)
    print(f"[{LABEL}] embedder={payload['meta']['embedder']} leakage={payload['leakage']}")
    for r in payload["results"]:
        print(
            f"  {r['knowledge_base']:18s} {r['query']:24s} {r['ranking']:10s} "
            f"@1={r['label@1']} @3={r['label@3']} mrr={r['mrr@10']}"
        )
    print(f"wrote {args.out}/phase5-retrieval-dev.{{md,csv,json}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
