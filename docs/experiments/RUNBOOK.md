# Experiment runbook: real-LLM runs

This runbook gives the exact commands to run the evaluation (BRIEF Sections 3-4) with a real LLM
on the **test** split. Everything in `docs/experiments/` produced so far used the LOCAL-ONLY stub
LLM and is labelled `smoke-test / synthetic`. Do not report those numbers as results.

Rules (BRIEF Section 3 and DECISIONS D85-D90):
- Tune prompts, weights and thresholds on the **dev** split only. The test split is run once, at
  the end, with everything frozen. The runner refuses `split: test` without `--final`.
- Run **N >= 3** runs per condition on the test split (`runs: 3` in the test configs).
- Report bootstrap intervals and paired comparisons, not single numbers. The primary
  comparisons are declared in the config before the run (`primary_comparisons`, part of
  the experiment id) and Holm-corrected; everything else is exploratory.
- `Full-no-redaction` measures what redaction costs in diagnostic quality. It sends
  synthetic identifiers unredacted, is only allowed in offline mode, and the run fails
  closed in `data_mode: aws`.
- The incidents are synthetic. Label results `llm / synthetic incidents` (the test configs do)
  and state the synthetic-data limits next to any number.

All commands run from `backend/` with the project venv active.

## 0. Install and prepare data

```bash
pip install -e ".[dev,aws,rag,embeddings]"
python -m app.offline.generate            # data/generated/synthetic-v1 (seed 42)
python -m app.rag.corpus                  # data/generated/historical-v1 (seed 7)
```

`embeddings` installs sentence-transformers (PyTorch) for the configured embedder. The first run
downloads `all-MiniLM-L6-v2`; no incident data is sent anywhere for that.

## 1. Set credentials (environment only, never in a file)

Bash:

```bash
export CLOUDDIAG_LLM__API_KEY='<your key>'
# only for OpenAI-compatible gateways or local servers (vLLM, Ollama):
export CLOUDDIAG_LLM__BASE_URL='https://<gateway>/v1'
```

PowerShell:

```powershell
$env:CLOUDDIAG_LLM__API_KEY = '<your key>'
$env:CLOUDDIAG_LLM__BASE_URL = 'https://<gateway>/v1'
```

Redaction is on by default and applies to every external call. External clients can only be
created through the redacting factory (DECISIONS D67).

## 2. Check the pipeline with the stub (no key needed)

```bash
python -m app.experiments run --config ../experiments/smoke-dev.yaml
python -m app.experiments verify <experiment id printed above>
```

`verify` re-runs the stored config and must print `"reproduced": true`. The stub is
deterministic, so its results are byte-identical.

## 3. Pilot on dev (real model, small, cheap)

Edit `experiments/real-dev-pilot.yaml`: set `settings.llm.model` to an exact, dated model version
and set the prices (`input_cost_per_1k`, `output_cost_per_1k`) from the provider's current price
list. Then:

```bash
python -m app.experiments run --config ../experiments/real-dev-pilot.yaml
```

Check `docs/experiments/runs/<id>/report.md`:
- `rejected` should be close to 0. If many answers are rejected, read the `failure` fields in
  `results.jsonl` and fix the prompt **on dev**. Then bump `PROMPT_VERSION` in
  `app/diagnosis/prompts.py`, which changes the experiment id.
- `manifest.json` -> `totals` gives tokens and cost. Use them to estimate the final run.

Cost estimate for the final run: print the plan before running anything.

```bash
python -m app.experiments plan --config ../experiments/real-test-openai.yaml
python -m app.experiments plan --config ../experiments/real-test-gemini.yaml
```

The test configs use the cheaper plan (DECISIONS D86):
- N=3 runs for the baselines (B1-B4) and Full;
- N=1 for the ablations (A1-A5, the two knowledge-base conditions, Full without redaction)
  and the RQ6 sweeps;
- self-consistency (5 samples) only for Full and B4, the conditions whose calibration is
  reported.

Calls per test incident: Full 3 x (1 + 5) = 18, B4 18, B2 3, B3 3, B1 0, eight ablations 1
each, twelve sweeps 1 each, giving 62. With the 38 test incidents that is **2,356 LLM calls**
(the earlier plan, with N=3 and self-consistency everywhere, needed 16,416).

At the smoke run's averages (about 6.5k input and 1.3k output tokens per call, including the
system prompt) that is about **15.3M input and 3.1M output tokens**. At the example prices in
the configs:
- about **$69** for `gpt-4o-2024-08-06` ($2.50 / $10 per 1M tokens);
- about **$34** for `gemini-1.5-pro-002` ($1.25 / $5 per 1M).

Repairs add roughly 0-5%. Re-run `plan` with `--input-tokens/--output-tokens` from your
pilot's `manifest.json` totals, and with the provider's current prices, before the final run.
The dev pilot itself is about 110 calls (well under $1 at the example prices).

## 4. Final run on the test split

Freeze first:
- Commit every config and code change.
- Make sure `git status` is clean. The manifest records the commit and a hash of the application
  source.
- Pin an exact model version in the config.

OpenAI-compatible:

```bash
python -m app.experiments run --config ../experiments/real-test-openai.yaml --final --db
```

Google Gemini:

```bash
python -m app.experiments run --config ../experiments/real-test-gemini.yaml --final --db
```

`--final` is required for the test split. `--db` also stores the run in the database configured
by `CLOUDDIAG_DATABASE_URL` (tables `evaluation_runs` and `experiment_results`). Leave it out to
write files only.

Each run writes `docs/experiments/runs/<experiment id>/`:

| file | content |
|---|---|
| `manifest.json` | id, label, split, timestamps, configured model and provider-reported versions, prompt version and hash, dataset/corpus versions and hashes, config, effective settings (no secrets), source hash, git commit, library versions, totals, results hash |
| `summary.json`, `summary.csv` | per condition: accuracy, root-cause match, top-3, evidence P/R, context recall, hallucination rate, rubric, rejected, ECE/Brier (verbalised and self-consistency), tokens, cost, latency, with 95% bootstrap intervals |
| `comparisons.json`, `comparisons.csv` | paired comparisons over incidents: difference, incident and cluster (fault-type) bootstrap intervals and p-values, exact McNemar p; the pre-declared primary comparisons carry Holm-adjusted p (`holm_p`) |
| `strata.json`, `strata.csv` | accuracy, root-cause match, evidence recall and validity per condition and case type (clean, red-herring, compound, insufficient-evidence) |
| `results.jsonl` | one record per condition x incident x run: retrieved evidence, diagnosis, ground truth, metrics (git-ignored: large) |
| `results.csv` | the per-record metrics as a flat table |
| `report.md` | the tables above |
| `spot_check.csv`, `judge_prompt.txt` | human review sample and LLM-judge rubric (step 6) |

## 5. Reproducibility with a real model

The experiment id is a hash of the config, dataset, corpus, prompt, model name, embedder and
application source. The same id means the same inputs and code. Real LLMs are not bit-exactly
deterministic, even at temperature 0 with a seed. So `verify` on a real-model run can report
`"reproduced": false` with the problem `results differ` while `experiment_id` still matches.
That is expected: it confirms the inputs and code were identical. Keep the original
`results.jsonl` (archive the run directory) as the record of the reported numbers.

```bash
python -m app.experiments verify <experiment id>
python -m app.experiments list
```

## 6. Recommendation quality: LLM judge and human spot check

`rubric` in the summary is a deterministic proxy (`rubric-v1`, DECISIONS D81). For the reported
recommendation quality:
1. Open `spot_check.csv`. It holds a seeded 10% sample of Full diagnoses with the reference root
   cause and resolution. Score each row 0-4 in `human_score_0_4`, using the four criteria in
   `judge_prompt.txt`.
2. Optionally score all Full diagnoses with an LLM judge using `judge_prompt.txt`. **The judge
   must be a different model family from the diagnosing model** (`judge` block in the config;
   a config with the same family is rejected, DECISIONS D88). The OpenAI test config names a
   Gemini judge, and the Gemini config names an OpenAI judge. Report judge vs human agreement
   on the
   spot-check rows before using judge scores.

## 7. Results template

Fill in from `summary.csv` and `comparisons.csv`. Keep the label and the experiment id.

```text
Experiment: <exp id>   Model: <provider-reported version>   Prompt: diag-v1 (<sha>)
Split: test (38 incidents, synthetic), runs per condition: 3, label: llm / synthetic incidents

| Condition | Accuracy [95% CI] | Root cause [95% CI] | Evidence recall | Halluc. rate | ECE (verb.) | ECE (SC) | Tokens/diag | Cost/diag |
|-----------|-------------------|---------------------|-----------------|--------------|-------------|----------|-------------|-----------|
| B1        |                   |                     |                 |              |             |          |             |           |
| B2        |                   |                     |                 |              |             |          |             |           |
| B3        |                   |                     |                 |              |             |          |             |           |
| B4        |                   |                     |                 |              |             |          |             |           |
| Full      |                   |                     |                 |              |             |          |             |           |
| A1 ... A5 |                   |                     |                 |              |             |          |             |           |
| Full-KB-fault-removed / Full-KB-distractors |  |                     |                 |              |             |          |             |           |

Primary comparisons (pre-declared, root-cause match, cluster bootstrap, Holm-corrected):
  B2 vs Full: diff <+/-x.xxx> [cluster CI], Holm p = <p>   (likewise B3, B4, A1, A2, A5)
Exploratory (unadjusted): A3, A4, knowledge-base conditions, Full-no-redaction, RQ6 sweeps
By case type (Full): clean <acc>, red-herring <acc>, compound <acc>, insufficient-evidence <acc>
Human spot check: n = <n>, mean human score <x.x>/4, judge-human agreement <kappa or %>
Caveats: synthetic incidents; small test set (38) and few fault types (clusters), so
cluster intervals are wide; calibration on few samples; only the primary family is
Holm-corrected.
```

## Troubleshooting

- `RedactionPolicyError`: you are in `data_mode: aws` with redaction weakened. Restore the
  redaction settings. Real AWS data may only be sent with the full policy (DECISIONS D68).
- `RedactionError`: strict mode found a raw identifier that redaction could not replace. Add a
  `redaction.custom_patterns` entry for that format. Do not turn strict mode off.
- HTTP 429 or 5xx: the clients retry `llm.max_retries` times with backoff. Lower concurrency
  elsewhere or raise the retries.
- Many rejections: inspect the `failure.errors` field in `results.jsonl`. Fix the prompt on dev,
  bump the prompt version, and re-run the pilot.
