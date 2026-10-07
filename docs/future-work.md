# Future work

Ordered by what most limits the conclusions today. Items 1-3 decide whether the research
question can be answered at all; the stretch features (BRIEF, "Out of scope") come after them.

## 1. Real-LLM experiments on the test split

Run the frozen condition matrix with at least two model families, N >= 3 runs per condition,
on the test split, following `docs/experiments/RUNBOOK.md` (configs
`experiments/real-test-openai.yaml`, `real-test-gemini.yaml`; a cheaper dev pilot first,
`real-dev-pilot.yaml`). Then:

- report Full vs B1-B4 and A1-A5 with bootstrap intervals and the pre-declared, Holm-corrected
  primary comparisons;
- the knowledge-base conditions (fault type removed, distractors) to see whether the model
  follows misleading history, and `Full-no-redaction` for the cost of redaction;
- the LLM-judge rubric for root-cause text and recommendations (judge from a different model
  family, D88) and the human spot check of `spot_check.csv`;
- calibration of verbalised vs self-consistency confidence.

## 2. Real incidents

- Deploy the test stack (`infra/test-stack/template.yaml`), inject each fault with
  `app.offline.fault_injection`, capture the incidents with `app.collectors.capture`, and write
  their ground truth by hand. Even 10-20 real incidents would test the simulator's assumptions
  (detector thresholds, evidence weights, onset estimate) that every current number depends on.
- Re-check the dev-tuned settings (D55) on captured incidents before freezing anything.
- Add red herrings on connected resources and incidents with the `other` label.

## 3. Better evidence and retrieval components

- Semantic evidence scoring with an embedding model instead of TF-IDF (D53), re-tuned on dev.
- SentenceTransformers (or another semantic embedder) for retrieval, measured against the
  hashing baseline, on a historical corpus that does not share the simulator's templates.
- Multivariate anomaly detection (cross-metric, cross-resource) and seasonality-aware
  baselines.
- Traffic-based dependencies (VPC flow logs, X-Ray) to complement configuration-based edges.

## 4. Stronger verification

- Semantic citation checking (entailment model or LLM judge) on top of the lexical verifier,
  which misses false claims that reuse words of the cited line.
- Checks for times and identifiers in explanations, not only numbers.
- A prompt-injection test set built from adversarial log lines.

## 5. Operations

- Shared job queue and rate limiter (e.g. Redis) so the API can run as several replicas.
- Collect telemetry from the API (call the collectors when an alarm arrives, after the
  CloudTrail lag) instead of the separate capture CLI.
- Metrics and traces for the service itself (job durations, LLM latency, token spend).
- Downsampling for long chart windows; an accessibility audit of the UI.

## 6. Stretch features (BRIEF order, after Phase 10)

1. Conversational investigation assistant that answers only from stored incident context and
   cites every claim.
2. Graph database backend behind the `GraphStore` interface.
3. Automated remediation that requires explicit human approval and is never destructive by
   default.
4. Multi-user accounts and roles.
