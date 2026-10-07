# Evidence ranking and candidate causes (dev split) — smoke-test / synthetic

> **SMOKE-TEST / SYNTHETIC.** Simulator data with injected faults, not real AWS telemetry. Ground-truth evidence is defined by the simulator (DECISIONS D23), so these numbers measure agreement with the simulator's assumptions, not real-world accuracy.

- Dataset: `synthetic-v1`, generator `1.1.0`, seed 42, content sha256 `06cec245b951be8e9b08524367439b5ee3d37fa75d6551d3bb8e2922d8388f8b`
- Split: `dev` — 86 incidents, 82 with ground-truth evidence (insufficient-evidence cases have none and are excluded from P/R)
- Command: `python -m app.evaluation.evidence_eval --tune`
- Defaults: window 30 min before the alarm + 10 min after, K = 10, weights temporal 0.15, resource 0.0, anomaly 0.3, semantic 0.3, dependency 0.3, anomaly method `zscore` (threshold 6.0), max 2 items per group

Precision@K = ground-truth items in the top-K / items returned. Recall@K = ground-truth items in the top-K / ground-truth items. Window coverage = ground-truth items inside the window (upper bound on recall). Red-herring rate = red-herring cases with a red-herring event in the top-K. Ground truth averages 7.07 items per incident, so precision@K cannot exceed that number / K.

## Ablations (default window and K)

| config | window_min | k | precision | recall | window_coverage | red_herring_rate |
|---|---|---|---|---|---|---|
| Full | 30 | 10 | 0.406 | 0.601 | 1.0 | 0.0 |
| A2 no dependency graph | 30 | 10 | 0.356 | 0.525 | 1.0 | 0.5 |
| A3 no anomaly detection | 30 | 10 | 0.187 | 0.263 | 1.0 | 0.0 |
| A4 no temporal (weight 0) | 30 | 10 | 0.404 | 0.599 | 1.0 | 0.0 |
| A5 recency (no ranking) | 30 | 10 | 0.009 | 0.014 | 1.0 | 0.0 |
| minus resource | 30 | 10 | 0.406 | 0.601 | 1.0 | 0.0 |
| minus semantic | 30 | 10 | 0.36 | 0.529 | 1.0 | 0.0 |
| only temporal | 30 | 10 | 0.093 | 0.134 | 1.0 | 0.0 |
| only resource | 30 | 10 | 0.0 | 0.0 | 1.0 | 0.0 |
| only anomaly | 30 | 10 | 0.327 | 0.488 | 1.0 | 0.375 |
| only semantic | 30 | 10 | 0.18 | 0.267 | 1.0 | 0.0 |
| only dependency | 30 | 10 | 0.0 | 0.0 | 1.0 | 0.0 |

A2 also removes the graph (dependency component 0); A3 removes detector output, so the onset falls back to the alarm time and metric points get no anomaly score; A4 sets the temporal weight to 0 (chains do not feed the score, see DECISIONS D52).

## By category (Full, default window and K)

| category | window_min | k | precision | recall | window_coverage | red_herring_rate |
|---|---|---|---|---|---|---|
| standard | 30 | 10 | 0.396 | 0.609 | 1.0 | n/a |
| red_herring | 30 | 10 | 0.4 | 0.617 | 1.0 | 0.0 |
| compound | 30 | 10 | 0.6 | 0.422 | 1.0 | n/a |

## Window x K (Full)

| config | window_min | k | precision | recall | window_coverage | red_herring_rate |
|---|---|---|---|---|---|---|
| Full | 5 | 5 | 0.346 | 0.252 | 0.727 | 0.0 |
| Full | 5 | 10 | 0.321 | 0.473 | 0.727 | 0.0 |
| Full | 5 | 20 | 0.194 | 0.569 | 0.727 | 0.0 |
| Full | 5 | 40 | 0.114 | 0.593 | 0.727 | 0.125 |
| Full | 15 | 5 | 0.395 | 0.291 | 0.997 | 0.0 |
| Full | 15 | 10 | 0.407 | 0.603 | 0.997 | 0.0 |
| Full | 15 | 20 | 0.272 | 0.78 | 0.997 | 0.0 |
| Full | 15 | 40 | 0.15 | 0.815 | 0.997 | 0.25 |
| Full | 30 | 5 | 0.385 | 0.282 | 1.0 | 0.0 |
| Full | 30 | 10 | 0.406 | 0.601 | 1.0 | 0.0 |
| Full | 30 | 20 | 0.273 | 0.782 | 1.0 | 0.0 |
| Full | 30 | 40 | 0.145 | 0.818 | 1.0 | 0.25 |
| Full | 60 | 5 | 0.378 | 0.276 | 1.0 | 0.0 |
| Full | 60 | 10 | 0.406 | 0.601 | 1.0 | 0.0 |
| Full | 60 | 20 | 0.27 | 0.771 | 1.0 | 0.0 |
| Full | 60 | 40 | 0.143 | 0.818 | 1.0 | 0.0 |

## Candidate causes and event chains

hit@n: the true primary resource is the resource of one of the first n candidate causes. top_cause_is_gt_evidence: the first candidate cause is built on a ground-truth evidence event. Red-herring columns count red-herring events among candidate causes and in the reported chains (red-herring cases only).

| window_min | hit@1 | hit@3 | hit@5 | top_cause_is_gt_evidence | mean_candidates | mean_chain_signals | red_herring_cases | red_herring_candidate_causes | red_herring_chain_events |
|---|---|---|---|---|---|---|---|---|---|
| 5 | 0.488 | 0.744 | 0.793 | 0.585 | 3.83 | 3.81 | 8 | 0 | 0 |
| 15 | 0.61 | 0.915 | 0.963 | 0.598 | 4.67 | 4.71 | 8 | 0 | 0 |
| 30 | 0.61 | 0.915 | 0.963 | 0.598 | 4.68 | 4.71 | 8 | 0 | 0 |
| 60 | 0.61 | 0.915 | 0.963 | 0.598 | 4.68 | 4.71 | 8 | 0 | 0 |

## Weight grid (dev split only; top 20 by recall, then precision)

| temporal | resource | anomaly | semantic | dependency | precision | recall | red_herring_rate |
|---|---|---|---|---|---|---|---|
| 0.0 | 0.0 | 0.15 | 0.15 | 0.3 | 0.406 | 0.605 | 0.0 |
| 0.15 | 0.0 | 0.3 | 0.3 | 0.3 | 0.406 | 0.601 | 0.0 |
| 0.0 | 0.0 | 0.15 | 0.15 | 0.15 | 0.404 | 0.599 | 0.0 |
| 0.0 | 0.0 | 0.15 | 0.3 | 0.3 | 0.404 | 0.599 | 0.0 |
| 0.0 | 0.0 | 0.3 | 0.3 | 0.3 | 0.404 | 0.599 | 0.0 |
| 0.0 | 0.0 | 0.3 | 0.3 | 0.15 | 0.402 | 0.597 | 0.0 |
| 0.0 | 0.0 | 0.15 | 0.15 | 0.0 | 0.401 | 0.594 | 0.25 |
| 0.0 | 0.0 | 0.15 | 0.3 | 0.0 | 0.401 | 0.594 | 0.0 |
| 0.0 | 0.0 | 0.15 | 0.3 | 0.15 | 0.401 | 0.594 | 0.0 |
| 0.0 | 0.0 | 0.3 | 0.3 | 0.0 | 0.401 | 0.594 | 0.25 |
| 0.15 | 0.0 | 0.15 | 0.15 | 0.3 | 0.4 | 0.59 | 0.0 |
| 0.15 | 0.0 | 0.3 | 0.3 | 0.15 | 0.398 | 0.585 | 0.0 |
| 0.0 | 0.15 | 0.3 | 0.3 | 0.15 | 0.394 | 0.583 | 0.0 |
| 0.0 | 0.15 | 0.3 | 0.3 | 0.3 | 0.394 | 0.583 | 0.0 |
| 0.0 | 0.15 | 0.3 | 0.3 | 0.0 | 0.393 | 0.581 | 0.25 |
| 0.15 | 0.0 | 0.3 | 0.15 | 0.3 | 0.385 | 0.569 | 0.0 |
| 0.0 | 0.0 | 0.3 | 0.15 | 0.3 | 0.384 | 0.569 | 0.0 |
| 0.15 | 0.0 | 0.15 | 0.3 | 0.3 | 0.384 | 0.567 | 0.0 |
| 0.0 | 0.0 | 0.3 | 0.15 | 0.15 | 0.383 | 0.567 | 0.0 |
| 0.0 | 0.15 | 0.3 | 0.15 | 0.15 | 0.382 | 0.566 | 0.0 |

## Caveats

- In-sample: the default weights, detector threshold and other evidence settings were chosen on this dev split (DECISIONS D55), so the Full row is optimistic. An unbiased estimate comes only from the test split in the Phase 7 experiment runner.
- Ground-truth evidence and the ranking's signals come from one author's assumptions about how faults look (circularity). Real captures are needed to confirm any ranking.
- Ground truth lists only the first occurrences of each key signal, so other correct but unlisted items (later datapoints, secondary metrics) count as false positives.
- Small sample: differences of a few points between configurations are within noise; Phase 7 adds bootstrap confidence intervals.
