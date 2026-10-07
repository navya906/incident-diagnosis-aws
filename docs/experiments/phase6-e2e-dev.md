# Diagnosis engine end-to-end smoke run (dev split) — smoke-test / synthetic

> **SMOKE-TEST / SYNTHETIC.** Stub LLM (`stub-deterministic-v1`, LOCAL-ONLY) on simulator data. This shows that every stage runs and validates, not how well anything diagnoses. The stub's keyword rules were written against the simulator's wording, so its label agreement is **not a result** (DECISIONS D75).

- Dataset: `synthetic-v1`, content sha256 `06cec245b951be8e9b08524367439b5ee3d37fa75d6551d3bb8e2922d8388f8b`
- Prompt: `diag-v1` (sha `9df947fcdc3580a0`); context budget 6000 tokens; embedder `hashing-v1-384`; 3 self-consistency samples (Full only)
- Command: `python -m app.evaluation.diagnosis_e2e`

## Conditions

| condition | incidents | valid | repaired | rejected | mean_citations | unsupported_citation_rate | requires_review | historical_used | mean_context_tokens | items_dropped_by_budget | mean_self_consistency | stub_label_agreement_NOT_A_RESULT |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Full | 86 | 86 | 0 | 0 | 2.93 | 0.0 | 11 | 83 | 2406 | 0 | 0.984 | 0.965 |
| A1 no RAG | 86 | 86 | 0 | 0 | 2.93 | 0.0 | 11 | 0 | 1451 | 0 | None | 0.965 |
| A2 no graph | 86 | 86 | 0 | 0 | 2.91 | 0.0 | 14 | 80 | 1925 | 0 | None | 0.93 |
| A3 no anomaly detection | 86 | 86 | 0 | 0 | 2.87 | 0.0 | 5 | 82 | 2076 | 0 | None | 0.988 |
| A4 no chains | 86 | 86 | 0 | 0 | 2.74 | 0.0 | 20 | 70 | 1645 | 0 | None | 0.767 |

valid = passed the validator (schema, citations exist in the context, quoted values match, root-cause resource known, historical rules), possibly after the one repair. unsupported_citation_rate = cited ids that are unknown or misquoted / all cited ids (0 for every valid answer by construction; the validator rejects the rest).

## Severity (deterministic engine, Full)

- Levels: {'CRITICAL': 24, 'HIGH': 47, 'LOW': 2, 'MEDIUM': 13}
- The stub's advisory `severity_suggestion` equals the engine's level in 43 of 86 valid diagnoses (advisory only; the engine's level is the one reported).
- Factors without data (counts over incidents): {'error_rate': 1}
