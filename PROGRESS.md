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

Branch: `phase3/anomaly-detection` (from `main`). Results: `docs/experiments/phase3-anomaly-detectors-dev.md` (+ `.csv`, `.json`).

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

## Next: Phase 4 (correlation, dependency graph, evidence ranking)
Investigation windows, temporal ranking, event chains, candidate causes, NetworkX `GraphStore`, evidence score per Section 2 with configurable weights and K. Gate: evidence precision/recall@K on dev and chain tests including red herrings. Choose the anomaly method/threshold for the anomaly component on dev only (see D46).

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
