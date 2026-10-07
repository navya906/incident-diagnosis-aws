# Testing

## Categories

Every backend test has exactly one category marker; `backend/tests/conftest.py` assigns and
checks it (D109):

| Category | Definition | How it is assigned |
|---|---|---|
| `unit` | one module or class with in-memory inputs: no database, HTTP app, AWS mock or generated dataset | default |
| `integration` | several components together, in-process: the API app with SQLite, migrations, moto/Stubber AWS mocks, the generated dataset, the full diagnosis pipeline, the experiment runner | module marker (`test_api.py`, `test_db_and_api.py`, `test_experiments.py`, `test_aws_collectors.py`, `test_offline_dataset.py`) or use of a fixture that builds real components (`dataset`, `client`, `aws`, `world`, `small_run`, ...) |
| `e2e` | the running system through its external interface: a separately started server process over HTTP (`test_e2e_http.py`) or a browser against the built UI and a real backend (`frontend/e2e/`, Playwright) | explicit marker |

The frontend component tests (Vitest + Testing Library, `frontend/src/**/*.test.ts(x)`) render
one component or call one module with a mocked `fetch`; they count as unit tests.

## Counts (2026-10-08, `phase10/hardening-docs`)

| Suite | Unit | Integration | End-to-end | Command |
|---|---|---|---|---|
| Backend (pytest) | 225 | 164 | 3 | `cd backend && pytest -q` (prints `test categories (selected): ...`) |
| Frontend components (Vitest) | 30 | | | `cd frontend && npm test` |
| Browser (Playwright) | | | 5 | `cd frontend && npm run e2e` |
| **Total** | **255** | **164** | **8** | BRIEF Phase 10 minimum: 30 / 10 / 5 |

The live pgvector test is skipped locally without `CLOUDDIAG_TEST_PG_URL`; CI's `pgvector` job
runs it and fails if it is skipped.

## End-to-end tests

| Test | What it proves |
|---|---|
| `backend/tests/test_e2e_http.py::test_quickstart_flow_import_diagnose_and_close` | the quickstart flow over HTTP against `python -m app.demo`: auth, offline import without ground truth, async diagnosis, every citation resolves to a stored event, every view, lifecycle to CLOSED |
| `...::test_alarm_webhook_opens_one_incident_per_alarm` | EventBridge alarm opens one incident, repeats deduplicate, OK states are ignored; a diagnosis without telemetry fails instead of inventing a conclusion |
| `...::test_auth_rate_limit_and_audit_over_the_wire` | 401 without or with a wrong key, failed attempts limited with 429 + `Retry-After`, denials audited, keys never in logs or audit |
| `frontend/e2e/walkthrough.spec.ts` | the whole UI on one incident: import, diagnose, labelled evidence, timeline, metrics, logs and CloudTrail search, graph roles, lifecycle to CLOSED; screenshots of every step |
| `frontend/e2e/evidence-rule.spec.ts` | an insufficient-evidence incident never shows a root cause (D102), on the diagnosis page, the overview or the list |
| `frontend/e2e/lifecycle.spec.ts` | closing an unresolved incident needs a note; the refusal is shown; CLOSED is terminal (server returns 409) |
| `frontend/e2e/auth.spec.ts` | wrong key refused, key per tab in sessionStorage only, never in URLs, forgotten on request, required again in a new context |
| `frontend/e2e/navigation.spec.ts` | every incident tab opens by deep link (SQS topology), graph text list matches the nodes, no conclusion before a diagnosis |

The same checks run against the Docker stack: `python scripts/quickstart_check.py` and
`E2E_BASE_URL=http://localhost:3000 E2E_API_KEY=demo-key-change-me npx playwright test`
(`docs/deployment.md`).

## Gates that are tests

- Contracts and the diagnosis schema accept/reject cases (`test_contracts.py`,
  `test_diagnosis_schema.py`).
- Dataset regeneration is byte-identical; ground truth validates (`test_offline_dataset.py`).
- Real collectors and the replay collector emit schema-identical events (`test_aws_collectors.py`).
- Leakage: no test incident in any knowledge base (`test_redaction_rag.py`).
- Redaction: nothing recognisable leaves the process (`test_redaction_rag.py`,
  `test_redaction_hardening.py`).
- Experiments reproduce by id, and a code change alone is reported separately from a change in
  results (`test_experiments.py`, D107).
- Migrations match the models (`test_db_and_api.py`).
- Docs: required documents exist, state the required limitations, cover every route and table,
  their links resolve, and every experiment or report id they cite exists (`test_docs.py`).

## CI (`.github/workflows/ci.yml`)

| Job | Runs |
|---|---|
| backend (with and without the `rag` extra) | ruff, ruff format check, pytest (all categories) |
| pgvector | migrations on PostgreSQL 16 + pgvector, the live vector-store test, downgrade/upgrade |
| frontend | typecheck, Vitest, production build |
| e2e | Playwright suite against `app.demo` and the built app; screenshots as an artifact |
| reproducibility | `python -m app.experiments verify` for the experiment cited in `docs/evaluation.md`, `python -m app.evaluation.provenance check` |
| docker | builds both images, `docker compose up --wait`, `scripts/quickstart_check.py` against the stack |
