# Progress

## Phase 0: Foundations and contracts — implemented, gate PASSED

Built: repo skeleton; `docker-compose.yml` (db = pgvector/pgvector:pg16, api, frontend placeholder);
config (YAML + env); secret-scrubbing logging; contracts (`CanonicalEvent`, taxonomy, `GroundTruth`,
`Diagnosis`, evidence/score weights, anomaly); interfaces (`Collector`, `Detector`, `GraphStore`,
`VectorStore`, `LLMClient`, `Embedder`); 10 DB models + Alembic migration `0001`; `/health` endpoint.

Gate status:
- [x] `pytest` passes (contracts, diagnosis accept/reject, config, logging, interfaces, migration vs models, migration up/down, `/health`).
- [x] `ruff check` clean.
- [x] Schemas validate with unit tests, including diagnosis accept/reject cases.
- [x] `docker compose up --build` brings up api + db (verified 2026-10-06, Windows 11, Docker Desktop 4.93.0 / Engine 29.8.1, Compose v5.5.1; it had been waived under D17 until then):
  - `GET /health` -> 200 `{"status":"ok","database":"ok"}`; stopping `db` -> 503, restarting -> 200.
  - `alembic upgrade head` ran on PostgreSQL 16: 10 tables + `alembic_version` = `0001`; extension `vector` 0.8.7 installed.
  - Postgres schema vs SQLAlchemy models: `compare_metadata` diff is empty.
  - Frontend placeholder served on http://localhost:3000.
  - Inside the api container (Python 3.11.17, Linux): 79 tests pass; the 2 IaC-template tests error only because the image does not include `infra/` (by design).

## Phase 1: Scenario generator, offline dataset, replay collector — implemented, gate PASSED

Branch: `phase1/scenario-generator` (merged to `main`). Details: `docs/offline-dataset.md`, `infra/README.md`.

Built:
- `app/offline/topology.py`: 4 reference topologies (ecs, lambda, ec2, sqs) with resources, dependency edges, CloudWatch metric specs and log groups.
- `app/offline/faults.py`: signatures for all 10 fault types (metric effects with lag/ramp/oscillation, logs, CloudTrail, Config, alarm, ground-truth text, resolution).
- `app/offline/simulator.py`: seeded deterministic simulator (AR(1) noise, daily cycle, 60 s / 300 s resolution, Sum vs Average scaling, missing points, collection gaps, background logs/CloudTrail/Config noise, data-driven alarm time).
- `app/offline/dataset.py`: plan of 124 cases, stratified dev/test split, two files per incident (observable vs truth), manifest with per-file sha256, `DatasetLoader` with checksum verification.
- `app/offline/replay.py`: `ReplayCollector` (implements `Collector`), plus `inventory()` and `default_request()`.
- `app/offline/generate.py`: CLI `python -m app.offline.generate`.
- `infra/test-stack/template.yaml` (CloudFormation: ALB + ECS Fargate + RDS + SQS/Lambda consumer) and `app/offline/fault_injection.py` (inject/revert all 10 faults, dry run by default). **Written, never run against AWS.**

Gate status:
- [x] >=100 cases load through `ReplayCollector` into CanonicalEvents (124 cases).
- [x] Regeneration with the same seed is byte-identical (test compares every file).
- [x] Ground truth validates against the taxonomy; every evidence id exists in the events; primary resource is in the inventory.
- [x] >=10 red-herring (12), >=5 insufficient-evidence (6), >=5 compound (6) cases.
- [x] Blinded descriptions (forbidden-word test), ground truth absent from observable files, opaque incident ids.
- [x] `pytest` 81 passed; `ruff check` and `ruff format --check` clean.

Known limits (all data is smoke-test / synthetic):
- The simulator, ground truth and later rules share one author's assumptions about how faults look (circularity risk; see BRIEF analysis). Real captures via the test stack are the mitigation.
- No scenario uses the `other` label.
- Byte-identity is verified across OSes: seed 42 gives the same `content_sha256` on Windows/Python 3.12 and Linux/Python 3.11 (D22).
- Fault-injection tool and CloudFormation template are untested against AWS.

## Phase 2: Real AWS collectors — implemented, gate PASSED

Branch: `phase2/aws-collectors` (merged to `main`). Details: `docs/aws-setup.md`.

Built (`backend/app/collectors/`):
- `cloudwatch_metrics.py`: batched `GetMetricData` (500 queries/call, paginated), period chosen from data age, per-target-group ALB metrics summed; metric catalog uses the same names/namespaces/stats/units as the simulator.
- `cloudwatch_logs.py`: `FilterLogEvents` on the resources' own log groups, filter pattern, per-group cap, severity inference, secret scrubbing and truncation.
- `cloudtrail.py`: `LookupEvents` with a 2 req/s token bucket, one lookup per resource name, EventId de-duplication, read-only events skipped by default, ingestion-lag warning, sanitized request parameters.
- `config_history.py`: `GetResourceConfigHistory`, optional; skipped with a warning when there is no recorder or the resource is not recorded.
- `inventory.py`: discovery from seed ARNs (ALB, target groups, ECS, Lambda, EC2, RDS, SQS, SGs, IAM roles) producing the same resource/relationship records as the dataset.
- `aws_collector.py`: composite `AwsCollector` (implements `Collector`), on-disk event cache, `required_iam_actions()`.
- `capture.py`: CLI to capture a real incident into the offline observable format.
- `aws_common.py`: clients with adaptive retries, throttling backoff with jitter, rate limiter, pagination, `sanitize`.
- Contract: per-source metadata schema, canonical resource-id format and `contract_violations()` added to `app/contracts/events.py` (D31).
- `infra/iam/collector-readonly-policy.json` (20 read-only actions) and `docs/aws-setup.md`.

Gate status:
- [x] moto-based tests pass (inventory, metrics, logs, Config not-discovered path); botocore Stubber covers CloudTrail `LookupEvents` (not implemented in moto) and Config history items / no-recorder.
- [x] Contract test: real collectors and `ReplayCollector` both emit `CanonicalEvent`s with zero `contract_violations` for metrics, logs, CloudTrail and Config; simulator metric catalog equals the real catalog (namespace, name, stat, unit).
- [x] No credentials in source (repository scan test); IAM policy covers every called action and contains no write actions.
- [x] `pytest` 119 passed; `ruff check` and `ruff format --check` clean.

Known limits:
- Not run against a real AWS account yet (needs the user's sandbox account; see `docs/aws-setup.md`).
- `connects_to` edges are inferred from security-group rules; SQS producers are not discovered.
- ECS service ids can collide across clusters (D36).
- Simulator generator bumped to 1.1.0 for stat parity; dataset content hash changed (D35).

## Phase 3: Anomaly detection — implemented, gate PASSED

Branch: `phase3/anomaly-detection` (merged to `main`, PR #4). Results: `docs/experiments/phase3-anomaly-detectors-dev.md` (+ `.csv`, `.json`).

Built:
- `app/anomaly/detectors.py`: z-score, modified z-score (MAD), moving average (relative deviation), rolling std (volatility ratio), Isolation Forest. The first four share a trailing-window engine: baseline window in minutes (works at 60 s and 300 s), no look-ahead, optional exclusion of already-flagged points from the baseline, spread floor for flat series, points with too little history are skipped. Isolation Forest is fit on the first `train_minutes` of each series and scores the rest (deterministic `random_state`).
- `app/anomaly/series.py`: CanonicalEvents -> MetricSeries (grouped, sorted, duplicate timestamps averaged, NaN/inf dropped, period from metadata or inferred robustly to gaps).
- `app/anomaly/service.py`: `AnomalyService` runs the configured methods over an incident's events.
- `anomaly` config section (`config/default.yaml`): methods, baseline window, min history, floors and per-method thresholds.
- `app/evaluation/anomaly_eval.py`: CLI comparing methods against the simulator's injected-fault windows (point precision, window recall, F1, detection delay, series false-alarm rate; by resolution; optional threshold sweep). Refuses the test split without `--final`.
- Every anomaly carries score, baseline, observed value, method, timestamp, metric and resource (existing `Anomaly` contract, unchanged). No LLM involvement (a test checks no LLM module is imported).

Gate status:
- [x] Comparison table of detectors on the dev split, labelled **smoke-test / synthetic** (86 incidents, 443 injected windows), with by-resolution and threshold-sensitivity tables.
- [x] Unit tests for each method (step detection, robustness to baseline outliers, relative jumps, oscillation, determinism, both resolutions, sparse/tiny series, flat-series floor, gaps shrinking the window), plus series building, config wiring, service and evaluation metrics.
- [x] `pytest` 151 passed; `ruff check` and `ruff format --check` clean.

Results summary (dev split, default thresholds, smoke-test / synthetic):

| method (threshold) | precision | recall | F1 | median delay (min) | series false-alarm rate |
|---|---|---|---|---|---|
| zscore (3.0) | 0.680 | 0.856 | 0.758 | 1.0 | 0.574 |
| mad (3.5) | 0.474 | 0.910 | 0.623 | 1.7 | 0.572 |
| moving_average (0.5) | 0.410 | 0.896 | 0.562 | 1.7 | 0.373 |
| rolling_std (3.0) | 0.789 | 0.824 | 0.806 | 2.0 | 0.053 |
| isolation_forest (0.0) | 0.274 | 0.910 | 0.421 | 1.0 | 0.845 |

Observations: textbook thresholds produce many false alarms on autocorrelated noise; the dev sweep shows z-score at 4.0-6.0 reaches F1 0.86-0.89 with a series false-alarm rate of 0.23-0.05 (D46). 300 s data roughly halves z-score/MAD precision and adds about 3 minutes of delay versus 60 s.

Known limits:
- Labels and detectors share the simulator's assumptions (circularity); rankings need confirmation on real captures.
- Labels cover the full post-onset window, so ramp-up points that still look normal count against recall and delay.
- Detectors are univariate; there is no cross-metric or cross-resource fusion yet (that is correlation, Phase 4).
- Anomalies are not yet persisted to the `anomalies` table (API/DB wiring is Phase 8).

## Phase 4: Correlation, dependency graph, evidence ranking — implemented, gate PASSED

Branch: `phase4/correlation-evidence` (merged to `main` after Phase 3, PR #5; D48, D58). Verified on `main` at `e837887`: 176 tests pass, ruff clean. Results: `docs/experiments/phase4-evidence-ranking-dev.md` (+ `.csv`, `.json`).

Built:
- `app/graph/`: `NetworkXGraphStore` (implements `GraphStore`) with typed nodes (load_balancer, ecs_service, lambda_function, ec2_instance, rds_instance, sqs_queue, security_group, iam_role, ...) and typed edges (routes_to, connects_to, secured_by, assumes_role, sends_to, polls); `upstream`/`downstream`/`path` on structural edges, `blast_radius`/`impact_path`/`impact_sources` on the derived impact graph (D49); `build_graph()` from `ResourceRecord`/`RelationshipRecord`, which both real inventory discovery and the offline dataset produce.
- `app/correlation/`: investigation windows (5/15/30/60 min before the alarm + 10 min after), event classification and redundancy groups, onset estimation and temporal proximity ranking (D54), signals -> time-ordered links over the impact graph -> event chains -> **candidate causes** (temporal precedence + dependency path; labelled "candidate cause", never "causal") (D52).
- `app/evidence/`: `EvidenceRanker` implementing the Section 2 score (five components in [0,1], configurable weights and K, event-type priors for events without a numeric anomaly, per-group cap keeping first occurrences, D51), local lexical semantic scorer (D53), stable evidence ids `evd_<sha256(incident|event)>` (D56), recency baseline (A5), and `InvestigationPipeline` (anomalies -> onset -> window -> chains -> ranking) with ablation switches `use_graph` / `use_anomalies` / `use_chains`.
- Contracts: `app/contracts/correlation.py` (`Signal`, `InvestigationWindow`, `EventChain`, `CandidateCause`, `CorrelationResult`), `EvidenceRanking` and `make_evidence_id` (D56). Existing contracts unchanged.
- Config: `evidence` section extended and new `correlation` section in `config/default.yaml`; defaults chosen on dev only (D55).
- `app/evaluation/evidence_eval.py`: CLI for evidence precision/recall@K, window x K grid, ablations, by-category table, candidate-cause hit@n and red-herring counts, optional dev-only weight grid (`--tune`). Refuses the test split without `--final`; `--tune` is dev-only.

Gate status:
- [x] Evidence precision/recall@K against ground-truth evidence on dev, labelled **smoke-test / synthetic** (86 incidents, 82 with ground-truth evidence), for K in {5, 10, 20, 40} and windows {5, 15, 30, 60}, plus ablations.
- [x] Chain-detection tests including red-herring cases: unit tests (unrelated deployment at the onset, change after the alarm, change beyond the link gap, multi-hop SG -> RDS -> ECS -> ALB chain, SQS `polls` chain, no-graph mode, determinism) and a dataset test over every dev red-herring case at all four windows (no red-herring event is ever a candidate cause or in a chain).
- [x] `pytest` 176 passed (25 new); `ruff check` and `ruff format --check` clean.

Results summary (dev split, smoke-test / synthetic, **in-sample**: the settings were tuned on this split):

| configuration (W = 30 min, K = 10) | precision@10 | recall@10 | red-herring rate |
|---|---|---|---|
| Full | 0.406 | 0.601 | 0.0 |
| A2 no dependency graph | 0.356 | 0.525 | 0.5 |
| A3 no anomaly detection | 0.187 | 0.263 | 0.0 |
| A4 temporal weight 0 | 0.404 | 0.599 | 0.0 |
| A5 recency (no ranking) | 0.009 | 0.014 | 0.0 |

- Recall@K for Full at W = 30: 0.28 (K=5), 0.60 (10), 0.78 (20), 0.82 (40). Ground truth averages 7.1 items per incident, so precision@10 cannot exceed about 0.71.
- W = 5 misses 27% of ground-truth evidence (window coverage 0.73); 15, 30 and 60 min perform the same.
- Candidate causes (W >= 15): the true primary resource is the first candidate in 61% of cases and in the top 3 in 92%; zero red-herring candidate causes or chain events across the 8 dev red-herring cases.
- Anomaly detection is the strongest single signal; the graph mainly removes red herrings and unrelated resources (A2 lets red herrings into the top-10 in half the red-herring cases); temporal adds almost nothing once the anomaly component is centred on the onset; the alarmed-resource `resource` component hurts on this dataset and has weight 0 (D55).

Known limits:
- All numbers are synthetic and in-sample (D55); the test split has not been touched.
- Every red herring in the dataset sits on a resource with no dependency path. A red herring on a connected resource (e.g. an unrelated change on the same RDS instance) can only be demoted by timing, not excluded; no dataset case covers it.
- Ground truth lists only first occurrences of key signals, so correct but unlisted items count as false positives (precision is a lower bound).
- The semantic component is lexical (TF-IDF + failure terms) until Phase 5 embedders exist.
- Onset estimates on 300 s data are quantised to 5 minutes (dev mean absolute error 3.3 min vs 6.4 min for the alarm time).
- Evidence, chains and the graph are not yet persisted to the `evidence` / `resource_relationships` tables (API/DB wiring is Phase 8).

## Phase 5: Redaction, embeddings, historical RAG — implemented, gate PASSED

Branch: `phase5/redaction-rag` (from `main`). Results: `docs/experiments/phase5-retrieval-dev.md` (+ `.csv`, `.json`).

Built:
- `app/ai/redaction.py`: `Redactor` with consistent per-session pseudonyms (`ACCOUNT_1`, `IP_2`, `PRINCIPAL_1`, `HOST_1`, `SECRET_3` ...) for account ids, ARNs (partition/service/region kept), IPv4/IPv6, principals (IAM unique ids, access key ids, e-mails, identity keys in structured data), hostnames and secrets (key=value, bearer tokens, JWTs, PEM keys, URL passwords); optional resource-name redaction; `restore` maps answers back (never secrets); strict `check`/`ensure_safe`. `RedactingLLMClient` wraps every external LLM client (D59).
- Interfaces: `is_external` flag on `LLMClient` and `Embedder`; `VectorStore.search` takes `exclude_ids` for leave-one-out (D60, D61).
- `app/rag/embedders.py`: `HashingEmbedder` (LOCAL-ONLY, deterministic, no download), `SentenceTransformerEmbedder` (config default, optional `embeddings` extra), `OpenAIEmbedder` (OpenAI-compatible) and `GeminiEmbedder` over stdlib HTTP; `build_embedder` wraps external ones in `RedactingEmbedder` (D60).
- `app/rag/vector_store.py`: `LocalVectorStore` (FAISS `IndexFlatIP`, NumPy exact search when faiss is absent, save/load) and `PgVectorStore` (pgvector `<=>`; NumPy on SQLite); model name and dimension stored per index and enforced (D61). DB: `vector_indexes` / `vector_items` tables, `VectorType` (pgvector on PostgreSQL, JSON elsewhere), migration `0002`.
- `app/rag/corpus.py`: separate historical corpus `historical-v1` (simulator seed 7, keys `hist:*`, 40 past incidents, 4 per fault type) with manifest; dev-only records for leave-one-out; held-out (test) fingerprints (D62). CLI `python -m app.rag.corpus`.
- `app/rag/knowledge_base.py`: `KnowledgeBase` with a leakage guard (refuses any record carrying a test incident id, event id or description hash, before indexing), `HistoricalRetriever` (nearest neighbours + re-ranking by similarity, signal-vocabulary overlap and alarm/resource-type context), `build_query` from the description + Phase 4 evidence (D63).
- `app/rag/guidance.py`: versioned historical prompt section ("context only, NOT evidence", rules for `historical_influence`) and `validate_historical_influence` (no historical id cited as evidence, declared ids must have been retrieved, `how` required, verbatim copies of a past root cause flagged) (D64).
- Contracts: `app/contracts/historical.py` (`HistoricalRecord`, `RetrievedIncident` labelled `HISTORICAL`).
- `app/evaluation/retrieval_eval.py`: dev retrieval report (corpus vs leave-one-out, with/without evidence in the query, cosine vs re-ranked) plus a leakage section.
- Config: `redaction` and `embeddings` extended, new `vector_store` and `rag` sections; `rag` extra (`faiss-cpu`) installed in CI.
- Tests: the "no LLM module imported" checks of Phases 3 and 4 now run in a fresh interpreter (`tests/isolation.py`), so they no longer depend on test order (D65).

Hardening before merging (D67-D71):
- Construction guard: external `LLMClient`/`Embedder` classes cannot be instantiated outside `app/ai/clients.py` (`create_llm_client`, `create_embedder`), which always returns them wrapped in redaction (`GuardedMeta`, D67).
- Real-AWS mode: `data_mode: aws` makes the redaction policy mandatory; external clients and the capture CLI fail closed if redaction is disabled, not strict, or a built-in category is off (D68).
- Redaction: configurable `custom_patterns` (validated at config load, optional `value` group, `secret` flag), bare 40-character secret keys, URL passwords with an empty user, JDBC parameters kept after the password; fixed a bug where an e-mail match swallowed a `SECRET_n` token in connection strings (D69).
- CI: backend job runs with and without `faiss-cpu`; a `pgvector` job runs PostgreSQL 16 with pgvector, migrates, and fails if the live test is skipped (D70).
- New tests (`tests/test_redaction_hardening.py`): metric values, timestamps, event ids, resource ids and metric metadata of every dataset event unchanged by redaction; 20 non-identifier strings untouched; JWTs, bearer/basic tokens, six connection-string forms, four private-key block types and bare 40-character keys redacted and never restored; custom patterns; fail-closed checks for every weakened setting; construction guard over every `app` client class plus a source scan for the factory key.
- The credential scan now skips `.pytest_cache`, and parametrized secret test cases have short ids, so secret-looking strings never land in pytest's node-id cache.

Gate status:
- [x] Retrieval tests: same-fault retrieval on dev queries, deterministic results, re-rank score composition, leave-one-out never returns the query, every vector store backend returns identical rankings and rejects model/dimension mismatches, one index never mixes embedding models.
- [x] Leakage test: no test incident id, event id or description hash in any index (corpus or dev leave-one-out); a record built from a test incident is refused even under a different id; `records_from_dataset("test")` is refused.
- [x] Redaction tests: every category, consistent pseudonyms, restore, switches, structured data, strict refusal. **Gate**: with redaction on, a recording external LLM and recording OpenAI/Gemini embedding transports receive no raw identifier (16 identifier kinds plus a real dataset incident), and every dataset incident (dev and test) serialised for a prompt passes the strict check.
- [x] `pytest` 261 passed, 1 skipped (the live pgvector test, which needs `CLOUDDIAG_TEST_PG_URL`; CI runs it in the `pgvector` job); `ruff check` and `ruff format --check` clean.
- [x] Fresh venv with `pip install -e ".[dev,aws]"` only (no faiss-cpu, no sentence-transformers; Python 3.12, 2026-10-07): 261 passed, 1 skipped (the same live pgvector test); the vector store used the NumPy fallback.
- [x] pgvector verified on PostgreSQL 16 in the compose `db` container (2026-10-07): `alembic upgrade head` -> `0002`, `embedding` column type `vector`, model/schema diff empty, the live test passes (`<=>` ordering and leave-one-out), downgrade to `0001` and upgrade again work.

Results summary (dev split, 82 queries, hashing embedder, smoke-test / synthetic):

| knowledge base | query | ranking | label@1 | label@3 | MRR@10 |
|---|---|---|---|---|---|
| historical corpus | description + evidence | re-ranked | 0.963 | 0.988 | 0.977 |
| historical corpus | description only | re-ranked | 0.598 | 0.634 | 0.677 |
| leave-one-out dev | description + evidence | re-ranked | 0.939 | 0.963 | 0.959 |

- Leakage: 0 test incident ids in either index, 0 fingerprint overlap (38 test incidents, 32,944 fingerprints), 0 queries returned themselves.
- Ranked evidence in the query is what makes retrieval work (label@1 0.60 -> 0.96); re-ranking changes results by at most 0.02 on this data, so its default weights were kept, not tuned.
- **The 0.96 is an upper bound** (D71): queries and past incidents share the simulator's fault templates, so this measures template matching. The hashing embedder is lexical (shared words and word pairs), not semantic.

Known limits:
- The near-ceiling label@1 is mostly template overlap: past incidents and queries share the simulator's wording. Real incident history will be much harder.
- Results use the LOCAL-ONLY hashing embedder. SentenceTransformers is the configured default but was not installed here (it pulls PyTorch); re-run `python -m app.evaluation.retrieval_eval --embedder sentence_transformers` after `pip install -e ".[embeddings]"`.
- Redaction is pattern-based: identifiers in formats it does not know (e.g. a bare user name in free text outside identity keys) pass through; strict mode verifies only what it can recognise. Resource names stay readable by default (D59).
- Retrieval quality is measured by taxonomy-label agreement only; whether RAG improves diagnoses is measured in Phase 7 (Full vs A1), which must also run the knowledge-base conditions in D71: matching fault type removed, and distractor entries.
- The evidence ranker still uses the lexical semantic scorer; swapping in an embedding scorer waits for a real embedding model and would need a D55 re-check on dev.

## Next: Phase 6 (diagnosis engine and severity)
LLM clients (OpenAI-compatible, Gemini) created only through `create_llm_client` (direct construction raises; the stub LLM must be added to the local allowlist in `tests/test_redaction_hardening.py` on purpose); versioned prompt builder (using `render_historical_section`); context builder with token budget; output validator + one repair attempt (including `validate_historical_influence`); citation verifier; self-consistency sampling; deterministic stub LLM (LOCAL-ONLY); deterministic severity engine. Gate: end-to-end on dev with the stub; validator rejects malformed/uncited outputs; severity tests.

Carry-over for Phase 7 (D71): besides B1-B4, Full and A1-A5, evaluate RAG with (a) the query's fault type removed from the knowledge base and (b) distractor entries that share alarm metric, topology and wording but differ in root cause; report accuracy, `historical_influence` declarations and `validate_historical_influence` failures for Full vs A1 under each.

## Team hand-off
Setup, commands and working rules are in `README.md` and `CONTRIBUTING.md`. CI runs ruff (lint + format check) and pytest on every PR. Branch naming: `phaseN/<topic>`.

## How to run tests
```
cd backend
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
```

## Repository
https://github.com/navya906/incident-diagnosis-aws (default branch `main`). `BRIEF.md` holds the build brief plus a status/amendments section.
