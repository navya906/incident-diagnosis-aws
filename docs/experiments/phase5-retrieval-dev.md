# Historical retrieval (dev split) — smoke-test / synthetic

> **SMOKE-TEST / SYNTHETIC.** Queries, past incidents and labels all come from the simulator. These numbers show whether retrieval finds past incidents of the same fault type under the simulator's assumptions, not real-world usefulness.

- Dataset: `synthetic-v1`, content sha256 `06cec245b951be8e9b08524367439b5ee3d37fa75d6551d3bb8e2922d8388f8b`
- Knowledge base: `historical-v1` (seed 7, 40 past incidents, separate from the dataset) and a leave-one-out knowledge base of the other dev incidents
- Embedder: `hashing-v1-384` (dimension 384)
- Re-ranking weights: similarity 0.7, signals 0.2, context 0.1; 20 candidates
- Command: `python -m app.evaluation.retrieval_eval --embedder hashing`

label@1: the top past incident has the query's primary taxonomy label. label@3: one of the top 3 does. MRR@10: mean reciprocal rank of the first same-label incident. Majority-label rate in the corpus (what always guessing one label would score): 0.1.

## Results

| knowledge_base | query | ranking | label@1 | label@3 | mrr@10 | mean_top1_similarity | query_returned_itself | queries |
|---|---|---|---|---|---|---|---|---|
| corpus | description + evidence | cosine | 0.963 | 0.988 | 0.978 | 0.606 | 0 | 82 |
| corpus | description + evidence | re-ranked | 0.963 | 0.988 | 0.977 | 0.605 | 0 | 82 |
| corpus | description only | cosine | 0.585 | 0.622 | 0.665 | 0.679 | 0 | 82 |
| corpus | description only | re-ranked | 0.598 | 0.634 | 0.677 | 0.677 | 0 | 82 |
| leave-one-out dev | description + evidence | cosine | 0.951 | 0.976 | 0.968 | 0.615 | 0 | 82 |
| leave-one-out dev | description + evidence | re-ranked | 0.939 | 0.963 | 0.959 | 0.612 | 0 | 82 |
| leave-one-out dev | description only | cosine | 0.585 | 0.671 | 0.65 | 0.689 | 0 | 82 |
| leave-one-out dev | description only | re-ranked | 0.585 | 0.683 | 0.655 | 0.686 | 0 | 82 |

## Leakage check

- Test incidents: 38 (32944 fingerprints: incident ids, event ids, description hashes)
- Test incident ids in the corpus index: **0**; in the leave-one-out index: **0**
- Fingerprint overlap between indexed records and the test split: **0**
- Leave-one-out: queries that retrieved themselves: see `query_returned_itself` (must be 0).

## Caveats

- Past incidents and queries share the simulator's templates (log lines, API names, root-cause wording per fault type), so same-label retrieval is easier than on real incident history.
- The hashing embedder is a LOCAL-ONLY lexical stand-in; SentenceTransformers or an API embedder should be re-run with `--embedder` set accordingly before drawing conclusions.
- Taxonomy-label agreement is a proxy for usefulness; Phase 7 measures whether retrieval changes diagnosis accuracy (Full vs A1).
