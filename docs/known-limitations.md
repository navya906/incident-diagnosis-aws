# Known limitations

Read this before using any number from `docs/experiments/` or any diagnosis the system shows.

## 1. Synthetic-data validity

- **Every reported number comes from synthetic incidents.** The 124 incidents of
  `synthetic-v1` come from one simulator whose fault signatures, noise and wording were written
  by the same people who wrote the detectors, the evidence weights, the rule baseline and the
  stub LLM's keyword rules. Results measure agreement with the simulator's assumptions
  (circularity), not accuracy on real AWS incidents.
- Ground-truth evidence is defined by the simulator (first occurrences of key signals, D23), so
  correct but unlisted evidence counts as a false positive.
- Every red herring sits on a resource with no dependency path to the alarm; an unrelated change
  on a connected resource (same RDS instance) is not covered.
- The historical corpus shares templates with the queries, so the 96% retrieval figure is an
  upper bound (D71), and the hashing embedder is lexical, not semantic.
- No incident uses the `other` label; compound (6) and insufficient-evidence (6) cases are few.
- The real collectors, the CloudFormation test stack and the fault-injection tool have been
  tested with moto and botocore Stubber only, **never against a real AWS account**.
- Mitigation: capture real incidents with the test stack (`infra/`, `docs/aws-setup.md`), write
  their ground truth by hand, and evaluate on them before trusting any conclusion.

## 2. Stub-LLM results are not diagnostic quality

All diagnoses in the repository, the demo and the Docker quickstart come from the LOCAL-ONLY
stub, a keyword matcher written against the simulator's wording (D75). Its accuracy, calibration
and zero hallucination rate are plumbing checks. No real-LLM experiment has been run (no keys in
the build environment); `docs/experiments/RUNBOOK.md` gives the commands and the results
template. Some conditions (A1, the knowledge-base conditions, `Full-no-redaction`) cannot differ
from Full with the stub: only a real model can show whether RAG helps or misleads and what
redaction costs.

## 3. Small-sample calibration caveats

- Calibration (ECE, Brier, reliability) is computed on 86 dev or 38 test incidents per
  condition, with 10 confidence bins. With so few incidents per bin, ECE is noisy and biased
  upward; differences between conditions of a few points are not meaningful.
- Self-consistency confidence uses few samples (5 by default, 2 in the demo), so it takes only a
  handful of values.
- Cluster bootstrap intervals use 11 fault-type clusters and are coarse. Only the pre-declared
  primary comparisons are Holm-corrected; the exploratory tables are not.
- Strata (red-herring, compound, insufficient-evidence) have 4 to 8 dev cases each; their
  per-stratum accuracies are anecdotes, not estimates.

## 4. CloudTrail lag and AWS data limits

- **CloudTrail delivers events with a delay (typically up to about 15 minutes).** A diagnosis
  started right after an alarm can miss the API call that caused it, for example the deployment
  or security-group change. The collector warns when the window ends inside the configured lag
  (`aws.cloudtrail_ingestion_lag_minutes: 15`) and does not cache such results (D34); re-run the
  capture and the diagnosis after the lag has passed.
- `LookupEvents` is limited to 2 requests per second per account and region, and covers
  management events only; data events (S3 object access, Lambda invokes) are not collected.
- AWS Config history is optional and missing when no recorder is configured.
- CloudWatch metric resolution depends on age (60 s up to 15 days, then 5 min, then 1 h); 5-minute
  data quantises the onset estimate to 5 minutes and roughly halves detector precision on dev.
- Dependencies are inferred from configuration (security-group rules, target groups,
  event-source mappings), not from traffic; SQS producers are not discovered; ECS services with
  the same name in different clusters collide (D36, D37).

## 5. LLM data-exposure risk

- With an external LLM or embedding provider, incident telemetry (log lines, API calls, metric
  names, resource names) is sent to that provider. Redaction replaces the identifier formats it
  knows (account ids, ARNs, IPs, principals, AWS hostnames, secrets, custom patterns) before the
  call, and aws mode makes it mandatory, but it is pattern-based: user names in free text,
  internal hostnames, customer data inside log messages and any format without a pattern are
  sent as they are. Resource names stay readable by default (`redaction.resource_names: false`).
- Check the provider's retention and training terms against your data policy before enabling
  it, add `custom_patterns` for your own identifiers, or use a self-hosted model through the
  OpenAI-compatible client.
- Telemetry text can contain instructions aimed at the model (prompt injection). The validator
  limits the damage (citations must exist, quoted numbers must match, severity is deterministic,
  nothing is executed), but the wording of a diagnosis can still be steered.
- The citation verifier is lexical: a false claim that reuses words of the cited line passes
  (4 of 86 injected cases in the verifier audit, `docs/experiments/phase7-verifier-audit.md`).
  Semantic support needs an LLM judge and human review.

## 6. Deployment and product limits

- One API process: diagnosis jobs and rate limits are in-process; several replicas need a
  shared queue and limiter. Restarting the API fails running jobs (they can be re-run).
- One role: every API key can do everything; no user accounts or per-user audit names
  (multi-user roles are out of scope until Phase 10 is done, BRIEF).
- No TLS inside the stack; terminate it in front of nginx.
- The API does not call AWS itself: real incidents need the capture CLI and the events and
  inventory endpoints.
- Timings use the incident's creation time, so imported historical incidents measure time since
  import, not since the original alarm (D93).
- Charts render every point (SVG); week-long windows of 1-minute data would need downsampling.
- Accessibility was considered (labels, roles, a text list of graph roles) but not audited.
- Token counts in the context budget are estimates (characters / 4).
