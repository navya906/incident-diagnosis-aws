# Verifier audit: injected faults (dev split) — smoke-test / synthetic

> **SMOKE-TEST / SYNTHETIC.** Faults injected into the stub's valid answers on simulator data. This measures the validator's coverage of each fault type, not a model's error rate.

- Dataset: `synthetic-v1` (sha `06cec245b951be8e9b08524367439b5ee3d37fa75d6551d3bb8e2922d8388f8b`)
- Command: `python -m app.evaluation.verifier_audit`
- Report id: `rpt-fa410c0154c9` (content hash of the .json results; `python -m app.evaluation.provenance check`)
- Clean answers: 86; false positives (a clean answer rejected or flagged): 0 (rate 0.0)

detected = rejected by the validator, or counted as an unsupported citation in the hallucination rate. strict = rejected with `reject_unsupported_claims: true`.

| fault | injected | detected | detection rate | rejected (strict) | rejection rate (strict) |
|---|---|---|---|---|---|
| fabricated_id | 86 | 86 | 1.0 | 86 | 1.0 |
| historical_id | 86 | 86 | 1.0 | 86 | 1.0 |
| wrong_value | 86 | 86 | 1.0 | 86 | 1.0 |
| unsupported_claim | 86 | 82 | 0.9535 | 82 | 0.9535 |
| wrong_resource | 84 | 84 | 1.0 | 84 | 1.0 |
| uncited_conclusion | 84 | 84 | 1.0 | 84 | 1.0 |

## Limits

- `unsupported_claim` is caught lexically (no shared content word with the cited line). A false claim that reuses words from the cited line passes; a claim about the same event in different words would also be flagged. Semantic support needs the LLM judge.
- Numbers below 10 and times are not value-checked; a wrong small count passes.
