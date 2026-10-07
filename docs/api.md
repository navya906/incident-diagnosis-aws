# REST API

Base path `/api`. `GET /health` is public; every `/api` route needs an API key. Interactive
OpenAPI docs: `/docs` (the schema only, no data).

## Authentication, limits, audit

- **Keys:** set `CLOUDDIAG_API__API_KEYS='["<key1>","<key2>"]'` (environment only). Send a key as
  `X-API-Key: <key>`, or as the password of HTTP Basic auth (`https://ingest:<key>@host/api/...`,
  the form an SNS HTTPS subscription can carry). With no keys configured every `/api` request is
  rejected. `api.auth_disabled: true` is for local development and is refused in aws mode.
  In production (`CLOUDDIAG_ENVIRONMENT=production`) and in aws mode the API refuses to start
  with the documented demo key (`demo-key-change-me`), with any key shorter than 16 characters,
  or with auth disabled (D108).
- **Rate limit:** a token bucket per key (per client IP for requests without a valid key):
  `rate_limit_per_minute` (120) refill, `rate_limit_burst` (30). Over the limit: `429` with
  `Retry-After`; the web UI waits for `Retry-After` and retries up to three times. Behind the
  bundled nginx the client address comes from `X-Forwarded-For`, which nginx overwrites with
  the connecting address; the API trusts it only from `FORWARDED_ALLOW_IPS` (production
  compose: the nginx container, the API port is not published).
- **Input:** strict request models (unknown fields rejected), body limit `max_body_bytes`, events
  limit `max_events_per_request`, canonical resource ids, windows of at most 48 h. Validation
  errors list the location and message only and never echo submitted values.
- **Audit:** every change (non-GET) and every denied request (401/403/413/429) is written to
  `audit_log` (actor = `key-<sha256 prefix>` or `ip:<address>`, never the key) and to the log.
  Read it with `GET /api/audit`.
- **Secrets:** configured keys are scrubbed from all log output. Event text in responses has
  secrets removed (key=value, bearer tokens, JWTs, private keys, URL passwords). Unhandled
  errors return `{"detail": "internal error", "request_id": ...}`, and job errors are stored
  scrubbed.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/incidents` | Create an incident from native JSON, an SNS notification or an EventBridge alarm event (webhook). Returns `201` when created, `200` for an open incident with the same dedup key, `202` for ignored alarm states or SNS subscription messages. |
| GET | `/api/incidents` | List incidents (`status`, `limit`, `offset`) with timings. |
| GET | `/api/incidents/{id}` | Detail: lifecycle history, timings, allowed next states, counts. |
| POST | `/api/incidents/{id}/transitions` | `{"to": "<STATUS>", "note": "..."}`. Returns `409` if the move is not allowed. |
| POST | `/api/incidents/{id}/events` | Ingest CanonicalEvents (checked against the per-source contract). |
| POST | `/api/incidents/{id}/inventory` | Ingest resources and relationships (dependency graph). |
| GET | `/api/incidents/{id}/events` | Search: `source` (repeatable), `resource_id`, `q` (text), `severity`, paging. |
| GET | `/api/incidents/{id}/logs` | Log lines (`q`, `severity`, `resource_id`). |
| GET | `/api/incidents/{id}/cloudtrail` | CloudTrail calls and AWS Config changes. |
| GET | `/api/incidents/{id}/metrics` | Metric series (`resource_id`, `metric`) with anomaly flags. |
| GET | `/api/incidents/{id}/timeline` | Non-metric events, anomalies and lifecycle transitions in time order. |
| GET | `/api/incidents/{id}/graph` | Nodes and typed edges, with `affected`, `upstream`, `downstream` and `can_cause` flags. |
| GET | `/api/incidents/{id}/evidence` | Ranked evidence (score components) with the events. |
| POST | `/api/incidents/{id}/diagnose` | Start an async diagnosis job: `{"condition": "Full"/"A1".."A5"/"B4", "samples": n}`. Returns `202` with `job_id`. |
| GET | `/api/jobs/{job_id}` | Job status: PENDING, RUNNING, SUCCEEDED or FAILED, with timestamps, `diagnosis_id` and error. |
| GET | `/api/incidents/{id}/diagnoses` | All diagnoses, newest first. |
| GET | `/api/incidents/{id}/diagnosis` | Latest diagnosis with deterministic severity, citations, self-consistency and context statistics. |
| GET | `/api/offline/incidents` | Offline dataset incidents available for import: id, split, title and alarm time only (no category, labels or other ground truth) (D99). |
| POST | `/api/offline/import` | `{"offline_incident_id": "inc-..."}`: create an incident from the offline dataset (observable data only). |
| GET | `/api/metrics/lifecycle` | Counts by status and mean time to detect, diagnose and resolve. |
| GET | `/api/audit` | Audit records (`limit`, `incident_id`). |
| GET | `/api/experiments`, `/api/experiments/{id}` | Stored experiment manifests and summaries. |

## Lifecycle

`DETECTED -> INVESTIGATING -> DIAGNOSED -> MITIGATING -> RESOLVED -> CLOSED`. A diagnosis job
moves DETECTED -> INVESTIGATING when it starts and INVESTIGATING -> DIAGNOSED when it succeeds.
DIAGNOSED and RESOLVED can go back to INVESTIGATING. Closing an incident that is not resolved
(for example a false alarm) needs a note. CLOSED is terminal. Every change is stored with its
timestamp and actor.

Timings per incident (minutes) and their means:

| Timing | Definition |
|---|---|
| time to detect | alarm time - onset (onset estimated by the pipeline, or given on creation) |
| time to diagnose | first DIAGNOSED - incident creation |
| time to resolve | first RESOLVED - incident creation |

## Alarm webhook setup

CloudWatch alarm -> **SNS** topic -> HTTPS subscription to
`https://ingest:<api key>@<host>/api/incidents`. Messages are signature-checked: SignatureVersion
1 or 2, with the certificate only from `https://sns.<region>.amazonaws.com/*.pem`. Restrict
topics with `api.allowed_sns_topic_arns`. Subscription confirmations are verified but never
auto-confirmed: confirm the subscription in the SNS console.

CloudWatch alarm -> **EventBridge** rule (`source: aws.cloudwatch`, `detail-type: CloudWatch
Alarm State Change`) -> API destination `POST https://<host>/api/incidents`, with a connection
that sends the `X-API-Key` header.

Only transitions into the ALARM state open an incident. Alarm dimensions map to canonical
resource ids:

| Dimension | Resource id |
|---|---|
| LoadBalancer | `alb/` |
| TargetGroup | `tg/` |
| DBInstanceIdentifier | `rds/` |
| FunctionName | `lambda/function/` |
| ServiceName (ECS) | `ecs/service/` |
| QueueName | `sqs/queue/` |
| InstanceId | `ec2/instance/` |

Repeated alarms deduplicate into the open incident (dedup key = account, region, alarm name).

## Getting telemetry in

Diagnosis jobs use the events and inventory stored for the incident:
- offline: `POST /api/offline/import`;
- real: run `python -m app.collectors.capture ...` (real-AWS mode, see `docs/aws-setup.md`) and
  post the events and inventory to the incident.

A job on an incident without events fails with "no telemetry stored".
