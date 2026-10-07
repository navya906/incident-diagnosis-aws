# Evaluation and experiment methodology

How the system is evaluated, how experiments are identified and reproduced, and what the current
results are. **Every result below is smoke-test / synthetic:** simulator incidents and the
deterministic LOCAL-ONLY stub LLM. They show that the evaluation machinery works and how the
deterministic components behave on the simulator; they are not evidence of diagnostic quality on
real incidents (`docs/known-limitations.md`). Real-LLM runs: `docs/experiments/RUNBOOK.md`.

## Research questions

| RQ | Question | Measured by |
|---|---|---|
| RQ1 | Does evidence selection + correlation + dependency reasoning + retrieval + LLM beat simpler approaches? | Full vs B1-B4 |
| RQ2 | What does each component contribute? | Full vs A1-A5 |
| RQ3 | Are the diagnoses faithful to the evidence? | citation verification, hallucination rate, verifier audit |
| RQ4 | Is the stated confidence calibrated? | ECE and Brier for verbalised and self-consistency confidence |
| RQ5 | Does retrieved history help or mislead? | Full vs A1 under the knowledge-base conditions (fault type removed, distractors) |
| RQ6 | How sensitive is the result to K, the time window and the evidence types? | sweeps |

## Data and leakage control

- **Dataset** `synthetic-v1` (generator 1.1.0, seed 42, content sha256 `06cec245...`): 124
  incidents, 10 fault types x 10 variants plus 12 red-herring, 6 insufficient-evidence and 6
  compound cases (`docs/offline-dataset.md`).
- **Split:** stratified dev (86) / test (38). Rules, prompts, weights and thresholds are tuned on
  dev only. The runners refuse the test split without `--final`; it is reserved for the final
  real-LLM run.
- **Blinded descriptions:** symptoms only (alarm metric and resource); a test checks that no
  description names a root cause; observable and ground-truth files are separate.
- **RAG leakage control:** the historical knowledge base is a separate corpus (`historical-v1`,
  seed 7, own scenario keys); a guard refuses any record matching a test incident id, event id
  or description hash; leave-one-out on dev never returns the query (D62, D63).

## Conditions (BRIEF Section 4)

| ID | Condition |
|---|---|
| B1 | Rule-based diagnosis (rules written on dev) |
| B2 | LLM + description only |
| B3 | LLM + description + raw telemetry, newest first, same token budget |
| B4 | LLM + description + ranked evidence (no graph, no RAG) |
| Full | B4 + dependency graph + historical RAG |
| A1-A5 | Full minus RAG / dependency graph / anomaly detection / temporal correlation / evidence ranking (recency instead) |
| Full-KB-fault-removed, Full-KB-distractors | Full with the query's fault type removed from the knowledge base, or with distractor entries (D71) |
| Full-no-redaction | Full without redaction, offline only (the cost of redaction) (D87) |
| RQ6 | Full with K in {5, 10, 20, 40}, window in {5, 15, 30, 60} min, evidence types metrics / +CloudTrail / +logs / +Config |

## Metrics

Per diagnosis (D81): taxonomy accuracy; resource match; **root-cause match = label and resource
(primary metric)**; top-3 label; cited-evidence precision and recall against ground-truth
evidence; context recall (ground-truth evidence shown to the model); **hallucination rate =
unsupported citations in first answers / citations**; rejected and repaired counts; verbalised
and self-consistency confidence; a deterministic recommendation rubric (`rubric-v1`); latency,
tokens, estimated cost; severity. Insufficient-evidence cases are correct only when the diagnosis
says `insufficient_evidence`; compound cases are scored on the primary (earlier) fault; invalid
diagnoses count as wrong. For real runs the free-text root cause is also scored by an LLM judge
from a different model family (D88) and a seeded 10% sample goes to human review.

## Statistics

- N runs per condition (1 with the deterministic stub; at least 3 for real models).
- 95% bootstrap intervals (2,000 resamples), plus **cluster bootstrap by fault type** (11
  clusters), because incidents of one fault type share templates (D85).
- Paired comparisons on the same incidents: bootstrap difference and exact McNemar test.
- **Primary comparisons are declared in the config before the run** (B2, B3, B4, A1, A2, A5 vs
  Full on root-cause match) and Holm-corrected as one family; all other comparisons are
  exploratory and unadjusted.
- Calibration: ECE (10 equal-width bins) and Brier score, with the small-sample caveat.
- Strata by case type (clean, red-herring, compound, insufficient-evidence).

## Experiment ids and reproducibility

An experiment is a YAML config (`experiments/*.yaml`) run by
`python -m app.experiments run --config <file>`. Its id is

`exp-` + sha256(config, dataset hash, corpus hash, prompt hash, model, embedder, hash of `backend/app/**/*.py`)[:12]

so the same id means the same inputs and code (D82). The run directory
`docs/experiments/runs/<id>/` holds the manifest (versions, settings without secrets, git
commit, library versions, results hash), summaries, paired comparisons, strata, the report, the
human spot-check sample and the judge prompt; the per-record `results.jsonl` is regenerable
from the id. `python -m app.experiments verify <id>` re-runs the stored config and compares the
results hash; a code change alone is reported as a note, a change in results as a failure
(D107). CI verifies the experiment cited below on every push.

Component evaluations (detectors, evidence ranking, retrieval, the diagnosis smoke run, the
verifier audit) are separate CLIs whose reports carry content-addressed `rpt-` ids (D110);
`python -m app.evaluation.provenance check` verifies them and re-running the command printed in
each report reproduces its id.

## Results index

| Id | What | Command |
|---|---|---|
| `exp-7e89b5a0473d` | full condition matrix, dev, stub (`docs/experiments/runs/exp-7e89b5a0473d/report.md`) | `python -m app.experiments run --config ../experiments/smoke-dev.yaml` |
| `rpt-40fc8fb64c37` | anomaly detector comparison, dev (`docs/experiments/phase3-anomaly-detectors-dev.md`) | `python -m app.evaluation.anomaly_eval --sweep` |
| `rpt-d9d6019400b2` | evidence ranking P/R@K and candidate causes, dev (`docs/experiments/phase4-evidence-ranking-dev.md`) | `python -m app.evaluation.evidence_eval --tune` |
| `rpt-6dc7127fdb57` | historical retrieval and leakage, dev (`docs/experiments/phase5-retrieval-dev.md`) | `python -m app.evaluation.retrieval_eval --embedder hashing` |
| `rpt-136edb46e231` | diagnosis pipeline smoke run, dev (`docs/experiments/phase6-e2e-dev.md`) | `python -m app.evaluation.diagnosis_e2e` |
| `rpt-fa410c0154c9` | citation-verifier audit with injected faults, dev (`docs/experiments/phase7-verifier-audit.md`) | `python -m app.evaluation.verifier_audit` |

`exp-7e89b5a0473d` replaces the Phase 7 run `exp-fbeacbc7cd9b` as the cited run: the code changed
in Phases 8-10, so the id changed, but `verify` shows both runs have identical results.

## Results (smoke-test / synthetic)

### Condition matrix: `exp-7e89b5a0473d` (dev, 86 incidents, stub LLM, 1 run)

| Condition | Root-cause match [95% CI, cluster] | Accuracy | Cited evidence recall | Context recall | Hallucination | Rejected |
|---|---|---|---|---|---|---|
| B1 rules | 0.674 [0.518, 0.833] | 0.686 | 0.263 | n/a | 0.000 | 0 |
| B2 description only | 0.046 [0.000, 0.158] | 0.046 | 0.000 | 0.000 | 0.000 | 0 |
| B3 raw telemetry | 0.174 [0.000, 0.405] | 0.186 | 0.009 | 0.524 | 0.000 | 0 |
| B4 ranked evidence | 0.779 [0.605, 0.915] | 0.930 | 0.300 | 0.502 | 0.000 | 0 |
| **Full** | **0.895 [0.789, 0.977]** | 0.965 | 0.372 | 0.601 | 0.000 | 0 |
| A1 no RAG | 0.895 [0.789, 0.977] | 0.965 | 0.372 | 0.601 | 0.000 | 0 |
| A2 no graph | 0.779 [0.605, 0.915] | 0.930 | 0.300 | 0.502 | 0.000 | 0 |
| A3 no anomaly detection | 0.942 [0.892, 0.988] | 0.977 | 0.281 | 0.263 | 0.000 | 0 |
| A4 no temporal correlation | 0.698 [0.474, 0.909] | 0.779 | 0.305 | 0.600 | 0.000 | 0 |
| A5 recency, no ranking | 0.965 [0.907, 1.000] | 0.977 | 0.305 | 0.014 | 0.000 | 0 |

Primary comparisons (root-cause match, paired, cluster bootstrap, Holm-corrected): B2 vs Full
-0.849 (Holm p < 0.001), B3 -0.721 (< 0.001), B4 -0.116 (0.008), A2 -0.116 (0.008), A1 0.000
(1.0), A5 +0.070 (0.376). The knowledge-base conditions and `Full-no-redaction` equal Full
exactly. RQ6 (root-cause match): K = 5 / 10 / 20 / 40 gives 0.942 / 0.895 / 0.872 / 0.837;
windows of 15-60 min give 0.884-0.895 and 5 min 0.686; metrics alone 0.186, + CloudTrail 0.581,
+ logs 0.895. Calibration of Full: ECE 0.161 (verbalised), 0.046 (self-consistency). All
numbers: `docs/experiments/runs/exp-7e89b5a0473d/report.md`.

**How to read this.** The table shows that each condition changes what the model sees as
designed (context recall moves as expected, B2 and metrics-only contexts leave nothing to cite)
and that the statistics run. It does not show what a real model would do:

- the stub's keyword rules were written against the simulator's wording (D75), so its accuracy
  is high wherever the right lines are in the context;
- A1, the knowledge-base conditions and `Full-no-redaction` equal Full because the stub ignores
  history and is local (redaction never applies); only a real LLM can answer RQ5;
- A3 and A5 score at or above Full because the stub still reads the candidate-cause timeline;
  this is a stub artifact, not a finding about anomaly detection or ranking;
- the zero hallucination rate holds by construction (the stub cites only lines it was shown);
  the verifier audit below is the meaningful faithfulness check so far.

### Component results

| Report | Main result on dev (smoke-test / synthetic) |
|---|---|
| `rpt-40fc8fb64c37` detectors | rolling std (threshold 3.0) has the best balance: precision 0.789, recall 0.824, F1 0.806, series false-alarm rate 0.053; z-score at 3.0 reaches recall 0.856 at precision 0.680; textbook thresholds give many false alarms on autocorrelated noise (D46). |
| `rpt-d9d6019400b2` evidence ranking | W = 30 min, K = 10: precision 0.406, recall 0.601 (in-sample, the weights were tuned on dev, D55); without the graph recall 0.525 and red herrings reach the top-10 in half the red-herring cases; recency ranking recall 0.014; the true primary resource is the first candidate cause in 61% of cases, in the top 3 in 92%. |
| `rpt-6dc7127fdb57` retrieval | same-label retrieval label@1 0.963 with evidence in the query, 0.598 description only; zero test incidents in any index. An upper bound: queries and corpus share the simulator's templates and the embedder is lexical (D71). |
| `rpt-136edb46e231` pipeline | every dev incident gives a valid diagnosis under Full and A1-A4 (86/86, 0 rejected, unsupported-citation rate 0). |
| `rpt-fa410c0154c9` verifier audit | injected faults detected: wrong values 86/86, wrong resource 84/84, uncited conclusions 84/84, unsupported claims 82/86 (the lexical check misses claims that reuse words of the cited line). |

## What remains to be measured

1. The real-LLM matrix on the test split (RUNBOOK), which is the only way to answer RQ1-RQ5.
2. The same on captured real incidents (`docs/future-work.md`).
3. The LLM-judge rubric and the human spot check for those runs.
