# AWS setup for the real collectors (Phase 2)

The real collectors are **read-only**. They call 20 AWS API actions, all listed in
`infra/iam/collector-readonly-policy.json` (a test checks the list matches the code). Nothing in
this repository stores credentials; boto3 uses its standard credential chain.

## 1. Install the AWS extra

```bash
cd backend
pip install -e ".[dev,aws]"      # boto3 + moto (moto is only used by tests)
```

## 2. Create a read-only identity

Use a sandbox account. Create an IAM role (preferred, for SSO or an instance/task role) or a user,
and attach the policy:

```bash
aws iam create-policy --policy-name IncidentDiagnosisCollectorReadOnly \
  --policy-document file://infra/iam/collector-readonly-policy.json
aws iam attach-role-policy --role-name <your-role> \
  --policy-arn arn:aws:iam::<account-id>:policy/IncidentDiagnosisCollectorReadOnly
```

`Resource: "*"` is used because most of these Describe/List/Lookup actions do not support
resource-level permissions. All actions are read-only (a test rejects write verbs).

## 3. Point the app at it

Credentials: `aws sso login --profile <p>`, or environment variables, or an instance/task role.
Then either edit `config/default.yaml` (`aws.region`, `aws.profile`) or set environment variables:

```bash
export CLOUDDIAG_AWS__REGION=us-east-1
export CLOUDDIAG_AWS__PROFILE=incident-readonly
```

Never put keys in `config/default.yaml`, `.env.example`, or code. `.env` is git-ignored.

## 4. Capture a real incident

Real telemetry may only enter the system in real-AWS mode. Set `CLOUDDIAG_DATA_MODE=aws`; the
capture CLI refuses to run otherwise, and in this mode any external LLM or embedding client is
refused unless redaction is enabled, strict, and every built-in category (account ids, ARNs, IPs,
principals, secrets, hostnames) is on (fail closed, DECISIONS D68). Add organisation-specific
identifiers as `redaction.custom_patterns` in your config.

```bash
export CLOUDDIAG_DATA_MODE=aws
python -m app.collectors.capture \
  --seed-arn arn:aws:elasticloadbalancing:us-east-1:<acct>:loadbalancer/app/<name>/<id> \
  --start 2026-10-06T10:00:00Z --end 2026-10-06T11:30:00Z \
  --title "ALARM: 5xx on web ALB" --description "Users are receiving HTTP 5xx errors." \
  --out ../data/captured
```

Discovery starts at the seed ARN(s) and follows ALB -> target groups -> ECS services / EC2 /
Lambda -> IAM roles, Lambda -> SQS (event source mappings), and compute -> RDS where the RDS
security group admits the compute security group. The output is an `ObservableScenario` JSON file
with the same event schema as the synthetic dataset. Write its ground truth by hand.

## What each collector does

| Source | API | Notes |
|---|---|---|
| CloudWatch metrics | `GetMetricData` | up to 500 queries per call, paginated; period 60 s / 300 s / 3600 s by data age (CloudWatch retention); ALB per-target-group metrics are summed |
| CloudWatch Logs | `FilterLogEvents` | only the resources' own log groups; filter pattern for problem lines; capped per group (`logs_max_events_per_group`); messages scrubbed and truncated |
| CloudTrail | `LookupEvents` | rate limited to 2 req/s (the API limit), one lookup per resource name, de-duplicated by EventId; read-only events skipped by default; warns when the window ends inside the ~15 min ingestion lag |
| AWS Config | `GetResourceConfigHistory` | optional; skipped with a warning if there is no configuration recorder or the resource was not recorded |

All calls use botocore adaptive retries plus an outer exponential backoff with jitter for
sustained throttling. Sensitive keys (passwords, secrets, tokens, keys) are dropped from
CloudTrail request parameters and log text is scrubbed before it becomes an event. Identifier
redaction (account ids, ARNs, IPs) for LLM calls happens later, in Phase 5.

Set `aws.cache_dir` to cache collected events on disk (keyed by request and resource
fingerprint, TTL `aws.cache_ttl_seconds`). Results that produced warnings are not cached.

## Limits

- Not yet run against a real account from the build environment; tested with moto and botocore
  Stubber (moto does not implement CloudTrail `LookupEvents`, and its Config history output is
  malformed for some resource types).
- ECS service ids are `ecs/service/<name>`; two services with the same name in different clusters
  would collide (D36).
- `connects_to` edges are inferred from security-group rules, not observed traffic.
- SQS producers are not discovered (there is no API linking a queue to its senders).
