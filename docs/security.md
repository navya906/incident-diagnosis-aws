# Security

What the system protects, how, and what it does not cover. Tests named here are in
`backend/tests/` unless stated otherwise.

## Assets and threats

| Asset | Threat | Main controls |
|---|---|---|
| Telemetry of a real AWS account (account ids, ARNs, IPs, principals, log text) | disclosure to an external LLM or embedding provider | redaction before every external call, fail-closed policy in aws mode |
| Secrets inside telemetry (passwords, tokens, keys in logs) | disclosure via LLM, API responses, logs | secret redaction (never restored), secret filtering in responses, log scrubbing |
| The API and its data | unauthorised access, key guessing, abuse | API keys, constant-time comparison, rate limits, audit log |
| Incident creation via alarms | forged alarms | SNS signature verification, topic allowlist, key on EventBridge |
| The AWS account itself | the collectors changing anything | read-only IAM policy, no write calls anywhere |
| Diagnoses | fabricated or unsupported conclusions shown as fact | citation verifier, evidence rule in the UI, advisory severity |
| Containers | privilege escalation | non-root users, read-only root filesystem and dropped capabilities in production |

## Data sent to LLM and embedding providers

- External clients exist only behind `create_llm_client` / `create_embedder`, which return
  them wrapped in redaction; constructing them directly raises (D67; a test imports every
  module and checks every client class).
- Redacted: account ids, ARNs (structure kept), IPv4/IPv6, principals (IAM unique ids, access
  key ids, e-mails, identity fields), AWS hostnames, secrets (key=value, bearer/basic tokens,
  JWTs, PEM keys, URL passwords, bare 40-character keys), plus `redaction.custom_patterns`.
- Consistent pseudonyms per incident; secrets are never mapped back. Strict mode refuses to
  send text that still contains a replaced value.
- `data_mode: aws` makes redaction mandatory: the factory and the capture CLI refuse to run if
  redaction is disabled, not strict, or a category is off (D68).
- Tests: a recording fake provider receives no raw identifier for 16 identifier kinds and for
  real dataset incidents; every dataset incident serialised for a prompt passes the strict check
  (`test_redaction_rag.py`, `test_redaction_hardening.py`).
- **Residual risk:** redaction is pattern-based. Identifiers in unknown formats (a bare user
  name in free text, an internal hostname outside `*.amazonaws.com`) are sent unless covered by
  a custom pattern. Resource names stay readable by default. Use a provider and contract that
  fit your data policy, or a local model through the OpenAI-compatible client.

## API

- **Keys** (`app/api/security.py`): from the environment only, compared in constant time; sent
  as `X-API-Key` or as the HTTP Basic password; no keys = every `/api` request refused.
  Production and aws mode refuse the demo key, keys under 16 characters and disabled auth (D108).
- **Rate limiting:** token bucket per key, per client address for requests without a valid key
  (limits key guessing); 429 with `Retry-After`.
- **Client address:** the bundled nginx overwrites `X-Forwarded-For` with the connecting
  address; the API trusts it only from `FORWARDED_ALLOW_IPS` (production compose: the nginx
  container; the API port is not published).
- **Input validation:** strict models (unknown fields rejected), body size limit, event batch
  limit, canonical resource ids, bounded windows.
- **Errors never echo input:** validation errors return location and message only; unhandled
  errors return `internal error` and a request id (D96).
- **Audit:** every change and every denied request (401, 403, 413, 429) with actor = key hash
  or client address, never the key.
- **Logs:** configured keys, bearer/basic tokens and `x-api-key` values are scrubbed from every
  log line (D94).
- Tests: `test_api.py` (every route 401 without a key, rate limits, body limits, no secret in
  logs, responses or job errors, SNS signatures, production key rules) and
  `test_e2e_http.py` (the same over the wire against a running server).

## Alarm webhook

SNS messages are verified (SignatureVersion 1 and 2) with certificates fetched only from
`https://sns.<region>.amazonaws.com/`; topics can be allowlisted; subscription confirmations are
verified but never auto-confirmed (no outbound request triggered by a request). EventBridge
API destinations authenticate with the API key (D95).

## AWS access

`infra/iam/collector-readonly-policy.json`: 20 read-only actions; a test checks that it covers
every call the collectors make and contains no write verbs (D39). No credentials in the
repository (a scan test). The fault-injection tool is a separate, dry-run-by-default script for
a test stack only (D26).

## Browser

- The API key lives in `sessionStorage` (per tab, cleared when the tab closes), never in
  `localStorage` or a URL (component and Playwright tests).
- nginx sends a strict Content Security Policy (`default-src 'self'`, no inline scripts, no
  framing), `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`
  and a restrictive `Permissions-Policy`. Same-origin API: no CORS.
- The UI never renders model output as HTML (React text nodes only).

## Containers

- API: `python:3.11-slim`, user `app` (uid 10001); production: read-only root filesystem,
  `/tmp` as tmpfs, all capabilities dropped, `no-new-privileges`.
- Frontend: `nginx-unprivileged` (uid 101, port 8080), same production restrictions.
- Database: only on the compose network in production; password required (`POSTGRES_PASSWORD`).
- `.dockerignore` keeps `.env`, virtual environments, generated data and git metadata out of
  images.

## Not covered

- TLS: terminate it in front of nginx (load balancer or reverse proxy); see
  `docs/deployment.md`.
- One role: every key can do everything; no per-user accounts (out of scope, BRIEF).
- Key rotation is by configuration change and restart.
- No malware or dependency scanning in CI; pin and scan images in your own pipeline.
- Prompt injection through telemetry text (for example a log line that addresses the model) is
  limited by the validator, not prevented: a manipulated answer still has to cite evidence that
  exists, and its severity and actions are not acted on, but its wording can be wrong.

## Reporting a vulnerability

Do not open a public issue. Contact the repository owner privately (GitHub security advisory
on `navya906/incident-diagnosis-aws`).
