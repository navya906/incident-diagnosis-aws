# Anomaly detector comparison (dev split) — smoke-test / synthetic

> **SMOKE-TEST / SYNTHETIC.** Simulator data with injected faults, not real AWS telemetry. These numbers show how the detectors behave on the simulator's assumptions; they are not evidence of real-world accuracy.

- Dataset: `synthetic-v1`, generator `1.1.0`, seed 42, content sha256 `06cec245b951be8e9b08524367439b5ee3d37fa75d6551d3bb8e2922d8388f8b`
- Split: `dev` — 86 incidents, 1325 metric series
- Labels: injected-fault windows from the truth files (`anomaly_labels`)
- Command: `python -m app.evaluation.anomaly_eval --sweep`
- Config: `anomaly` section of `config/default.yaml` (thresholds below are the defaults)

Precision = flagged points inside an injected window / all flagged points. Recall = injected windows with at least one flag / injected windows with data. Delay = minutes from window start to first flag. Series false-alarm rate = unaffected series with any flag / unaffected series.

## All resolutions

| method | precision | recall | f1 | median_delay_min | mean_delay_min | series_false_alarm_rate | flagged_points | detected_windows | windows |
|---|---|---|---|---|---|---|---|---|---|
| zscore | 0.68 | 0.856 | 0.758 | 1.0 | 2.4 | 0.574 | 7304 | 379 | 443 |
| mad | 0.474 | 0.91 | 0.623 | 1.7 | 3.1 | 0.572 | 9971 | 403 | 443 |
| moving_average | 0.41 | 0.896 | 0.562 | 1.7 | 3.0 | 0.373 | 10907 | 397 | 443 |
| rolling_std | 0.789 | 0.824 | 0.806 | 2.0 | 2.6 | 0.053 | 1309 | 365 | 443 |
| isolation_forest | 0.274 | 0.91 | 0.421 | 1.0 | 2.3 | 0.845 | 23249 | 403 | 443 |

## By metric resolution

| resolution | method | precision | recall | f1 | median_delay_min | mean_delay_min | series_false_alarm_rate | flagged_points | detected_windows | windows |
|---|---|---|---|---|---|---|---|---|---|---|
| 300s | zscore | 0.323 | 0.742 | 0.45 | 4.0 | 4.0 | 0.544 | 854 | 170 | 229 |
| 60s | zscore | 0.727 | 0.977 | 0.834 | 1.0 | 1.0 | 0.609 | 6450 | 209 | 214 |
| 300s | mad | 0.306 | 0.852 | 0.45 | 4.5 | 5.2 | 0.598 | 1042 | 195 | 229 |
| 60s | mad | 0.493 | 0.972 | 0.654 | 1.0 | 1.1 | 0.541 | 8929 | 208 | 214 |
| 300s | moving_average | 0.412 | 0.856 | 0.556 | 4.5 | 5.1 | 0.329 | 796 | 196 | 229 |
| 60s | moving_average | 0.41 | 0.939 | 0.57 | 1.0 | 0.9 | 0.425 | 10111 | 201 | 214 |
| 300s | rolling_std | 0.818 | 0.834 | 0.826 | 4.0 | 4.1 | 0.067 | 407 | 191 | 229 |
| 60s | rolling_std | 0.776 | 0.813 | 0.794 | 1.0 | 0.9 | 0.036 | 902 | 174 | 214 |
| 300s | isolation_forest | 0.245 | 0.878 | 0.383 | 4.0 | 4.0 | 0.777 | 3827 | 201 | 229 |
| 60s | isolation_forest | 0.28 | 0.944 | 0.432 | 0.5 | 0.6 | 0.925 | 19422 | 202 | 214 |

## Threshold sensitivity (dev split only)

| threshold | method | precision | recall | f1 | median_delay_min | mean_delay_min | series_false_alarm_rate | flagged_points | detected_windows | windows |
|---|---|---|---|---|---|---|---|---|---|---|
| 2.0 | zscore | 0.276 | 0.824 | 0.413 | 1.0 | 2.1 | 0.833 | 17565 | 365 | 443 |
| 3.0 | zscore | 0.68 | 0.856 | 0.758 | 1.0 | 2.4 | 0.574 | 7304 | 379 | 443 |
| 4.0 | zscore | 0.849 | 0.867 | 0.858 | 1.7 | 2.5 | 0.233 | 5730 | 384 | 443 |
| 6.0 | zscore | 0.918 | 0.856 | 0.886 | 2.0 | 2.5 | 0.053 | 4900 | 379 | 443 |
| 2.5 | mad | 0.353 | 0.903 | 0.507 | 1.0 | 3.1 | 0.787 | 14037 | 400 | 443 |
| 3.5 | mad | 0.474 | 0.91 | 0.623 | 1.7 | 3.1 | 0.572 | 9971 | 403 | 443 |
| 5.0 | mad | 0.522 | 0.898 | 0.66 | 2.0 | 3.0 | 0.336 | 8613 | 398 | 443 |
| 7.0 | mad | 0.532 | 0.865 | 0.659 | 2.0 | 3.0 | 0.211 | 7991 | 383 | 443 |
| 0.25 | moving_average | 0.282 | 0.955 | 0.435 | 1.7 | 3.4 | 0.598 | 17162 | 423 | 443 |
| 0.5 | moving_average | 0.41 | 0.896 | 0.562 | 1.7 | 3.0 | 0.373 | 10907 | 397 | 443 |
| 1.0 | moving_average | 0.603 | 0.707 | 0.651 | 1.7 | 2.5 | 0.163 | 6358 | 313 | 443 |
| 2.0 | moving_average | 0.899 | 0.65 | 0.755 | 1.7 | 2.4 | 0.054 | 4159 | 288 | 443 |
| 2.0 | rolling_std | 0.588 | 0.831 | 0.688 | 1.0 | 2.4 | 0.233 | 1882 | 368 | 443 |
| 3.0 | rolling_std | 0.789 | 0.824 | 0.806 | 2.0 | 2.6 | 0.053 | 1309 | 365 | 443 |
| 5.0 | rolling_std | 0.826 | 0.729 | 0.775 | 1.7 | 2.6 | 0.02 | 1140 | 323 | 443 |
| 8.0 | rolling_std | 0.814 | 0.655 | 0.726 | 2.0 | 2.6 | 0.018 | 1037 | 290 | 443 |
| -0.05 | isolation_forest | 0.193 | 0.968 | 0.322 | 0.5 | 1.5 | 0.959 | 37014 | 429 | 443 |
| 0.0 | isolation_forest | 0.274 | 0.91 | 0.421 | 1.0 | 2.3 | 0.845 | 23249 | 403 | 443 |
| 0.05 | isolation_forest | 0.401 | 0.795 | 0.533 | 1.0 | 2.6 | 0.726 | 13511 | 352 | 443 |
| 0.1 | isolation_forest | 0.547 | 0.598 | 0.572 | 1.0 | 2.2 | 0.517 | 7408 | 265 | 443 |

## Caveats

- Labels mark the whole post-onset window of every injected effect, including the ramp, so early ramp points that look normal count against recall/delay, and a fault's effects are assumed to persist to the end of the window.
- Ground-truth windows come from the same simulator assumptions the detectors are tested on (see PROGRESS.md, circularity risk). Real captures are needed to confirm any ranking.
- Delay is quantised by the metric period (1 or 5 minutes).
