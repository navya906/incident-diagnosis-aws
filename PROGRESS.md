# Progress

## Phase 0: Foundations and contracts — implemented, gate PARTIALLY verified

Built: repo skeleton; `docker-compose.yml` (db = pgvector/pgvector:pg16, api, frontend placeholder);
config (YAML + env); secret-scrubbing logging; contracts (`CanonicalEvent`, taxonomy, `GroundTruth`,
`Diagnosis`, evidence/score weights, anomaly); interfaces (`Collector`, `Detector`, `GraphStore`,
`VectorStore`, `LLMClient`, `Embedder`); 10 DB models + Alembic migration `0001`; `/health` endpoint.

Gate status:
- [x] `pytest` passes: 38 tests (contracts, diagnosis accept/reject, config, logging, interfaces, migration vs models, migration up/down, `/health`).
- [x] `ruff check` clean.
- [x] Schemas validate with unit tests, including diagnosis accept/reject cases.
- [ ] **`docker compose up` brings up api + db: NOT VERIFIED.** Docker is not installed on this machine, so the compose file, Dockerfile and PostgreSQL migration were never run. Migration was only exercised on SQLite. Verify with `docker compose up --build` then `curl localhost:8000/health`.

## Next: Phase 1 (scenario generator, offline dataset, replay collector)
Do not start until the Docker gate item is confirmed (or explicitly waived).

## How to run tests
```
cd backend
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check .
```

## Repository
Pushed to https://github.com/navya906/incident-diagnosis-aws (branch `main`). `BRIEF.md` holds the build brief plus a status/amendments section.
