# Deployment

Two compose configurations share one set of images:

| | Offline quickstart (default) | Production override |
|---|---|---|
| Command | `docker compose up --build` | `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build` |
| Data | synthetic dataset baked into the image | your incidents (alarm webhook, capture CLI); the offline import still works |
| LLM | deterministic stub | stub unless you configure a provider |
| API key | `demo-key-change-me` (development only) | required, at least 16 characters, demo key refused |
| Published ports | 3000 (UI), 8000 (API), 5432 (db), all on 127.0.0.1 | `FRONTEND_PORT` (default 8080) only |
| Containers | non-root | non-root, read-only root filesystem, no capabilities, restart policy |

## Offline quickstart (no AWS credentials, no LLM key)

Requirements: Docker with Compose v2.24 or newer, about 3 GB of free disk for the images.

```bash
git clone https://github.com/navya906/incident-diagnosis-aws.git
cd incident-diagnosis-aws
docker compose up --build
```

The first build takes a few minutes: it installs the backend and generates the synthetic
dataset (124 incidents) and the historical corpus inside the API image. When all three
containers are healthy:

1. Open http://localhost:3000 and enter the API key `demo-key-change-me`.
2. Choose an incident under "Import an offline incident" and press **Import**.
3. Open **Diagnosis** and press **Run diagnosis**. After a few seconds the page shows the root
   cause, the supporting evidence with the event behind each citation, the evidence timeline,
   the resource and dependency path, contradicting evidence, alternative hypotheses and
   recommendations, each statement labelled FACT, INFERENCE, HYPOTHESIS or RECOMMENDATION.
4. Explore Timeline, Metrics, Logs, CloudTrail and Graph; move the incident through the
   lifecycle on Overview.

All output in this mode is **smoke-test / synthetic**: simulator data and the stub LLM, which
demonstrates the pipeline, not diagnostic quality.

Check the whole flow from a terminal (standard-library Python, no install needed):

```bash
python scripts/quickstart_check.py
```

It checks health and authentication, imports an incident, runs a diagnosis, verifies that
every citation resolves to a stored event, opens every view and closes the incident. The
browser end-to-end tests can run against the same stack (they need Node.js and the dataset
manifest, which `python -m app.offline.generate` in `backend/` recreates identically):

```bash
cd frontend
E2E_BASE_URL=http://localhost:3000 E2E_API_KEY=demo-key-change-me npx playwright test
```

Stop with `docker compose down` (add `-v` to delete the database volume).

To use another key: `CLOUDDIAG_API__API_KEYS='["your-key"]' docker compose up`.

## Production

1. Create `.env` next to `docker-compose.yml` (never commit it):

   ```bash
   POSTGRES_PASSWORD=<long random password>
   CLOUDDIAG_API__API_KEYS=["<random key, 32+ characters>"]
   FRONTEND_PORT=8080
   # Optional real LLM (see docs/experiments/RUNBOOK.md for model names):
   # CLOUDDIAG_LLM__PROVIDER=openai_compatible
   # CLOUDDIAG_LLM__MODEL=<model>
   # CLOUDDIAG_LLM__BASE_URL=https://api.openai.com/v1
   # CLOUDDIAG_LLM__API_KEY=<key>
   # Real AWS telemetry (makes the full redaction policy mandatory):
   # CLOUDDIAG_DATA_MODE=aws
   ```

   Generate keys with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
   `POSTGRES_PASSWORD` takes effect when the volume is created; to change it later, change it
   inside PostgreSQL too.

2. Start: `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.
   Compose refuses to start without `POSTGRES_PASSWORD` and `CLOUDDIAG_API__API_KEYS`; the API
   refuses the demo key, short keys and disabled auth (`docker compose logs api`).

3. Put TLS in front: a load balancer or reverse proxy terminating HTTPS and forwarding to
   `FRONTEND_PORT`. nginx (inside the frontend container) proxies `/api` and `/health` to the
   API, which is not published.

4. Verify: `python scripts/quickstart_check.py --url https://<host> --key <key>` (it imports
   one offline incident; skip it if you do not want demo data in production).

5. Alarms: subscribe the API to your CloudWatch alarms (SNS or EventBridge,
   `docs/api.md#alarm-webhook-setup`). Telemetry for real incidents comes from the capture CLI
   (`docs/aws-setup.md`) posted to `/api/incidents/{id}/events` and `/inventory`.

### What the production override changes

- `CLOUDDIAG_ENVIRONMENT=production` (strict key checks), JSON logs.
- Database and API ports are not published; only nginx is.
- `FORWARDED_ALLOW_IPS="*"` for the API: safe only because the API is reachable from the
  compose network alone, and nginx overwrites `X-Forwarded-For` with the real client address.
- Read-only root filesystems with `/tmp` as tmpfs, `cap_drop: ALL`, `no-new-privileges`,
  `restart: unless-stopped`.

## Configuration

Defaults: `config/default.yaml`. Override any key with `CLOUDDIAG_<SECTION>__<KEY>`, e.g.
`CLOUDDIAG_EVIDENCE__TOP_K=20`. Secrets come from the environment only. Most relevant:

| Variable | Default (compose) | Meaning |
|---|---|---|
| `CLOUDDIAG_API__API_KEYS` | `["demo-key-change-me"]` | JSON list of accepted keys |
| `CLOUDDIAG_ENVIRONMENT` | `development` | `production` enables strict key checks |
| `CLOUDDIAG_DATA_MODE` | `offline` | `aws` makes redaction mandatory (fail closed) |
| `CLOUDDIAG_LLM__PROVIDER` / `MODEL` / `BASE_URL` / `API_KEY` | `stub` | diagnosis model |
| `CLOUDDIAG_EMBEDDINGS__PROVIDER` | `hashing` | `sentence_transformers` needs the `embeddings` extra in the image |
| `CLOUDDIAG_API__RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_BURST` | 120 / 30 | per key |
| `CLOUDDIAG_API__JOB_WORKERS` | 2 | concurrent diagnosis jobs |
| `CLOUDDIAG_LOG_JSON` | `false` (`true` in production) | structured logs |

## Operations

- **Health:** `GET /health` (public) reports API and database status; both images have Docker
  health checks.
- **Migrations** run at API start (`alembic upgrade head`).
- **Backups:** the `pgdata` volume holds everything (incidents, telemetry, diagnoses, audit);
  back it up with `pg_dump`.
- **Scaling:** run one API replica. Diagnosis jobs and rate limits are in-process (D97); more
  replicas need a shared queue and limiter. Raise `JOB_WORKERS` for more concurrent jobs.
- **Interrupted jobs** (API restart during a job) are marked FAILED at start-up; run them again.
- **Upgrades:** pull, rebuild, restart; migrations apply automatically.

## Without Docker

`python -m app.demo` in `backend/` runs the API on SQLite with the offline dataset and the
stub; `npm run dev` in `frontend/` serves the UI on http://localhost:5173 (README, section 3).
