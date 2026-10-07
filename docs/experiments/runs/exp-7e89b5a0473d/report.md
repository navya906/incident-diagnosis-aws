# Experiment exp-7e89b5a0473d (smoke-dev, dev split) — smoke-test / synthetic

> **SMOKE-TEST / SYNTHETIC.** Stub LLM (LOCAL-ONLY) on simulator data. The numbers show that the matrix runs end to end and is reproducible; they say nothing about real diagnostic quality. The stub and B1 share keyword rules written against the simulator's wording (DECISIONS D75, D79).

- Model: `stub` / `stub-deterministic-v1`; reported versions: stub-deterministic-v1
- Prompt `diag-v1` (sha `9df947fcdc3580a0`); rubric `rubric-v1`; embedder `hashing-v1-384`
- Dataset `synthetic-v1` (sha `06cec245b951be8e9b08524367439b5ee3d37fa75d6551d3bb8e2922d8388f8b`); corpus `historical-v1` (sha `abb3f72be3902e72`)
- 86 incidents x 25 conditions; runs: 1 (baselines, Full), 1 (ablations), 1 (sweeps) = 2150 diagnoses; source `deb708868b004d3e`; results sha256 `a5fde9474b271723...`
- Reproduce: `python -m app.experiments verify exp-7e89b5a0473d`

Accuracy = taxonomy label; root cause = label and resource; top-3 = label among the root cause and the first two alternatives. Evidence precision/recall compare cited events with ground-truth evidence; context recall = ground-truth evidence shown to the model. Hallucination rate = unsupported citations in first answers / citations in first answers. Brackets: 95% bootstrap interval over incidents; `cl.` = cluster bootstrap that resamples whole fault types (incidents of one fault type are correlated). ECE uses 10 bins; with fewer than a few hundred diagnoses per condition it is noisy.

## Primary comparisons (pre-declared, Holm-corrected)

Paired over the same incidents on the primary metric; p-values from the cluster bootstrap, Holm-corrected across this family. Everything below this table is exploratory and unadjusted.

| comparison | metric | n (clusters) | diff | 95% CI (cluster) | cluster p | Holm p |
|---|---|---|---|---|---|---|
| B2 vs Full | root_cause_correct | 86 (11) | -0.849 | [-0.977, -0.657] | 0.000 | 0.000 |
| B3 vs Full | root_cause_correct | 86 (11) | -0.721 | [-0.942, -0.431] | 0.000 | 0.000 |
| B4 vs Full | root_cause_correct | 86 (11) | -0.116 | [-0.214, -0.033] | 0.002 | 0.008 |
| A1 vs Full | root_cause_correct | 86 (11) | +0.000 | [+0.000, +0.000] | 1.000 | 1.000 |
| A2 vs Full | root_cause_correct | 86 (11) | -0.116 | [-0.214, -0.033] | 0.002 | 0.008 |
| A5 vs Full | root_cause_correct | 86 (11) | +0.070 | [+0.000, +0.172] | 0.188 | 0.376 |

## Conditions

| condition | accuracy | root cause | top-3 | evid. P | evid. R | context R | halluc. | rubric | rejected | ECE verb. | ECE SC | tokens (in/out) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| B1 | 0.686 [0.593, 0.779] cl.[+0.532, +0.839] | 0.674 [0.581, 0.767] cl.[+0.518, +0.833] | 0.954 | 0.584 | 0.263 | n/a | 0.000 | 0.613 | 0 | 0.142 | n/a | 0.000/0.000 |
| B2 | 0.046 [0.012, 0.093] cl.[+0.000, +0.158] | 0.046 [0.012, 0.093] cl.[+0.000, +0.158] | 0.046 | 0.000 | 0.000 | 0.000 | 0.000 | 0.262 | 0 | 0.254 | n/a | 915.430/287.674 |
| B3 | 0.186 [0.105, 0.268] cl.[+0.022, +0.416] | 0.174 [0.105, 0.256] cl.[+0.000, +0.405] | 0.756 | 0.023 | 0.009 | 0.524 | 0.000 | 0.776 | 0 | 0.642 | n/a | 6407.395/407.209 |
| B4 | 0.930 [0.872, 0.977] cl.[+0.867, +0.978] | 0.779 [0.686, 0.861] cl.[+0.605, +0.915] | 0.977 | 0.636 | 0.300 | 0.502 | 0.000 | 0.968 | 0 | 0.154 | 0.052 | 5123.407/1405.244 |
| Full | 0.965 [0.919, 1.000] cl.[+0.904, +1.000] | 0.895 [0.826, 0.954] cl.[+0.789, +0.977] | 0.977 | 0.779 | 0.372 | 0.601 | 0.000 | 0.980 | 0 | 0.161 | 0.046 | 9406.186/1501.837 |
| A1 | 0.965 [0.919, 1.000] cl.[+0.904, +1.000] | 0.895 [0.826, 0.954] cl.[+0.789, +0.977] | 0.977 | 0.779 | 0.372 | 0.601 | 0.000 | 0.980 | 0 | 0.161 | n/a | 2182.093/467.779 |
| A2 | 0.930 [0.872, 0.977] cl.[+0.867, +0.978] | 0.779 [0.686, 0.861] cl.[+0.605, +0.915] | 0.977 | 0.636 | 0.300 | 0.502 | 0.000 | 0.968 | 0 | 0.154 | n/a | 2660.756/501.407 |
| A3 | 0.977 [0.942, 1.000] cl.[+0.946, +1.000] | 0.942 [0.884, 0.988] cl.[+0.892, +0.988] | 1.000 | 0.649 | 0.281 | 0.263 | 0.000 | 0.974 | 0 | 0.136 | n/a | 2811.046/457.814 |
| A4 | 0.779 [0.686, 0.861] cl.[+0.565, +0.956] | 0.698 [0.605, 0.791] cl.[+0.474, +0.909] | 0.965 | 0.626 | 0.305 | 0.600 | 0.000 | 0.933 | 0 | 0.115 | n/a | 2391.454/465.046 |
| A5 | 0.977 [0.942, 1.000] cl.[+0.921, +1.000] | 0.965 [0.919, 1.000] cl.[+0.907, +1.000] | 0.977 | 0.717 | 0.305 | 0.014 | 0.000 | 0.980 | 0 | 0.177 | n/a | 3154.942/506.198 |
| Full-KB-fault-removed | 0.965 [0.919, 1.000] cl.[+0.904, +1.000] | 0.895 [0.826, 0.954] cl.[+0.789, +0.977] | 0.977 | 0.779 | 0.372 | 0.601 | 0.000 | 0.980 | 0 | 0.161 | n/a | 3189.860/468.581 |
| Full-KB-distractors | 0.965 [0.919, 1.000] cl.[+0.904, +1.000] | 0.895 [0.826, 0.954] cl.[+0.789, +0.977] | 0.977 | 0.779 | 0.372 | 0.601 | 0.000 | 0.980 | 0 | 0.161 | n/a | 3080.058/500.081 |
| Full-no-redaction | 0.965 [0.919, 1.000] cl.[+0.904, +1.000] | 0.895 [0.826, 0.954] cl.[+0.789, +0.977] | 0.977 | 0.779 | 0.372 | 0.601 | 0.000 | 0.980 | 0 | 0.161 | n/a | 3135.395/500.884 |
| RQ6-K5 | 0.988 [0.965, 1.000] cl.[+0.961, +1.000] | 0.942 [0.884, 0.988] cl.[+0.865, +1.000] | 0.988 | 0.758 | 0.333 | 0.282 | 0.000 | 0.977 | 0 | 0.178 | n/a | 3015.000/488.139 |
| RQ6-K10 | 0.965 [0.919, 1.000] cl.[+0.904, +1.000] | 0.895 [0.826, 0.954] cl.[+0.789, +0.977] | 0.977 | 0.779 | 0.372 | 0.601 | 0.000 | 0.980 | 0 | 0.161 | n/a | 3135.395/500.884 |
| RQ6-K20 | 0.977 [0.942, 1.000] cl.[+0.921, +1.000] | 0.872 [0.802, 0.942] cl.[+0.726, +0.977] | 0.977 | 0.756 | 0.367 | 0.782 | 0.000 | 0.980 | 0 | 0.179 | n/a | 3336.558/512.698 |
| RQ6-K40 | 0.965 [0.930, 1.000] cl.[+0.907, +1.000] | 0.837 [0.756, 0.919] cl.[+0.692, +0.954] | 0.977 | 0.713 | 0.356 | 0.818 | 0.000 | 0.977 | 0 | 0.194 | n/a | 3764.977/535.767 |
| RQ6-W5 | 0.814 [0.733, 0.895] cl.[+0.667, +0.942] | 0.686 [0.593, 0.779] cl.[+0.506, +0.872] | 0.942 | 0.545 | 0.259 | 0.473 | 0.000 | 0.948 | 0 | 0.136 | n/a | 2863.500/475.558 |
| RQ6-W15 | 0.965 [0.919, 1.000] cl.[+0.904, +1.000] | 0.884 [0.814, 0.942] cl.[+0.758, +0.977] | 0.977 | 0.787 | 0.376 | 0.603 | 0.000 | 0.980 | 0 | 0.157 | n/a | 3103.000/500.512 |
| RQ6-W30 | 0.965 [0.919, 1.000] cl.[+0.904, +1.000] | 0.895 [0.826, 0.954] cl.[+0.789, +0.977] | 0.977 | 0.779 | 0.372 | 0.601 | 0.000 | 0.980 | 0 | 0.161 | n/a | 3135.395/500.884 |
| RQ6-W60 | 0.965 [0.919, 1.000] cl.[+0.904, +1.000] | 0.895 [0.826, 0.954] cl.[+0.789, +0.977] | 0.977 | 0.783 | 0.374 | 0.601 | 0.000 | 0.980 | 0 | 0.168 | n/a | 3212.000/504.419 |
| RQ6-Emetrics | 0.337 [0.244, 0.442] cl.[+0.105, +0.586] | 0.186 [0.105, 0.267] cl.[+0.043, +0.360] | 0.360 | 0.172 | 0.072 | 0.287 | 0.000 | 0.657 | 0 | 0.276 | n/a | 2689.767/388.826 |
| RQ6-E+cloudtrail | 0.651 [0.546, 0.744] cl.[+0.448, +0.819] | 0.581 [0.477, 0.686] cl.[+0.389, +0.742] | 0.733 | 0.496 | 0.137 | 0.343 | 0.000 | 0.811 | 0 | 0.136 | n/a | 2821.151/423.454 |
| RQ6-E+logs | 0.965 [0.919, 1.000] cl.[+0.904, +1.000] | 0.895 [0.826, 0.954] cl.[+0.789, +0.977] | 0.977 | 0.752 | 0.360 | 0.576 | 0.000 | 0.980 | 0 | 0.170 | n/a | 3084.930/503.628 |
| RQ6-E+config | 0.965 [0.919, 1.000] cl.[+0.904, +1.000] | 0.895 [0.826, 0.954] cl.[+0.789, +0.977] | 0.977 | 0.779 | 0.372 | 0.601 | 0.000 | 0.980 | 0 | 0.161 | n/a | 3135.395/500.884 |

## By case type

| condition | case type | n | accuracy | root cause | evid. R | valid |
|---|---|---|---|---|---|---|
| B1 | clean | 70 | 0.729 | 0.729 | 0.265 | 1.000 |
| B1 | red-herring | 8 | 0.750 | 0.625 | 0.301 | 1.000 |
| B1 | compound | 4 | 0.250 | 0.250 | 0.146 | 1.000 |
| B1 | insufficient-evidence | 4 | 0.250 | 0.250 | n/a | 1.000 |
| B2 | clean | 70 | 0.000 | 0.000 | 0.000 | 1.000 |
| B2 | red-herring | 8 | 0.000 | 0.000 | 0.000 | 1.000 |
| B2 | compound | 4 | 0.000 | 0.000 | 0.000 | 1.000 |
| B2 | insufficient-evidence | 4 | 1.000 | 1.000 | n/a | 1.000 |
| B3 | clean | 70 | 0.129 | 0.114 | 0.008 | 1.000 |
| B3 | red-herring | 8 | 0.375 | 0.375 | 0.025 | 1.000 |
| B3 | compound | 4 | 0.000 | 0.000 | 0.000 | 1.000 |
| B3 | insufficient-evidence | 4 | 1.000 | 1.000 | n/a | 1.000 |
| B4 | clean | 70 | 0.971 | 0.814 | 0.304 | 1.000 |
| B4 | red-herring | 8 | 1.000 | 0.750 | 0.323 | 1.000 |
| B4 | compound | 4 | 0.500 | 0.500 | 0.176 | 1.000 |
| B4 | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| Full | clean | 70 | 1.000 | 0.943 | 0.384 | 1.000 |
| Full | red-herring | 8 | 1.000 | 0.750 | 0.348 | 1.000 |
| Full | compound | 4 | 0.750 | 0.750 | 0.209 | 1.000 |
| Full | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| A1 | clean | 70 | 1.000 | 0.943 | 0.384 | 1.000 |
| A1 | red-herring | 8 | 1.000 | 0.750 | 0.348 | 1.000 |
| A1 | compound | 4 | 0.750 | 0.750 | 0.209 | 1.000 |
| A1 | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| A2 | clean | 70 | 0.971 | 0.814 | 0.304 | 1.000 |
| A2 | red-herring | 8 | 1.000 | 0.750 | 0.323 | 1.000 |
| A2 | compound | 4 | 0.500 | 0.500 | 0.176 | 1.000 |
| A2 | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| A3 | clean | 70 | 1.000 | 0.971 | 0.299 | 1.000 |
| A3 | red-herring | 8 | 1.000 | 0.875 | 0.186 | 1.000 |
| A3 | compound | 4 | 0.500 | 0.500 | 0.160 | 1.000 |
| A3 | insufficient-evidence | 4 | 1.000 | 1.000 | n/a | 1.000 |
| A4 | clean | 70 | 0.800 | 0.729 | 0.312 | 1.000 |
| A4 | red-herring | 8 | 0.875 | 0.750 | 0.332 | 1.000 |
| A4 | compound | 4 | 0.500 | 0.250 | 0.144 | 1.000 |
| A4 | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| A5 | clean | 70 | 1.000 | 1.000 | 0.314 | 1.000 |
| A5 | red-herring | 8 | 1.000 | 0.875 | 0.291 | 1.000 |
| A5 | compound | 4 | 1.000 | 1.000 | 0.170 | 1.000 |
| A5 | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| Full-KB-fault-removed | clean | 70 | 1.000 | 0.943 | 0.384 | 1.000 |
| Full-KB-fault-removed | red-herring | 8 | 1.000 | 0.750 | 0.348 | 1.000 |
| Full-KB-fault-removed | compound | 4 | 0.750 | 0.750 | 0.209 | 1.000 |
| Full-KB-fault-removed | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| Full-KB-distractors | clean | 70 | 1.000 | 0.943 | 0.384 | 1.000 |
| Full-KB-distractors | red-herring | 8 | 1.000 | 0.750 | 0.348 | 1.000 |
| Full-KB-distractors | compound | 4 | 0.750 | 0.750 | 0.209 | 1.000 |
| Full-KB-distractors | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| Full-no-redaction | clean | 70 | 1.000 | 0.943 | 0.384 | 1.000 |
| Full-no-redaction | red-herring | 8 | 1.000 | 0.750 | 0.348 | 1.000 |
| Full-no-redaction | compound | 4 | 0.750 | 0.750 | 0.209 | 1.000 |
| Full-no-redaction | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| RQ6-K5 | clean | 70 | 1.000 | 0.957 | 0.350 | 1.000 |
| RQ6-K5 | red-herring | 8 | 1.000 | 0.875 | 0.270 | 1.000 |
| RQ6-K5 | compound | 4 | 1.000 | 1.000 | 0.170 | 1.000 |
| RQ6-K5 | insufficient-evidence | 4 | 0.750 | 0.750 | n/a | 1.000 |
| RQ6-K10 | clean | 70 | 1.000 | 0.943 | 0.384 | 1.000 |
| RQ6-K10 | red-herring | 8 | 1.000 | 0.750 | 0.348 | 1.000 |
| RQ6-K10 | compound | 4 | 0.750 | 0.750 | 0.209 | 1.000 |
| RQ6-K10 | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| RQ6-K20 | clean | 70 | 1.000 | 0.914 | 0.382 | 1.000 |
| RQ6-K20 | red-herring | 8 | 1.000 | 0.750 | 0.323 | 1.000 |
| RQ6-K20 | compound | 4 | 1.000 | 0.750 | 0.186 | 1.000 |
| RQ6-K20 | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| RQ6-K40 | clean | 70 | 1.000 | 0.886 | 0.375 | 1.000 |
| RQ6-K40 | red-herring | 8 | 1.000 | 0.750 | 0.310 | 1.000 |
| RQ6-K40 | compound | 4 | 0.750 | 0.500 | 0.122 | 1.000 |
| RQ6-K40 | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| RQ6-W5 | clean | 70 | 0.857 | 0.729 | 0.266 | 1.000 |
| RQ6-W5 | red-herring | 8 | 1.000 | 0.750 | 0.244 | 1.000 |
| RQ6-W5 | compound | 4 | 0.000 | 0.000 | 0.176 | 1.000 |
| RQ6-W5 | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| RQ6-W15 | clean | 70 | 1.000 | 0.929 | 0.388 | 1.000 |
| RQ6-W15 | red-herring | 8 | 1.000 | 0.750 | 0.353 | 1.000 |
| RQ6-W15 | compound | 4 | 0.750 | 0.750 | 0.209 | 1.000 |
| RQ6-W15 | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| RQ6-W30 | clean | 70 | 1.000 | 0.943 | 0.384 | 1.000 |
| RQ6-W30 | red-herring | 8 | 1.000 | 0.750 | 0.348 | 1.000 |
| RQ6-W30 | compound | 4 | 0.750 | 0.750 | 0.209 | 1.000 |
| RQ6-W30 | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| RQ6-W60 | clean | 70 | 1.000 | 0.943 | 0.386 | 1.000 |
| RQ6-W60 | red-herring | 8 | 1.000 | 0.750 | 0.348 | 1.000 |
| RQ6-W60 | compound | 4 | 0.750 | 0.750 | 0.209 | 1.000 |
| RQ6-W60 | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| RQ6-Emetrics | clean | 70 | 0.329 | 0.171 | 0.073 | 1.000 |
| RQ6-Emetrics | red-herring | 8 | 0.375 | 0.250 | 0.080 | 1.000 |
| RQ6-Emetrics | compound | 4 | 0.250 | 0.000 | 0.046 | 1.000 |
| RQ6-Emetrics | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| RQ6-E+cloudtrail | clean | 70 | 0.686 | 0.600 | 0.145 | 1.000 |
| RQ6-E+cloudtrail | red-herring | 8 | 0.500 | 0.500 | 0.108 | 1.000 |
| RQ6-E+cloudtrail | compound | 4 | 0.500 | 0.500 | 0.062 | 1.000 |
| RQ6-E+cloudtrail | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| RQ6-E+logs | clean | 70 | 1.000 | 0.943 | 0.374 | 1.000 |
| RQ6-E+logs | red-herring | 8 | 1.000 | 0.750 | 0.320 | 1.000 |
| RQ6-E+logs | compound | 4 | 0.750 | 0.750 | 0.195 | 1.000 |
| RQ6-E+logs | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |
| RQ6-E+config | clean | 70 | 1.000 | 0.943 | 0.384 | 1.000 |
| RQ6-E+config | red-herring | 8 | 1.000 | 0.750 | 0.348 | 1.000 |
| RQ6-E+config | compound | 4 | 0.750 | 0.750 | 0.209 | 1.000 |
| RQ6-E+config | insufficient-evidence | 4 | 0.500 | 0.500 | n/a | 1.000 |

Insufficient-evidence cases are correct only when the diagnosis says `insufficient_evidence`; compound cases are scored on the primary (earlier) fault.

## Exploratory paired comparisons against Full (unadjusted)

| condition | metric | n | diff (cond - Full) | 95% CI | 95% CI (cluster) | bootstrap p | McNemar p |
|---|---|---|---|---|---|---|---|
| B1 | label_correct | 86 | -0.279 | [-0.372, -0.186] | [-0.425, -0.135] | 0.000 | 0.0 |
| B1 | root_cause_correct | 86 | -0.221 | [-0.326, -0.116] | [-0.388, -0.056] | 0.000 | 0.0003 |
| B1 | cited_recall | 82 | -0.109 | [-0.160, -0.059] | [-0.192, -0.029] | 0.000 | n/a |
| B1 | rubric | 86 | -0.366 | [-0.392, -0.340] | [-0.418, -0.321] | 0.000 | n/a |
| B2 | label_correct | 86 | -0.919 | [-0.988, -0.837] | [-1.000, -0.750] | 0.000 | 0.0 |
| B2 | root_cause_correct | 86 | -0.849 | [-0.930, -0.756] | [-0.977, -0.657] | 0.000 | 0.0 |
| B2 | cited_recall | 82 | -0.372 | [-0.401, -0.343] | [-0.430, -0.319] | 0.000 | n/a |
| B2 | rubric | 86 | -0.718 | [-0.744, -0.686] | [-0.750, -0.649] | 0.000 | n/a |
| B3 | label_correct | 86 | -0.779 | [-0.872, -0.674] | [-0.977, -0.521] | 0.000 | 0.0 |
| B3 | root_cause_correct | 86 | -0.721 | [-0.826, -0.605] | [-0.942, -0.431] | 0.000 | 0.0 |
| B3 | cited_recall | 82 | -0.363 | [-0.394, -0.332] | [-0.425, -0.305] | 0.000 | n/a |
| B3 | rubric | 86 | -0.203 | [-0.224, -0.180] | [-0.241, -0.150] | 0.000 | n/a |
| B4 | label_correct | 86 | -0.035 | [-0.081, +0.000] | [-0.071, +0.000] | 0.074 | 0.25 |
| B4 | root_cause_correct | 86 | -0.116 | [-0.186, -0.046] | [-0.214, -0.033] | 0.000 | 0.002 |
| B4 | cited_recall | 82 | -0.072 | [-0.098, -0.047] | [-0.102, -0.042] | 0.000 | n/a |
| B4 | rubric | 86 | -0.012 | [-0.023, -0.003] | [-0.021, -0.003] | 0.029 | n/a |
| A1 | label_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| A1 | root_cause_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| A1 | cited_recall | 82 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| A1 | rubric | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| A2 | label_correct | 86 | -0.035 | [-0.081, +0.000] | [-0.071, +0.000] | 0.074 | 0.25 |
| A2 | root_cause_correct | 86 | -0.116 | [-0.186, -0.046] | [-0.214, -0.033] | 0.000 | 0.002 |
| A2 | cited_recall | 82 | -0.072 | [-0.098, -0.047] | [-0.102, -0.042] | 0.000 | n/a |
| A2 | rubric | 86 | -0.012 | [-0.023, -0.003] | [-0.021, -0.003] | 0.029 | n/a |
| A3 | label_correct | 86 | +0.012 | [-0.023, +0.046] | [-0.032, +0.078] | 0.807 | 1.0 |
| A3 | root_cause_correct | 86 | +0.046 | [+0.000, +0.105] | [-0.022, +0.143] | 0.127 | 0.2188 |
| A3 | cited_recall | 82 | -0.090 | [-0.123, -0.057] | [-0.142, -0.041] | 0.000 | n/a |
| A3 | rubric | 86 | -0.006 | [-0.015, +0.000] | [-0.020, +0.000] | 0.272 | n/a |
| A4 | label_correct | 86 | -0.186 | [-0.267, -0.105] | [-0.393, +0.000] | 0.000 | 0.0 |
| A4 | root_cause_correct | 86 | -0.198 | [-0.314, -0.081] | [-0.422, +0.011] | 0.000 | 0.0015 |
| A4 | cited_recall | 82 | -0.066 | [-0.109, -0.025] | [-0.151, +0.014] | 0.002 | n/a |
| A4 | rubric | 86 | -0.046 | [-0.070, -0.026] | [-0.093, -0.006] | 0.000 | n/a |
| A5 | label_correct | 86 | +0.012 | [+0.000, +0.035] | [+0.000, +0.035] | 0.720 | 1.0 |
| A5 | root_cause_correct | 86 | +0.070 | [+0.023, +0.128] | [+0.000, +0.172] | 0.002 | 0.0312 |
| A5 | cited_recall | 82 | -0.067 | [-0.091, -0.044] | [-0.113, -0.029] | 0.000 | n/a |
| A5 | rubric | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| Full-KB-fault-removed | label_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| Full-KB-fault-removed | root_cause_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| Full-KB-fault-removed | cited_recall | 82 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| Full-KB-fault-removed | rubric | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| Full-KB-distractors | label_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| Full-KB-distractors | root_cause_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| Full-KB-distractors | cited_recall | 82 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| Full-KB-distractors | rubric | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| Full-no-redaction | label_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| Full-no-redaction | root_cause_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| Full-no-redaction | cited_recall | 82 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| Full-no-redaction | rubric | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| RQ6-K5 | label_correct | 86 | +0.023 | [+0.000, +0.058] | [+0.000, +0.060] | 0.276 | 0.5 |
| RQ6-K5 | root_cause_correct | 86 | +0.046 | [-0.012, +0.116] | [-0.023, +0.153] | 0.208 | 0.2891 |
| RQ6-K5 | cited_recall | 82 | -0.038 | [-0.058, -0.020] | [-0.078, -0.006] | 0.000 | n/a |
| RQ6-K5 | rubric | 86 | -0.003 | [-0.009, +0.000] | [-0.010, +0.000] | 0.721 | n/a |
| RQ6-K10 | label_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| RQ6-K10 | root_cause_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| RQ6-K10 | cited_recall | 82 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| RQ6-K10 | rubric | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| RQ6-K20 | label_correct | 86 | +0.012 | [+0.000, +0.035] | [+0.000, +0.035] | 0.720 | 1.0 |
| RQ6-K20 | root_cause_correct | 86 | -0.023 | [-0.058, +0.000] | [-0.071, +0.000] | 0.272 | 0.5 |
| RQ6-K20 | cited_recall | 82 | -0.005 | [-0.019, +0.009] | [-0.026, +0.017] | 0.474 | n/a |
| RQ6-K20 | rubric | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| RQ6-K40 | label_correct | 86 | +0.000 | [-0.035, +0.035] | [-0.034, +0.034] | 1.000 | 1.0 |
| RQ6-K40 | root_cause_correct | 86 | -0.058 | [-0.105, -0.012] | [-0.114, +0.000] | 0.018 | 0.0625 |
| RQ6-K40 | cited_recall | 82 | -0.015 | [-0.036, +0.005] | [-0.059, +0.023] | 0.129 | n/a |
| RQ6-K40 | rubric | 86 | -0.003 | [-0.009, +0.000] | [-0.009, +0.000] | 0.716 | n/a |
| RQ6-W5 | label_correct | 86 | -0.151 | [-0.233, -0.081] | [-0.290, -0.036] | 0.000 | 0.0002 |
| RQ6-W5 | root_cause_correct | 86 | -0.209 | [-0.302, -0.128] | [-0.360, -0.060] | 0.000 | 0.0 |
| RQ6-W5 | cited_recall | 82 | -0.113 | [-0.145, -0.081] | [-0.172, -0.049] | 0.000 | n/a |
| RQ6-W5 | rubric | 86 | -0.032 | [-0.049, -0.015] | [-0.069, +0.000] | 0.000 | n/a |
| RQ6-W15 | label_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| RQ6-W15 | root_cause_correct | 86 | -0.012 | [-0.035, +0.000] | [-0.035, +0.000] | 0.725 | 1.0 |
| RQ6-W15 | cited_recall | 82 | +0.004 | [-0.004, +0.013] | [-0.003, +0.015] | 0.353 | n/a |
| RQ6-W15 | rubric | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| RQ6-W30 | label_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| RQ6-W30 | root_cause_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| RQ6-W30 | cited_recall | 82 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| RQ6-W30 | rubric | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| RQ6-W60 | label_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| RQ6-W60 | root_cause_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| RQ6-W60 | cited_recall | 82 | +0.002 | [+0.000, +0.006] | [+0.000, +0.006] | 0.713 | n/a |
| RQ6-W60 | rubric | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| RQ6-Emetrics | label_correct | 86 | -0.628 | [-0.733, -0.523] | [-0.862, -0.378] | 0.000 | 0.0 |
| RQ6-Emetrics | root_cause_correct | 86 | -0.709 | [-0.802, -0.616] | [-0.870, -0.533] | 0.000 | 0.0 |
| RQ6-Emetrics | cited_recall | 82 | -0.300 | [-0.340, -0.259] | [-0.392, -0.210] | 0.000 | n/a |
| RQ6-Emetrics | rubric | 86 | -0.323 | [-0.390, -0.259] | [-0.481, -0.169] | 0.000 | n/a |
| RQ6-E+cloudtrail | label_correct | 86 | -0.314 | [-0.419, -0.221] | [-0.524, -0.144] | 0.000 | 0.0 |
| RQ6-E+cloudtrail | root_cause_correct | 86 | -0.314 | [-0.430, -0.209] | [-0.506, -0.145] | 0.000 | 0.0 |
| RQ6-E+cloudtrail | cited_recall | 82 | -0.234 | [-0.276, -0.194] | [-0.332, -0.149] | 0.000 | n/a |
| RQ6-E+cloudtrail | rubric | 86 | -0.169 | [-0.230, -0.111] | [-0.305, -0.059] | 0.000 | n/a |
| RQ6-E+logs | label_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| RQ6-E+logs | root_cause_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| RQ6-E+logs | cited_recall | 82 | -0.012 | [-0.019, -0.005] | [-0.028, +0.000] | 0.000 | n/a |
| RQ6-E+logs | rubric | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| RQ6-E+config | label_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| RQ6-E+config | root_cause_correct | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | 1.0 |
| RQ6-E+config | cited_recall | 82 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |
| RQ6-E+config | rubric | 86 | +0.000 | [+0.000, +0.000] | [+0.000, +0.000] | 1.000 | n/a |

## Caveats

- Synthetic incidents; ground truth and fault signatures come from one simulator (circularity). Real captures are needed before any claim.
- Only the pre-declared primary comparisons are Holm-corrected; the exploratory table is not, so treat its p-values as descriptive.
- With few fault types (clusters), cluster intervals are wide and coarse; they are the honest ones when incidents of a fault type share templates.
- The rubric score is a deterministic proxy; real runs add the LLM-judge rubric (`judge_prompt.txt`) and a human spot check (`spot_check.csv`).
