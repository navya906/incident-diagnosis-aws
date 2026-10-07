# Build Brief: Generative-AI-Based AWS Cloud Incident Diagnosis

You are a senior software architect, cloud reliability engineer, and applied-ML researcher. Build a research-grade system that diagnoses AWS incidents by selecting the right evidence from multi-source telemetry, reasoning over AWS dependencies, retrieving similar past incidents, and producing a structured, evidence-cited root-cause diagnosis. Then measure, honestly, whether each component helps.

**Core contribution:** evidence selection + multi-source correlation + dependency reasoning + historical retrieval + LLM diagnosis, evaluated against baselines and ablations. It is NOT "an LLM summary of CloudWatch logs."

---

## 0. Working rules (read first, every session)

1. **One phase per session.** At the start, read `PROGRESS.md` and `DECISIONS.md`. At the end, update both. Do not begin the next phase until the current phase's Gate passes (tests green, lint clean, gate checklist met).
2. **Every phase:** implement → write tests → run tests → fix → update docs.
3. **Never fabricate results.** You cannot reach real AWS or a real LLM from the build sandbox. Anything produced with the stub LLM or synthetic data must be labelled `smoke-test / synthetic` in outputs and docs. Provide exact commands and a results template so the user can run real-LLM and real-AWS experiments themselves.
4. **No TODOs, pseudocode, or empty stubs in core paths.** Where an external dependency can't run here (AWS, LLM keys), implement the full interface plus a deterministic local implementation, marked `LOCAL-ONLY` and documented.
5. **Ambiguity:** choose the simplest reasonable option, record it in `DECISIONS.md`, continue. Ask only if truly blocked.
6. **Scope discipline:** do not build anything in the Out-of-scope list until Phase 10 is complete.

## 1. Fixed decisions

- Python 3.11, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, PostgreSQL + pgvector (FAISS behind the same `VectorStore` interface), boto3, pandas, NumPy, scikit-learn, NetworkX behind a `GraphStore` interface, pytest, moto for AWS mocks.
- Frontend: React, Vite, TypeScript, Tailwind, Recharts, React Flow (dependency graph), React Router.
- Docker + Docker Compose.
- LLM abstraction: OpenAI-compatible endpoint (also covers local models), Google Gemini. Embeddings: SentenceTransformers (default), OpenAI, Gemini. **One embedding model per vector index; model name and dimension stored in index metadata. Never mix models in one index.**
- Reference topology for all 10 scenarios: `Internet → ALB → ECS (Fargate) → RDS`, plus Lambda, SQS, and EC2 variants.
- Repository layout: as in the original spec, plus `backend/app/baselines/`, `backend/app/offline/` (replay collector, dataset loader), `backend/app/ai/redaction.py`, `backend/app/evaluation/stats.py`, `PROGRESS.md`, `DECISIONS.md`.

## 2. Core contracts (defined in Phase 0, never changed silently)

**CanonicalEvent**: `event_id` (deterministic hash), `timestamp`, `source`, `service`, `resource_id`, `event_type`, `metric`, `value`, `severity`, `message`, `metadata`, `raw_ref`. All collectors (real and replay) emit exactly this.

**Root-cause taxonomy (closed set)**: `deployment_failure`, `connection_exhaustion`, `function_timeout`, `cpu_saturation`, `lb_error_spike`, `iam_permission_failure`, `security_group_misconfiguration`, `db_latency_increase`, `task_crash_loop`, `queue_backlog`, `other`, `insufficient_evidence`.

**Ground truth per incident**: taxonomy label, primary resource id, free-text root cause, true onset time, ground-truth evidence event IDs, resolution.

**Evidence score** (replaces the original multiplicative formula, which zeroes out any item missing one signal, e.g. a CloudTrail deployment has no metric anomaly):

`score = w_t·temporal + w_r·resource + w_a·anomaly + w_s·semantic + w_d·dependency`

Each component is in [0,1]. Weights come from config. Event types without a numeric anomaly (deployments, config changes) get an event-type prior for the anomaly component, not 0. Ablating a signal = setting its weight to 0. Return top-K (configurable).

**Diagnosis schema (Pydantic)**:

```
incident_summary
root_cause: {taxonomy_label, description, confidence, resource_id, label: INFERENCE}
supporting_evidence: [{evidence_id, explanation, label: FACT|INFERENCE}]
contradicting_evidence: [{evidence_id, explanation}]
contributing_factors: [{description, label}]
alternative_hypotheses: [{taxonomy_label, description, confidence, rejected_because, label: HYPOTHESIS}]   # ranked; root cause + alternatives confidences sum <= 1
historical_influence: {used: bool, incident_ids: [], how: ""}
impact_analysis: {services, resources, blast_radius, user_impact}
severity_suggestion: LOW|MEDIUM|HIGH|CRITICAL      # advisory only
recommendations: [{category: IMMEDIATE|INVESTIGATIVE|CORRECTIVE|PREVENTIVE, action, label: RECOMMENDATION}]
missing_information: []
requires_human_review: bool                        # true when confidence < configured threshold or insufficient_evidence
```

Invalid output → one structured repair attempt → else reject and record the failure.

## 3. Evaluation rules (apply from Phase 1 onward)

- **Dev/test split** of scenarios. Rules, prompts, weights, and thresholds are tuned on dev only. Test is touched only by the final experiment runner.
- **Blinded incident descriptions**: describe symptoms only; they must not name the root cause or the failing service's fault.
- **RAG leakage control**: the historical knowledge base is built from a separate corpus; test incidents are never in the index (also run leave-one-out).
- **Root-cause match**: deterministic taxonomy label + resource match (primary metric), plus a rubric-based LLM judge for free text with a spot-check sample for human review.
- **Faithfulness / hallucination**: automated citation verification (every cited `evidence_id` exists in the supplied context; quoted values match). Report unsupported-claim rate.
- **Confidence**: report verbalized confidence AND self-consistency confidence (agreement across N samples). Report ECE/reliability with a caveat about small sample sizes.
- **Statistics**: N runs per condition, bootstrap confidence intervals, paired comparisons across conditions on the same incidents.
- Record latency, tokens, and estimated cost per diagnosis.

## 4. Experimental conditions (defined once, used everywhere)

| ID | Condition |
|---|---|
| B1 | Rule-based diagnosis (rules written on dev scenarios only) |
| B2 | LLM + description only |
| B3 | LLM + description + raw telemetry, truncated to the same token budget by recency |
| B4 | LLM + description + ranked evidence (temporal, resource, anomaly, semantic signals; no graph, no RAG) |
| Full | B4 + dependency graph + historical RAG |
| A1 | Full − RAG |
| A2 | Full − dependency graph (weight 0, no graph context) |
| A3 | Full − anomaly detection (weight 0, no anomaly context) |
| A4 | Full − temporal correlation (no chains, temporal weight 0) |
| A5 | Full − evidence ranking (top-K by recency) |

RQ6 experiments: sweep K ∈ {5, 10, 20, 40}, time window ∈ {5, 15, 30, 60 min}, and evidence-type subsets (metrics only, +CloudTrail, +logs, +Config).

---

## Phase 0: Foundations and contracts

**Build:** repo skeleton, Docker Compose (api, postgres+pgvector, frontend placeholder), config system (YAML + env, Pydantic settings), logging, the contracts in Section 2, abstract interfaces (`Collector`, `Detector`, `GraphStore`, `VectorStore`, `LLMClient`, `Embedder`), minimal DB models + first Alembic migration (incidents, events, aws_resources, resource_relationships, anomalies, evidence, diagnoses, historical_incidents, evaluation_runs, experiment_results), test tooling, `PROGRESS.md`, `DECISIONS.md`.
**Gate:** `docker compose up` brings up api + db; schemas validate with unit tests (including diagnosis schema accept/reject cases); `pytest` passes.

## Phase 1: Scenario generator, offline dataset, replay collector

**Build:** a seeded, deterministic simulator that generates telemetry (metrics, logs, CloudTrail, Config changes) and ground truth for the 10 fault types × ≥10 variants each (varying onset, severity, noise, metric resolution of 1 vs 5 minutes). Include ≥10 red-herring cases (unrelated deployment/config change near onset), ≥5 insufficient-evidence cases, ≥5 compound/concurrent-fault cases. Realistic noise and missing data. Dataset manifest with version + dev/test split. `ReplayCollector` implementing the same interface as real collectors. Also write fault-injection scripts and a small IaC template for a real test stack (ALB+ECS+RDS) so the user can capture a few real incidents later (do not run them).
**Gate:** ≥100 cases load through `ReplayCollector` into CanonicalEvents; regeneration with the same seed is byte-identical; ground truth validates against the taxonomy.

## Phase 2: Real AWS collectors

**Build:** boto3 collectors for CloudWatch Metrics (batched `GetMetricData`), CloudWatch Logs (bounded, filtered), CloudTrail (`LookupEvents`; handle its low rate limit and ingestion lag), AWS Config history (optional when no recorder), resource inventory/relationship discovery for the graph. Collection is driven by the incident's affected resources and window, not global polling. Pagination, retry/backoff, caching, sensitive-field filtering. Least-privilege IAM policy JSON and an AWS setup guide.
**Gate:** moto-based tests pass; a contract test proves real collectors and `ReplayCollector` emit schema-identical CanonicalEvents; no credentials in source.

## Phase 3: Anomaly detection

**Build:** z-score, modified z-score (MAD), moving average, rolling std, configurable thresholds, Isolation Forest; handle variable metric resolution and sparse data. Each anomaly carries score, baseline, observed value, method, timestamp, metric, resource. No LLM involvement. Use the simulator's injected-fault windows as anomaly labels to compare methods (precision, recall, detection delay).
**Gate:** comparison table of detectors on the dev split, labelled synthetic; unit tests for each method.

## Phase 4: Correlation, dependency graph, evidence ranking

**Build:** investigation windows (5/15/30/60 min), temporal proximity ranking, event-chain detection, candidate-cause identification (temporal precedence + dependency path; name it "candidate cause", not "causal"). NetworkX dependency graph with node/edge types, upstream/downstream/blast-radius queries, built from inventory (real) or dataset (offline). Evidence ranking per Section 2 with configurable weights and K; stable evidence IDs.
**Gate:** evidence precision/recall@K against ground-truth evidence on dev; chain-detection tests including red-herring cases.

## Phase 5: Redaction, embeddings, historical RAG

**Build:** configurable redaction (account IDs, ARNs, IPs, principals, secrets) with consistent pseudonyms, applied before ANY external LLM call; embedder abstraction; vector store (pgvector, FAISS fallback) with model metadata; historical-incident knowledge base from a corpus separate from the test set; similarity retrieval + re-ranking; "do not blindly reuse past diagnoses" enforced via prompt and the `historical_influence` field.
**Gate:** retrieval tests; a leakage test proves test incidents never appear in the index; redaction tests (no raw identifiers leave the process when redaction is on).

## Phase 6: Diagnosis engine and severity

**Build:** LLM clients (OpenAI-compatible, Gemini), versioned prompt builder, context builder with token budget and ordered sections (incident, timeline, anomalies, logs, CloudTrail, graph, historical), output validator + repair, citation verifier, self-consistency sampling, deterministic stub LLM for tests (`LOCAL-ONLY`), deterministic severity engine (affected services/resources, error rate, duration, availability; business criticality as static config input). The LLM severity is advisory only.
**Gate:** end-to-end pipeline on dev incidents with the stub LLM; validator rejects malformed/uncited outputs; severity tests.

## Phase 7: Evaluation, baselines, ablations

**Build:** B1–B4 and Full (Section 4), ablations A1–A5, RQ6 sweeps; all metrics (taxonomy accuracy, top-K, evidence P/R, calibration, hallucination rate, recommendation quality via rubric, latency, tokens/cost); YAML experiment configs; experiment IDs storing model, model version, prompt version, dataset version, config, timestamp, retrieved evidence, diagnosis, ground truth, metrics; CSV/JSON export; bootstrap CIs and paired tests; CLI runner with `--split dev|test`.
**Gate:** full matrix runs end-to-end with the stub LLM on dev and is reproducible by experiment ID; outputs clearly labelled smoke-test; `docs/experiments/RUNBOOK.md` gives exact commands for real-LLM runs on the test split.

## Phase 8: Backend API, lifecycle, security

**Build:** REST endpoints from the original spec, plus an alarm-ingestion webhook (CloudWatch Alarm → SNS/EventBridge → `POST /api/incidents`). Async diagnosis jobs. Incident lifecycle (DETECTED→…→CLOSED) with transition timestamps and per-incident time-to-detect/diagnose/resolve, plus aggregate means. API-key auth, rate limiting, input validation, audit logging, secret filtering.
**Gate:** API tests for all endpoints, lifecycle transition tests, security tests (auth required, rate limit, no secret leakage in logs/responses).

## Phase 9: Frontend

**Build:** Incident overview, interactive timeline, metrics charts around the incident window, searchable logs, CloudTrail view, dependency graph (React Flow) highlighting affected/upstream/downstream nodes, AI diagnosis page showing root cause → supporting evidence → timeline → resource → dependency path, with FACT / INFERENCE / HYPOTHESIS / RECOMMENDATION labels, contradicting evidence, and alternative hypotheses. Never display a conclusion without its evidence.
**Gate:** typecheck + build pass, component tests, one scripted end-to-end walk-through on offline data.

## Phase 10: Hardening, Docker, documentation, final verification

**Build:** production Docker configs, offline-mode quickstart that works with no AWS credentials, ≥30 unit / ≥10 integration / ≥5 end-to-end tests overall, docs (README, architecture, API, DB schema, AWS setup, AI architecture, evaluation + experiment methodology, security, deployment, known limitations, future work). Known limitations must state: synthetic-data validity limits, small-sample calibration caveats, CloudTrail lag, LLM data-exposure risk.
**Gate:** fresh clone → `docker compose up` → offline demo diagnosis in the UI; full test suite green; docs complete; any reported numbers are traceable to experiment IDs.

---

## Out of scope until Phase 10 is done (stretch, in this order)

1. Conversational investigation assistant (must answer only from stored incident context, every claim cited).
2. Graph database backend.
3. Automated remediation (must require explicit human approval; never destructive by default).
4. Multi-user accounts and roles.

---

## Implementation status and amendments (maintained alongside PROGRESS.md and DECISIONS.md)

| Phase | Status |
|---|---|
| 0 Foundations and contracts | Implemented, gate passed (Docker verified 2026-10-06, D29). |
| 1 Scenario generator, dataset, replay | Implemented, gate passed (124 synthetic cases, byte-identical regeneration). Test stack + fault injection written, not run. |
| 2 Real AWS collectors | Implemented, gate passed (moto/Stubber tests, contract test vs replay, IAM policy, setup guide). Not yet run against a real account. |
| 3 Anomaly detection | Implemented, gate passed (5 detectors, dev comparison table labelled synthetic, unit tests per method). |
| 4 Correlation, graph, evidence ranking | Implemented, gate passed (dev evidence P/R@K table labelled synthetic and in-sample; chain tests incl. red herrings). |
| 5 Redaction, embeddings, historical RAG | Implemented, gate passed (retrieval, leakage and redaction tests; pgvector verified on PostgreSQL 16). |
| 6 to 10 | Not started. |

Amendments (details in DECISIONS.md):
- Python target is `>=3.11` (dev machine runs 3.12; Docker image uses 3.11).
- Extra contracts were added so interfaces are typed: `MetricSeries`/`Anomaly`, `ScoreWeights`/`EvidenceItem`, `CollectionRequest`.
- The pgvector embedding column is deferred from the first migration to Phase 5.
- The low-confidence `requires_human_review` rule is the helper `needs_review(diagnosis, threshold)`, because the threshold is configuration.
- Collaboration scaffolding (README, CONTRIBUTING, CI, PR template, setup scripts) was added; dependencies are declared only in `backend/pyproject.toml`.
- Phase 1: dataset is 124 cases (D18); observable and truth files are separate (D19); insufficient-evidence cases are made by stripping telemetry from a real fault (D21); compound cases use the earlier fault as primary (D24).
- Phase 2: per-source event schema added to the contract (D31); canonical ids from ARNs (D32); simulator stats aligned with real CloudWatch (D35).
- Phase 3: no contract change; detectors use trailing minute-based windows (D41); textbook thresholds kept as defaults and the dev sweep recorded (D46).
- Phase 4: contracts added for correlation outputs, `EvidenceRanking` and stable evidence ids (D56); failure impact uses a derived impact graph (D49); chains and candidate causes do not feed the evidence score (D52); evidence weights and detector threshold chosen on dev (D55).
- Phase 5: `is_external` flag on `LLMClient`/`Embedder` and `exclude_ids` on `VectorStore.search` (D60, D61); historical-record contracts, vector tables and migration 0002 added (D61, D62); the configured default embedder (SentenceTransformers) is an optional extra and the reported results use the LOCAL-ONLY hashing embedder (D60). External LLM/embedding clients can only be created through the wrapping factory (D67); new setting `data_mode` (offline or aws) makes redaction mandatory and fail-closed for real AWS data (D68); the 96% retrieval figure is an upper bound and Phase 7 adds knowledge-base conditions with the matching fault type removed and with distractors (D71).
