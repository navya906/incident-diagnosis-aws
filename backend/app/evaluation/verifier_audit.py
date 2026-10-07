"""Verifier audit: inject faults into valid diagnoses and measure what the validator catches.

    python -m app.evaluation.verifier_audit        # dev split, writes docs/experiments/

For each dev incident, the stub's valid answer for the Full context is corrupted in one way at a
time, then validated exactly as the engine does:

  fabricated_id      a supporting citation replaced by a well-formed but non-existent evd_ id
  historical_id      a supporting citation replaced by a retrieved past incident's id
  wrong_value        a FACT explanation quotes a number that is not on the cited line
  unsupported_claim  a FACT explanation copied from another evidence line of a different source
                     while keeping the original id (a real id, but the claim is about something
                     else); detected by the lexical support check
  wrong_resource     the root-cause resource replaced by one that is not in the context
  uncited_conclusion every supporting citation removed from a non-insufficient conclusion

"detected" = the answer is rejected, or (for unsupported_claim with rejection off) the
citation is counted as unsupported in the hallucination rate. The unmodified answers measure
the false-positive rate. Synthetic data and stub answers: smoke-test / synthetic (D89).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app.ai.llm_clients import StubLLM
from app.config import Settings, get_settings
from app.diagnosis.context import ContextBuilder
from app.diagnosis.prompts import build_request
from app.diagnosis.validator import validate_output
from app.evidence.pipeline import InvestigationPipeline
from app.offline.dataset import DEFAULT_SEED, DatasetLoader, generate_dataset
from app.offline.replay import ReplayCollector
from app.rag.corpus import build_corpus, heldout_fingerprints
from app.rag.embedders import HashingEmbedder
from app.rag.knowledge_base import HistoricalRetriever, KnowledgeBase, build_query
from app.rag.vector_store import LocalVectorStore

LABEL = "smoke-test / synthetic"
REPO = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = REPO / "data" / "generated" / "synthetic-v1"
DEFAULT_CORPUS = REPO / "data" / "generated" / "historical-v1"
DEFAULT_OUT = REPO / "docs" / "experiments"
KINDS = (
    "fabricated_id",
    "historical_id",
    "wrong_value",
    "unsupported_claim",
    "wrong_resource",
    "uncited_conclusion",
)


def _fake_id(seed: str) -> str:
    return "evd_" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def corrupt(kind: str, data: dict, ctx, retrieved) -> dict | None:
    """One corrupted copy of a valid answer, or None when the kind does not apply."""
    d = json.loads(json.dumps(data))
    support = d["supporting_evidence"]
    if not support:
        return None
    first = support[0]
    ref = ctx.evidence_index.get(first["evidence_id"])
    if kind == "fabricated_id":
        first["evidence_id"] = _fake_id(first["evidence_id"])
    elif kind == "historical_id":
        if not retrieved:
            return None
        first["evidence_id"] = retrieved[0].incident_id
    elif kind == "wrong_value":
        if ref is None:
            return None
        wrong = max([abs(n) for n in ref.numbers] or [0]) * 3 + 17.5
        first["explanation"] = f"{first['explanation']} (observed value {wrong:g})"
        first["label"] = "FACT"
    elif kind == "unsupported_claim":
        if ref is None:
            return None
        other = next(
            (
                r
                for r in ctx.evidence_index.values()
                if r.source != ref.source and r.resource_id != ref.resource_id
            ),
            None,
        ) or next((r for r in ctx.evidence_index.values() if r.source != ref.source), None)
        if other is None:
            return None
        claim = other.line.split(" ", 4)[4] if other.line.count(" ") >= 4 else other.line
        first["explanation"] = "".join(c for c in claim if not c.isdigit()).strip()
        first["label"] = "FACT"
    elif kind == "wrong_resource":
        if d["root_cause"]["taxonomy_label"] == "insufficient_evidence":
            return None
        d["root_cause"]["resource_id"] = "rds/fabricated-database"
    elif kind == "uncited_conclusion":
        if d["root_cause"]["taxonomy_label"] == "insufficient_evidence":
            return None
        d["supporting_evidence"] = []
    return d


def run(
    dataset: Path, corpus_dir: Path, out_dir: Path, settings: Settings, limit: int | None = None
) -> dict:
    loader = DatasetLoader(dataset)
    collector = ReplayCollector(loader)
    kb = KnowledgeBase(
        HashingEmbedder(384), LocalVectorStore(), "historical", heldout_fingerprints(loader)
    )
    kb.build(build_corpus(corpus_dir, settings.rag.corpus_seed, settings.rag.corpus_version))
    retriever = HistoricalRetriever(kb, settings.rag)
    pipeline = InvestigationPipeline(settings)
    builder = ContextBuilder(settings.diagnosis)
    stub = StubLLM()
    thr = settings.diagnosis.review_confidence_threshold
    tol = settings.diagnosis.quote_tolerance
    counts = {k: {"injected": 0, "detected": 0, "rejected_strict": 0} for k in KINDS}
    clean = {"answers": 0, "false_positives": 0}
    for iid in loader.incident_ids("dev")[:limit]:
        s = loader.load(iid)
        inv = pipeline.run_offline(collector, iid)
        q = build_query(s.incident, s.events, [i.event_id for i in inv.ranking.items], s.resources)
        retrieved = retriever.retrieve(q)
        ctx = builder.build(
            incident=s.incident, events=s.events, investigation=inv, retrieved=retrieved
        )
        text = stub.complete(build_request(ctx.text, 0.0, 2048, 0)).text
        base = validate_output(text, ctx, retrieved, thr, tol)
        clean["answers"] += 1
        if not base.valid or base.citation.unsupported:
            clean["false_positives"] += 1
            continue
        data = json.loads(text)
        for kind in KINDS:
            bad = corrupt(kind, data, ctx, retrieved)
            if bad is None:
                continue
            out = validate_output(json.dumps(bad), ctx, retrieved, thr, tol)
            strict = validate_output(json.dumps(bad), ctx, retrieved, thr, tol, True)
            counts[kind]["injected"] += 1
            counts[kind]["detected"] += int(not out.valid or out.citation.unsupported > 0)
            counts[kind]["rejected_strict"] += int(not strict.valid)
    rows = [
        {
            "kind": k,
            **v,
            "detection_rate": round(v["detected"] / v["injected"], 4) if v["injected"] else None,
            "rejection_rate_strict": round(v["rejected_strict"] / v["injected"], 4)
            if v["injected"]
            else None,
        }
        for k, v in counts.items()
    ]
    payload = {
        "meta": {
            "label": LABEL,
            "split": "dev",
            "dataset_version": loader.manifest.dataset_version,
            "content_sha256": loader.manifest.content_sha256,
        },
        "clean": {
            **clean,
            "false_positive_rate": round(clean["false_positives"] / clean["answers"], 4)
            if clean["answers"]
            else None,
        },
        "injections": rows,
    }
    _write(out_dir, payload)
    return payload


def _write(out_dir: Path, payload: dict) -> None:
    rows = payload["injections"]
    md = [
        f"# Verifier audit: injected faults (dev split) — {LABEL}",
        "",
        f"> **{LABEL.upper()}.** Faults injected into the stub's valid answers on simulator data. "
        "This measures the validator's coverage of each fault type, not a model's error rate.",
        "",
        f"- Dataset: `{payload['meta']['dataset_version']}` "
        f"(sha `{payload['meta']['content_sha256']}`)",
        "- Command: `python -m app.evaluation.verifier_audit`",
        f"- Clean answers: {payload['clean']['answers']}; false positives (a clean answer "
        f"rejected or flagged): {payload['clean']['false_positives']} "
        f"(rate {payload['clean']['false_positive_rate']})",
        "",
        "detected = rejected by the validator, or counted as an unsupported citation in the "
        "hallucination rate. strict = rejected with `reject_unsupported_claims: true`.",
        "",
        "| fault | injected | detected | detection rate | rejected (strict) "
        "| rejection rate (strict) |",
        "|---|---|---|---|---|---|",
        *[
            f"| {r['kind']} | {r['injected']} | {r['detected']} | {r['detection_rate']} | "
            f"{r['rejected_strict']} | {r['rejection_rate_strict']} |"
            for r in rows
        ],
        "",
        "## Limits",
        "",
        "- `unsupported_claim` is caught lexically (no shared content word with the cited line). "
        "A false claim that reuses words from the cited line passes; a claim about the same "
        "event in different words would also be flagged. Semantic support needs the LLM judge.",
        "- Numbers below 10 and times are not value-checked; a wrong small count passes.",
        "",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "phase7-verifier-audit.md").write_text("\n".join(md), encoding="utf-8", newline="\n")
    (out_dir / "phase7-verifier-audit.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Inject faults and measure validator detection.")
    p.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(argv)
    if not (args.dataset / "manifest.json").is_file():
        generate_dataset(args.dataset, seed=DEFAULT_SEED)
    payload = run(args.dataset, args.corpus, args.out, get_settings())
    print(f"[{LABEL}] clean false positives: {payload['clean']}")
    for r in payload["injections"]:
        print(
            f"  {r['kind']:20s} {r['detected']}/{r['injected']} detected "
            f"(strict rejects {r['rejected_strict']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
