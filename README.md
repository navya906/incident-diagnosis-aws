# Generative-AI-Based AWS Cloud Incident Diagnosis

A research-grade system that diagnoses AWS incidents by selecting the right evidence from multi-source
telemetry, reasoning over AWS dependencies, retrieving similar past incidents, and producing a
structured, evidence-cited root-cause diagnosis, then measuring honestly whether each component helps.

> Read `BRIEF.md` (the spec), `PROGRESS.md` (where we are) and `DECISIONS.md` (why things are the way
> they are) **before** you touch anything. Current status: **Phase 0 done** (Docker gate unverified).

## 1. Requirements

| Tool | Version | Needed for |
|---|---|---|
| Python | 3.11 or newer (3.12 works) | backend, tests |
| Git | any recent | version control |
| Docker Desktop + Compose v2 | any recent | running api + Postgres/pgvector (Phase 0 gate, Phase 10 demo) |
| Node.js | 20+ | frontend, from Phase 9 only |
| AWS account / credentials | optional | only for real collectors (Phase 2) and real test stacks; **not needed** for offline mode |
| LLM API key (OpenAI-compatible or Gemini) | optional | only for real-LLM experiments; everything else runs on the deterministic stub |

Python libraries are declared in `backend/pyproject.toml` (single source of truth):
FastAPI, Uvicorn, Pydantic v2 + pydantic-settings, SQLAlchemy 2, Alembic, psycopg 3, NumPy, pandas,
scikit-learn, NetworkX, PyYAML. Dev extra: pytest, httpx, ruff. `aws` extra (Phase 2+): boto3, moto.
Planned later: SentenceTransformers, FAISS, pgvector client (Phases 5+).

## 2. Setup (virtual environment)

Always work inside the project virtual environment at `backend/.venv` (git-ignored).

**Windows (PowerShell)**
```powershell
cd backend
py -3.11 -m venv .venv        # or: python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

**macOS / Linux**
```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Or run the helper, which does the same: `scripts/setup.ps1` (Windows) or `scripts/setup.sh` (macOS/Linux).

Optional: `cp .env.example .env` (repo root) and adjust. Never commit `.env` or any key.

## 3. Everyday commands

Run from `backend/` with the venv active.

| Task | Command |
|---|---|
| Run tests | `pytest -q` |
| Lint | `ruff check .` |
| Auto-fix lint/imports | `ruff check . --fix` |
| Run API locally (needs a DB, or SQLite URL) | `uvicorn app.main:app --reload` |
| Apply migrations | `alembic upgrade head` |
| New migration after model change | `alembic revision --autogenerate -m "describe change"` |

Run against SQLite without Docker:
```bash
export CLOUDDIAG_DATABASE_URL=sqlite+pysqlite:///./dev.db     # PowerShell: $env:CLOUDDIAG_DATABASE_URL="..."
alembic upgrade head && uvicorn app.main:app --reload
```

Full stack with Docker (from repo root):
```bash
docker compose up --build
curl http://localhost:8000/health      # {"status":"ok","database":"ok",...}
```

## 4. Configuration

Defaults live in `config/default.yaml`. Override any key with an environment variable
`CLOUDDIAG_<KEY>` using `__` for nesting, e.g. `CLOUDDIAG_EVIDENCE__TOP_K=20`,
`CLOUDDIAG_LLM__PROVIDER=openai_compatible`. Priority: env > `.env` > YAML > code defaults.
Secrets (API keys) come from the environment only.

## 5. Repository layout

```
BRIEF.md  PROGRESS.md  DECISIONS.md   spec, status, decision log (keep in sync)
config/default.yaml                    default settings
docker-compose.yml                     api + pgvector Postgres + frontend placeholder
scripts/                               setup helpers
backend/
  pyproject.toml  alembic/  tests/
  app/
    contracts/    CanonicalEvent, taxonomy, GroundTruth, Diagnosis, evidence (never change silently)
    interfaces/   Collector, Detector, GraphStore, VectorStore, LLMClient, Embedder
    db/           SQLAlchemy models + session
    offline/ baselines/ ai/ evaluation/   filled in by later phases
frontend/                              placeholder until Phase 9
docs/                                  architecture, runbooks (grow per phase)
```

## 6. Working as a team (hand-off rules)

The project is built phase by phase (see `BRIEF.md`). To let several people continue seamlessly:

1. **Start of work:** `git pull`, read `PROGRESS.md` and `DECISIONS.md`, activate the venv, run `pytest`
   (it must be green before you begin).
2. **One phase owner at a time.** Phases are sequential; do not start phase N+1 until phase N's gate passes.
   Independent work inside a phase (e.g. separate collectors) can be split across branches.
3. **Branches and PRs:** branch from `main` as `phaseN/<short-topic>`; open a PR; CI (ruff + pytest) must pass.
   Do not push directly to `main` once more than one person is active.
4. **Contracts are frozen:** `app/contracts/` and `app/interfaces/` change only via a PR that also
   adds a `DECISIONS.md` entry and updates affected tests.
5. **End of work:** update `PROGRESS.md` (what is done, what is next, anything unverified) and add a row
   to `DECISIONS.md` for every non-obvious choice. Leave the tree green.
6. **Never fabricate results.** Anything from the stub LLM or synthetic data is labelled
   `smoke-test / synthetic`. Real numbers must trace to an experiment ID.
7. **No secrets in git.** `.env`, API keys, AWS credentials never get committed.
8. **Database changes** need an Alembic migration; a test checks that migrations match the models.
9. **Line endings/format:** `.gitattributes` and `.editorconfig` enforce LF; `ruff` enforces style.

See `CONTRIBUTING.md` for the detailed checklist.

## 7. Known gaps

- `docker compose up` has not yet been verified (Docker was unavailable on the first dev machine).
  Whoever has Docker first should verify and tick the box in `PROGRESS.md`.
