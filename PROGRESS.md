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

## Phase 6: Diagnosis engine and severity — implemented, gate PASSED

Branch: `phase6/diagnosis-engine` (from `main` after the Phase 5 merge; CI on that merge passed all three jobs including the pgvector job). Results: `docs/experiments/phase6-e2e-dev.md` (+ `.json`).

Built:
- `app/ai/llm_clients.py`: `OpenAICompatibleClient` (POST `/chat/completions`, JSON mode, seed) and `GeminiClient` (`generateContent`, `responseMimeType: application/json`), both over stdlib HTTP with retries on 408/409/429/5xx and network errors (`app/ai/http.py`), token usage and a cost estimate from configured prices. Both are external: only `build_llm_client` / `create_llm_client` can create them, always wrapped in redaction (D67), failing closed in aws mode (D68). `StubLLM` is the deterministic LOCAL-ONLY test double (D75); it is on the in-process allowlist.
- `app/diagnosis/context.py`: context builder with the ordered sections INCIDENT, TIMELINE (candidate causes and chains), ANOMALIES, LOGS, CLOUDTRAIL, GRAPH, HISTORICAL; every observed fact is one line tagged with its stable evidence id; token budget with per-section shares, carry-over and dropped-item counts (D72).
- `app/diagnosis/prompts.py`: versioned prompt `diag-v1` (system rules, output spec, repair template) with a template hash recorded per diagnosis (D73).
- `app/diagnosis/validator.py`: JSON parsing (fences tolerated), schema validation, citation verifier (ids must exist in the context; numbers quoted in FACT explanations must match the cited line), root-cause resource check, historical-influence rules; `requires_human_review` applied as policy (D74).
- `app/diagnosis/engine.py`: `DiagnosisEngine` (investigation -> retrieval -> context -> prompt -> one repair -> reject with recorded failure), self-consistency sampling with agreement-with-primary as the self-consistency confidence, one redactor per incident across all calls, ablation switches (`use_rag`, `use_graph`, `use_anomalies`, `use_chains`), latency/tokens/cost per diagnosis (D77).
- `app/severity/engine.py`: deterministic severity from affected resources/services, peak error rate, duration, availability (capacity loss), latency, and static business criticality from config; the LLM's `severity_suggestion` is stored as advisory only (D76).
- `app/evaluation/diagnosis_e2e.py`: end-to-end smoke run over every dev incident for Full and A1-A4 with the stub.
- Config: `llm` (temperature, max tokens, seed, timeout, retries, prices), `diagnosis` (self-consistency, context budget, section shares, quote tolerance), new `severity` section.

Gate status:
- [x] End-to-end pipeline on dev incidents with the stub LLM: all 86 dev incidents produce a valid diagnosis under Full, A1, A2, A3 and A4 (0 rejected, unsupported-citation rate 0), with severity, self-consistency (Full) and context statistics; labelled **smoke-test / synthetic**.
- [x] Validator rejects malformed and uncited outputs: non-JSON, arrays, truncated JSON, empty and `null`; schema violations (unknown label, missing field, confidences > 1); no supporting evidence; unknown or historical evidence ids; misquoted values in FACT explanations; unknown root-cause resource; claimed historical use without historical context. One repair attempt, then rejection with the failure recorded.
- [x] Severity tests: points per factor, each factor raises the score, major-outage floor, criticality from config (and invalid regexes rejected at load), monotonicity, determinism, missing data reported as unknown, every dev incident assessed.
- [x] `pytest` 296 passed, 1 skipped; `ruff check` and `ruff format --check` clean.

Results (dev split, stub LLM, smoke-test / synthetic; pipeline checks, not diagnostic quality):

| condition | valid | repaired | rejected | unsupported citations | mean context tokens |
|---|---|---|---|---|---|
| Full | 86/86 | 0 | 0 | 0.0 | 2406 |
| A1 no RAG | 86/86 | 0 | 0 | 0.0 | 1451 |
| A2 no graph | 86/86 | 0 | 0 | 0.0 | 1925 |
| A3 no anomaly detection | 86/86 | 0 | 0 | 0.0 | 2076 |
| A4 no chains | 86/86 | 0 | 0 | 0.0 | 1645 |

- Severity (engine, Full): CRITICAL 24, HIGH 47, MEDIUM 13, LOW 2; the stub's advisory suggestion matches the engine in 43 of 86.
- The repair path is exercised by tests with scripted stub outputs; the stub itself never needs repair on dev.
- The stub's taxonomy agreement (0.965 Full) is **not a result**: its keyword rules were written against the simulator's own wording (D75).

Known limits:
- No real LLM has been run (no keys in the build environment). `docs/experiments/RUNBOOK.md` with real-LLM commands is a Phase 7 deliverable.
- Token counts in the context budget are estimates (chars / 4); on dev the context uses about 1,500-2,400 of 6,000 tokens, so the budget never binds on synthetic data (tests cover tight budgets).
- Quoted-value checking covers numbers in FACT explanations only (INFERENCE may compute values); times, dates, ids and integers below 10 are not checked.
- Severity thresholds are first-principles defaults, not calibrated on real incidents; the latency factor is an addition to the brief's list (D76).
- Self-consistency counts invalid samples as disagreeing; with the stub, temperature only perturbs keyword scores, so agreement (0.98) says nothing about a real model.
- Diagnoses are not yet persisted to the `diagnoses` table (API/DB wiring is Phase 8).

## Phase 7: Evaluation, baselines, ablations — implemented, gate PASSED

Branch: `phase7/evaluation` (from `main` after the Phase 6 merge). Gate run: `docs/experiments/runs/exp-fbeacbc7cd9b/` (`report.md`, `summary.*`, `comparisons.*`, `results.csv`, `manifest.json`; `results.jsonl` is git-ignored and regenerable). Real-LLM instructions: `docs/experiments/RUNBOOK.md`.

Built:
- `app/experiments/config.py`: YAML experiment configs (`experiments/*.yaml`) validated by `ExperimentConfig` (conditions, runs N, seed, self-consistency samples, RQ6 sweeps, bootstrap, spot-check fraction, settings overrides; secrets refused). The condition matrix: B1-B4, Full, A1-A5 (BRIEF Section 4), the knowledge-base conditions `Full-KB-fault-removed` and `Full-KB-distractors` (D71), `Full-no-redaction` (D87), and RQ6 sweeps of K, window and evidence types (Full with one thing changed): 25 conditions (D83).
- `app/baselines/rules.py`: B1 rule-based baseline (D79). B2 (description only) and B3 (raw telemetry newest-first under the same token budget) are new context modes of the context builder (D78); A5 is a recency ranking mode of the evidence pipeline; KB conditions use label exclusion and per-incident distractor entries.
- `app/evaluation/metrics.py`: per-diagnosis metrics: taxonomy accuracy, resource and root-cause match, top-3, compound secondary label found, cited-evidence precision/recall, context recall/precision, first-answer citation checks (hallucination rate), verbalised and self-consistency confidence, recommendation rubric (deterministic proxy `rubric-v1` + LLM-judge prompt + human spot-check sample), severity, latency, tokens, cost (D81).
- `app/evaluation/stats.py`: bootstrap intervals over incidents, paired bootstrap differences, exact McNemar, ECE and Brier (D80).
- `app/experiments/runner.py` + CLI `python -m app.experiments run|verify|list`: experiment ids from config + dataset + corpus + prompt + model + embedder + source hash; manifest with model and provider-reported versions, prompt version/hash, dataset/corpus versions, config, effective settings (no secrets), timestamps, git commit, library versions, totals and a results hash; CSV/JSON export; optional database storage (`evaluation_runs`, `experiment_results`); `--split dev|test` with `--final` required for test; `verify` re-runs an id and compares the results hash (D82).
- `experiments/`: `smoke-dev.yaml` (gate), `real-dev-pilot.yaml`, `real-test-openai.yaml`, `real-test-gemini.yaml` (cheaper plan: N=3 for baselines and Full, N=1 for ablations and sweeps, self-consistency only for Full and B4; example model versions to replace with current ones).
- `docs/experiments/RUNBOOK.md`: exact commands for real-LLM runs on the test split, cost estimate, reproducibility with non-deterministic models, judge and spot-check procedure, results template.
- Engine extensions: `context_mode` (ranked/raw/description), `ranking_mode` (score/recency), weight overrides, window/K per call, retriever override and label exclusion; attempts record the provider-reported model and first-answer citation counts.

Hardening before committing (D85-D90):
- `Full-no-redaction` condition: Full with redaction off, to measure what redaction costs in quality; offline only, the run fails closed in aws mode (D87).
- Statistics: cluster bootstrap by ground-truth fault type (11 clusters on dev) for summary intervals and paired comparisons; pre-declared primary comparisons in the config (B2, B3, B4, A1, A2, A5 vs Full on root-cause match) Holm-corrected as one family; everything else marked exploratory (D85).
- Stratified tables by case type: clean, red-herring, compound, insufficient-evidence (`strata.*`, report section) (D90).
- Verifier audit (`python -m app.evaluation.verifier_audit`, `docs/experiments/phase7-verifier-audit.md`): faults injected into valid answers on all 86 dev incidents. Detection: fabricated ids 86/86, historical ids 86/86, wrong values 86/86, unknown root-cause resource 84/84, uncited conclusions 84/84, unsupported claims 82/86 (lexical check); 0 false positives on clean answers. It found and fixed three bugs: scientific-notation numbers escaped the quoted-value check; hex ids like `f7e821be` parsed as infinity and matched any quoted value; the stub's insufficient-evidence explanation was unsupported by its cited line (D89).
- Cheaper run plan: `runs_ablations`, `runs_sweeps`, `self_consistency_conditions`; `python -m app.experiments plan` counts calls and estimates cost. Final test run: 2,356 calls (was 16,416), about 15.3M input + 3.1M output tokens, about $69 (OpenAI example prices) or $34 (Gemini) (D86).
- Judge config: the judge must be a different model family from the diagnosing model; the config is rejected otherwise, and model families are read from the model name before the provider (D88).

Gate status:
- [x] Full matrix end-to-end with the stub LLM on dev: 25 conditions x 86 incidents = 2,150 diagnoses, 0 rejected, in under 2 minutes; outputs labelled **smoke-test / synthetic** (`exp-fbeacbc7cd9b`).
- [x] Reproducible by experiment id: `python -m app.experiments verify exp-fbeacbc7cd9b` re-ran all 2,150 diagnoses and matched the stored results hash (`fd16e8c5...`); the id was re-checked after committing (it does not depend on the commit); tests also check that changed inputs change the id and that the test split needs `--final`.
- [x] `docs/experiments/RUNBOOK.md` gives the exact commands for real-LLM runs on the test split (OpenAI-compatible and Gemini), checked by a test.
- [x] `pytest` 324 passed, 1 skipped; `ruff check` and `ruff format --check` clean.

Smoke results (dev, stub LLM, **not results**: the stub and B1 share keyword rules written against the simulator's wording, D75/D79):

| condition | accuracy [95% CI] | root cause | evidence recall | context recall |
|---|---|---|---|---|
| B1 rules | 0.686 [0.593, 0.779] | 0.674 | 0.263 | n/a |
| B2 description only | 0.046 [0.012, 0.093] | 0.046 | 0.000 | 0.000 |
| B3 raw telemetry by recency | 0.186 [0.105, 0.268] | 0.174 | 0.009 | 0.524 |
| B4 ranked evidence | 0.930 [0.872, 0.977] | 0.779 | 0.300 | 0.502 |
| Full | 0.965 [0.919, 1.000] | 0.895 | 0.372 | 0.601 |
| A4 no temporal | 0.779 [0.686, 0.861] | 0.698 | 0.305 | 0.600 |
| RQ6 metrics only | 0.337 [0.244, 0.442] | 0.186 | 0.072 | 0.287 |

What these smoke numbers can and cannot show:
- They show the plumbing works: each condition changes what the model sees (context recall moves as expected; B2 and metrics-only contexts leave the stub with little to cite), and the statistics run: primary comparisons (cluster bootstrap, Holm) B2 vs Full -0.849 (Holm p < 0.001), B3 -0.721 (< 0.001), B4 -0.116 (0.008), A2 -0.116 (0.008), A1 0.000 (1.0), A5 +0.070 (0.376).
- A1, both knowledge-base conditions and Full-no-redaction equal Full exactly: the stub never uses retrieved history to choose a label, and it is local, so redaction never applies to it. These conditions run, but only a real LLM can show whether RAG helps or misleads and what redaction costs.
- By case type (Full): clean 1.000 accuracy (70), red-herring 1.000 (8), compound 0.750 (4), insufficient-evidence 0.500 (4); the small strata make these very uncertain.
- A5 (recency) scores at least as well as Full despite context recall 0.014: the stub still reads the candidate-cause timeline. This is a stub artifact, not a finding about ranking.
- Hallucination rate is 0 everywhere because the stub only cites lines it was shown.

Known limits:
- No real-LLM run yet (no keys in the build environment); the configs pin example model versions that must be replaced with current ones before running.
- Bootstrap streams depend on the NumPy version (recorded in the manifest); intervals can shift slightly across NumPy versions.
- Calibration (ECE) on 86 or 38 incidents per condition is noisy; only the six primary comparisons are Holm-corrected, the exploratory table is not. With 11 fault-type clusters the cluster intervals are coarse.
- The rubric score is a deterministic proxy; the LLM-judge rubric and the human spot check are manual steps for real runs.
- The unsupported-claim check is lexical: false claims that reuse words from the cited line pass (4/86 in the audit); semantic support needs the LLM judge.

## Phase 8: Backend API, lifecycle, security — implemented, gate PASSED

Branch: `phase8/api` (from `main` after the Phase 7 merge, PR #8). API reference: `docs/api.md`.

Built:
- `app/api/routes.py`: 22 REST routes under `/api`: incidents (create, list, detail, lifecycle transitions); telemetry in (events, inventory, offline import); telemetry out (events search, logs, CloudTrail/Config, metric series with anomaly flags, timeline, dependency graph, ranked evidence); async diagnosis (start job, job status, diagnoses, latest diagnosis); lifecycle metrics; audit log; experiment summaries. The original spec is not in the repository, so the endpoint set was derived from the data model and the Phase 9 pages (D91).
- `app/api/alarms.py`: alarm-ingestion webhook on `POST /api/incidents` for SNS notifications (signature verification v1/v2, certificate only from `sns.<region>.amazonaws.com`, optional topic allowlist, subscription messages verified but never auto-confirmed) and EventBridge "CloudWatch Alarm State Change" events. Only transitions into ALARM open incidents; dimensions map to canonical resource ids; repeated alarms deduplicate into the open incident (D95).
- `app/api/lifecycle.py`: DETECTED -> INVESTIGATING -> DIAGNOSED -> MITIGATING -> RESOLVED -> CLOSED with an explicit transition table, reopen paths, a note required to close an unresolved incident, transition rows with timestamps and actors, per-incident time to detect/diagnose/resolve and their means (D92, D93).
- `app/api/jobs.py`: async diagnosis jobs on a thread pool (PENDING/RUNNING/SUCCEEDED/FAILED); jobs move the incident to INVESTIGATING and DIAGNOSED, persist anomalies, ranked evidence, the diagnosis and the deterministic severity, and record the onset; interrupted jobs are failed at start-up; retrieval falls back with a recorded note when the knowledge base cannot be built (D97).
- `app/api/security.py` + `app/main.py`: API keys (`X-API-Key` or HTTP Basic password, constant-time comparison, keys from env only, fail closed without keys, `auth_disabled` refused in aws mode); per-client token-bucket rate limiting (per IP for failed attempts); body size limit; audit middleware (all changes and all denials, actor = key hash or IP); error handlers that never echo inputs or internals (D94, D96).
- `app/api/service.py`, `app/api/schemas.py`: persistence (incidents, events with contract checks, inventory, anomalies, evidence, diagnoses), strict request models, secret filtering of event text in responses.
- `app/logging_config.py`: configured keys are scrubbed from every log line; bearer/basic tokens and `x-api-key` values too. Bug fixed: `Authorization: Bearer <token>` used to lose only the word "Bearer" (D94).
- DB: migration `0003` (incident lifecycle columns; `incident_transitions`, `diagnosis_jobs`, `audit_log`), verified on PostgreSQL 16 (upgrade, empty model diff, downgrade, upgrade).
- Config: `api` section extended; `cryptography` is now a base dependency (SNS signatures).

Gate status:
- [x] API tests for all endpoints (`tests/test_api.py`): create/list/get/dedup, validation (9 bad inputs), events/inventory ingest, offline import, every read view, async diagnosis end to end (job -> INVESTIGATING -> DIAGNOSED, diagnosis with severity and citations, ranked evidence, anomalies on metrics), job failures, interrupted-job recovery, experiments; route coverage is checked against the OpenAPI schema so a new route without a test fails.
- [x] Lifecycle transition tests: full path with reopen, illegal moves (409), closing without a note, terminal CLOSED, transition timestamps and actors, exact timings and aggregate means.
- [x] Security tests: every `/api` route returns 401 without a key, with a wrong key and with wrong Basic auth; fail closed without keys; `auth_disabled` refused in aws mode; per-key and per-IP rate limits with Retry-After; body and batch limits; audit of changes and denials without keys; no secret leakage in logs (keys, bearer tokens, passwords), in responses (event text, validation errors, 500s) or in job errors; SNS signatures (valid v1/v2, tampered, unsigned, foreign certificate host, wrong topic).
- [x] `pytest` 358 passed, 1 skipped; `ruff check` and `ruff format --check` clean.

Known limits:
- One role only (every key can do everything); multi-user roles are out of scope until Phase 10 (BRIEF).
- Rate limits and the job queue are in-process: several API replicas need a shared limiter and queue (e.g. Redis).
- Diagnosis jobs use stored telemetry: real incidents need the capture CLI plus `POST /events` and `/inventory`; the API does not call AWS collectors itself.
- `/docs` and `/openapi.json` are public (schema only, no data).
- Timings use the incident's creation time for diagnose/resolve, so imported historical incidents measure time since import, not since the original alarm (D93).

## Next: Phase 9 (frontend)
React + Vite + TypeScript + Tailwind + Recharts + React Flow: incident overview, interactive timeline, metrics charts, searchable logs, CloudTrail view, dependency graph, diagnosis page (root cause -> evidence -> timeline -> resource -> dependency path, FACT/INFERENCE/HYPOTHESIS/RECOMMENDATION labels, contradicting evidence, alternatives), all against the Phase 8 API. Gate: typecheck + build, component tests, scripted end-to-end walk-through on offline data.

Phase 7 carry-over (D71), done: the knowledge-base conditions are in the matrix; besides B1-B4, Full and A1-A5, evaluate RAG with (a) the query's fault type removed from the knowledge base and (b) distractor entries that share alarm metric, topology and wording but differ in root cause; report accuracy, `historical_influence` declarations and `validate_historical_influence` failures for Full vs A1 under each.

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
