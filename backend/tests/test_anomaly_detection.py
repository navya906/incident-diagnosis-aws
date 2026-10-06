"""Phase 3: unit tests for each detector, series building, the service and the evaluation."""

from __future__ import annotations

import math
import random
import sys
from datetime import UTC, datetime, timedelta

import pytest

from app.anomaly.detectors import (
    METHODS,
    IsolationForestDetector,
    MadDetector,
    MovingAverageDetector,
    RollingStdDetector,
    ZScoreDetector,
    build_detector,
    build_detectors,
)
from app.anomaly.series import infer_period_seconds, series_from_events
from app.anomaly.service import AnomalyService
from app.config import AnomalySettings, Settings
from app.contracts.anomaly import MetricPoint, MetricSeries
from app.contracts.events import CanonicalEvent, EventSource
from app.evaluation import anomaly_eval
from app.offline.dataset import generate_dataset
from app.offline.models import AnomalyLabel

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def make_series(values, period=60, start=T0, metric="m", resource="rds/db") -> MetricSeries:
    return MetricSeries(
        resource_id=resource,
        metric=metric,
        period_seconds=period,
        points=[
            MetricPoint(timestamp=start + timedelta(seconds=i * period), value=v)
            for i, v in enumerate(values)
            if v is not None
        ],
    )


def noisy(n, mean=100.0, sd=1.0, seed=0):
    rng = random.Random(seed)
    return [mean + rng.gauss(0, sd) for _ in range(n)]


def step_series(n=60, at=40, jump=10.0, period=60):
    vals = noisy(n)
    return make_series([v + (jump if i >= at else 0) for i, v in enumerate(vals)], period)


def first_flag_index(series, anomalies):
    times = [p.timestamp for p in series.points]
    return times.index(anomalies[0].timestamp) if anomalies else None


# ------------------------------------------------------------------ z-score
def test_zscore_detects_step_without_pre_step_false_alarms():
    s = step_series()
    out = ZScoreDetector(3.0).detect(s)
    assert first_flag_index(s, out) == 40
    a = out[0]
    assert a.method == "zscore" and a.resource_id == "rds/db" and a.metric == "m"
    assert a.baseline == pytest.approx(100, abs=1) and a.observed == pytest.approx(110, abs=3)
    assert a.score > 3


def test_zscore_excluding_anomalies_keeps_flagging_a_persistent_shift():
    s = step_series()
    kept = ZScoreDetector(3.0, exclude_anomalies_from_baseline=True).detect(s)
    adapted = ZScoreDetector(3.0, exclude_anomalies_from_baseline=False).detect(s)
    assert len(kept) == 20  # every post-step point stays anomalous against the clean baseline
    assert len(adapted) < len(kept)  # the contaminated baseline absorbs the shift


def test_zscore_threshold_is_configurable():
    s = step_series(jump=4.0)
    assert ZScoreDetector(3.0).detect(s)
    assert not ZScoreDetector(50.0).detect(s)


def test_flat_series_uses_floor_and_still_detects_step():
    s = make_series([4.0] * 30 + [1.0] * 5)
    out = ZScoreDetector(3.0).detect(s)
    assert out and math.isfinite(out[0].score) and out[0].observed == 1.0


# ------------------------------------------------------------------ MAD
def test_mad_is_robust_to_outliers_in_the_baseline():
    vals = noisy(40)
    vals[30] = 1000.0  # one huge outlier inside the next point's baseline
    vals.append(108.0)
    s = make_series(vals)
    mad = MadDetector(3.5, exclude_anomalies_from_baseline=False).detect(s)
    z = ZScoreDetector(3.0, exclude_anomalies_from_baseline=False).detect(s)
    last = s.points[-1].timestamp
    assert any(a.timestamp == last for a in mad)
    assert not any(a.timestamp == last for a in z)  # the outlier inflated the std


def test_mad_baseline_is_median():
    out = MadDetector(3.5).detect(step_series())
    assert out[0].method == "mad" and out[0].baseline == pytest.approx(100, abs=1)


# ------------------------------------------------------------------ moving average
def test_moving_average_flags_relative_jump():
    s = make_series(noisy(30, mean=0.002, sd=0.0002) + [0.05])
    out = MovingAverageDetector(0.5).detect(s)
    assert [a.observed for a in out] == [0.05]
    assert out[0].score > 10


def test_moving_average_tolerates_noise_on_near_zero_metric():
    s = make_series([0, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1])
    assert not MovingAverageDetector(2.0).detect(s)


# ------------------------------------------------------------------ rolling std
def test_rolling_std_detects_oscillation_that_keeps_the_mean():
    base = noisy(30, mean=50, sd=0.5)
    osc = [50 + (10 if i % 2 else -10) for i in range(10)]
    s = make_series(base + osc)
    out = RollingStdDetector(3.0, recent_points=3).detect(s)
    assert out and out[0].method == "rolling_std"
    assert first_flag_index(s, out) >= 30


def test_rolling_std_quiet_on_stationary_noise():
    assert not RollingStdDetector(3.0).detect(make_series(noisy(60)))


# ------------------------------------------------------------------ isolation forest
def test_isolation_forest_flags_spike_after_training_window_deterministically():
    s = make_series(noisy(50) + [130.0] + noisy(5, seed=9))
    d = IsolationForestDetector(threshold=0.0, train_minutes=30, random_state=0)
    out = d.detect(s)
    spike = s.points[50].timestamp
    assert any(a.timestamp == spike for a in out)
    assert all(a.timestamp >= T0 + timedelta(minutes=30) for a in out)  # never scores training
    assert [a.model_dump() for a in out] == [a.model_dump() for a in d.detect(s)]


def test_isolation_forest_skips_insufficient_training_data():
    d = IsolationForestDetector(train_minutes=30, min_train_points=5)
    assert d.detect(make_series([1.0, 2.0, 50.0])) == []
    sparse = make_series([1.0, None, None, None, 1.1, 1.0] + [9.0] * 3, period=600)
    assert d.detect(sparse) == []  # only 3 points fall in the 30-minute training window


# ------------------------------------------------------------------ resolution and sparsity
@pytest.mark.parametrize("method", METHODS)
def test_every_method_detects_a_step_at_both_resolutions(method):
    settings = AnomalySettings()
    for period in (60, 300):
        n = 18 if period == 300 else 90  # 90 minutes either way
        at = 12 if period == 300 else 60
        s = step_series(n=n, at=at, jump=80.0, period=period)  # +80%: above every default
        out = build_detector(method, settings).detect(s)
        step_t = s.points[at].timestamp
        assert any(a.timestamp >= step_t for a in out), (method, period)
        if method != "isolation_forest":  # IF flags normal noise too; its FP rate is measured
            assert all(a.timestamp >= step_t for a in out), (method, period)


@pytest.mark.parametrize("method", METHODS)
def test_sparse_and_tiny_series_never_crash(method):
    d = build_detector(method, AnomalySettings())
    assert d.detect(make_series([])) == []
    assert d.detect(make_series([5.0])) == []
    assert d.detect(make_series([1.0, 2.0, 3.0])) == []


def test_window_is_in_minutes_so_gaps_shrink_history():
    # 300 s data: a 30-minute window holds 6 points; with gaps only 3 remain -> not scored.
    vals = [100.0, None, 100.0, None, 100.0, None, 200.0]
    s = make_series(vals, period=300)
    assert not ZScoreDetector(3.0, baseline_minutes=30, min_history_points=5).detect(s)
    assert ZScoreDetector(3.0, baseline_minutes=30, min_history_points=3).detect(s)


# ------------------------------------------------------------------ series building
def _metric_event(ts, value, metric="CPUUtilization", rid="rds/db", period=60):
    return CanonicalEvent.build(
        timestamp=ts,
        source=EventSource.CLOUDWATCH_METRIC,
        service="rds",
        resource_id=rid,
        event_type="metric_datapoint",
        metric=metric,
        value=value,
        metadata={"namespace": "AWS/RDS", "stat": "Average", "period_seconds": period, "unit": ""},
        raw_ref=f"cw:AWS/RDS/{metric}/{rid}/{value}",
    )


def test_series_from_events_groups_sorts_dedupes_and_filters():
    events = [
        _metric_event(T0 + timedelta(minutes=2), 3.0),
        _metric_event(T0, 1.0),
        _metric_event(T0, 3.0),  # duplicate timestamp -> averaged
        _metric_event(T0 + timedelta(minutes=1), float("nan")),
        _metric_event(T0, 7.0, metric="DatabaseConnections", period=300),
        CanonicalEvent.build(
            timestamp=T0,
            source=EventSource.CLOUDWATCH_LOG,
            service="rds",
            resource_id="rds/db",
            event_type="log_line",
            message="x",
            metadata={"log_group": "g", "log_stream": "s"},
            raw_ref="cwlogs:g:s:1",
        ),
    ]
    series = {(s.resource_id, s.metric): s for s in series_from_events(events)}
    cpu = series[("rds/db", "CPUUtilization")]
    assert [p.value for p in cpu.points] == [2.0, 3.0] and cpu.period_seconds == 60
    assert series[("rds/db", "DatabaseConnections")].period_seconds == 300
    assert len(series) == 2


def test_infer_period_ignores_gaps():
    ts = [T0 + timedelta(minutes=m) for m in (0, 5, 10, 25, 30, 35, 40)]
    assert infer_period_seconds(ts) == 300
    assert infer_period_seconds([T0]) == 60


# ------------------------------------------------------------------ configuration and service
def test_detectors_follow_settings():
    settings = Settings().anomaly
    assert [d.name for d in build_detectors(settings)] == list(METHODS)
    tuned = settings.model_copy(update={"methods": ["mad"]})
    tuned.mad.threshold = 9.9
    (d,) = build_detectors(tuned)
    assert isinstance(d, MadDetector) and d.threshold == 9.9
    with pytest.raises(ValueError):
        build_detector("prophet", settings)


def test_service_runs_all_methods_on_events_and_uses_no_llm(tmp_path):
    events = [
        _metric_event(T0 + timedelta(minutes=i), 50.0 + (40 if i >= 35 else 0) + (i % 3) * 0.3)
        for i in range(45)
    ]
    out = AnomalyService(AnomalySettings()).detect(events)
    assert {a.method for a in out} >= {"zscore", "mad", "moving_average"}
    assert out == sorted(out, key=lambda a: (a.timestamp, a.resource_id, a.metric, a.method))
    assert not [m for m in sys.modules if m.startswith(("app.ai", "openai", "google.generativeai"))]


# ------------------------------------------------------------------ evaluation
def test_score_series_counts_precision_recall_and_delay():
    s = step_series(n=60, at=40, jump=20.0)
    start = s.points[38].timestamp  # label starts 2 points before the visible step
    label = AnomalyLabel(resource_id="rds/db", metric="m", start=start, end=s.points[-1].timestamp)
    c = anomaly_eval.score_series(s, [label], ZScoreDetector(3.0))
    assert c.windows == 1 and c.detected == 1 and c.delays_min == [2.0]
    assert c.tp_points == 20 and c.fp_points == 0
    clean = anomaly_eval.score_series(make_series(noisy(60)), [], ZScoreDetector(3.0))
    assert clean.clean_series == 1 and clean.windows == 0


def test_window_without_data_is_not_evaluable():
    s = make_series(noisy(20))
    label = AnomalyLabel(
        resource_id="rds/db", metric="m", start=T0 + timedelta(hours=5), end=T0 + timedelta(hours=6)
    )
    assert anomaly_eval.score_series(s, [label], ZScoreDetector(3.0)).windows == 0


def test_run_writes_labelled_tables(tmp_path):
    ds = tmp_path / "ds"
    generate_dataset(ds, seed=42)
    out = tmp_path / "exp"
    payload = anomaly_eval.run(
        ds,
        "dev",
        out,
        AnomalySettings(),
        sweep=True,
        methods=("zscore", "rolling_std"),
        max_incidents=6,
    )
    assert payload["meta"]["label"] == "smoke-test / synthetic"
    assert payload["meta"]["split"] == "dev" and payload["meta"]["incidents"] == 6
    assert {r["method"] for r in payload["all"]} == {"zscore", "rolling_std"}
    assert {r["resolution"] for r in payload["by_resolution"]} <= {"60s", "300s"}
    assert len(payload["sweep"]) == 8
    for r in payload["all"]:
        assert 0 <= r["precision"] <= 1 and 0 <= r["recall"] <= 1
    md = (out / "phase3-anomaly-detectors-dev.md").read_text(encoding="utf-8")
    assert "SMOKE-TEST / SYNTHETIC" in md and payload["meta"]["content_sha256"] in md
    assert (out / "phase3-anomaly-detectors-dev.csv").is_file()


def test_cli_refuses_test_split_without_final(tmp_path):
    with pytest.raises(SystemExit):
        anomaly_eval.main(["--split", "test", "--dataset", str(tmp_path), "--out", str(tmp_path)])


def test_committed_comparison_table_is_labelled_synthetic():
    path = anomaly_eval.DEFAULT_OUT / "phase3-anomaly-detectors-dev.md"
    text = path.read_text(encoding="utf-8")
    assert "SMOKE-TEST / SYNTHETIC" in text and "Split: `dev`" in text
    for m in METHODS:
        assert f"| {m} |" in text
