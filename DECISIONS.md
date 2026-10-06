# Decisions

| # | Phase | Decision | Why |
|---|---|---|---|
| D1 | 0 | Local Python is 3.12; code targets `>=3.11` and Docker image uses 3.11. | Brief fixes 3.11; no 3.12-only syntax is used except `StrEnum`/`datetime.UTC` (both 3.11+). |
| D2 | 0 | Tests run on SQLite; DB models use generic `JSON` columns. Migration `CREATE EXTENSION vector` runs on PostgreSQL only. | Hermetic tests with no Docker. A test asserts the migration matches the models. |
| D3 | 0 | The pgvector embedding column is NOT in migration 0001; it is added in Phase 5 with the `VectorStore`. `historical_incidents` stores `embedding_model`/`embedding_dim` metadata now. | Embedding dimension depends on the chosen model (one model per index). |
| D4 | 0 | ORM attribute for event metadata is `meta` (SQLAlchemy reserves `metadata`); `extra` used on other tables. API/contract field stays `metadata`. | Reserved name. |
| D5 | 0 | `Diagnosis` schema validates: conclusions need supporting evidence; `insufficient_evidence` forces `requires_human_review`; root + alternative confidences sum <= 1; alternatives ranked descending. The low-confidence review rule is the helper `needs_review(diagnosis, threshold)` because the threshold is config. | Brief says "true when confidence < configured threshold". |
| D6 | 0 | `event_id` = `ev_` + first 16 hex of sha256 over (UTC timestamp, source, service, resource_id, event_type, metric, raw_ref). Naive timestamps are treated as UTC. | Deterministic and timezone-independent. |
| D7 | 0 | Default evidence weights: temporal .25, resource .20, anomaly .25, semantic .15, dependency .15. | Placeholder; tuned on dev split only (Phase 4/7). |
| D8 | 0 | Config priority: init > env (`CLOUDDIAG_*`, `__` nesting) > `.env` > `config/default.yaml` > defaults. API keys are env-only and held as `SecretStr`. | Brief: YAML + env. |
| D9 | 0 | DB connections to non-SQLite URLs use `connect_timeout=3`. | `/health` must fail fast when the DB is down. |
| D10 | 0 | Frontend placeholder is a static page served by nginx in Compose. | Real React app is Phase 9. |
| D11 | 0 | Contracts for `Anomaly`/`MetricSeries`, `ScoreWeights`/`EvidenceItem`, `CollectionRequest` were added beyond the brief's list because the interfaces need typed signatures. | Interfaces cannot be typed otherwise. |
| D12 | 0 | The build brief is stored in the repo as `BRIEF.md`, with a status/amendments section appended at the end. | Keeps the spec next to the code it governs. |
| D13 | 0 | Remote is `https://github.com/navya906/incident-diagnosis-aws.git`, default branch `main`. `.venv`, `.env`, caches and generated data are git-ignored. | Requested by the user. |
