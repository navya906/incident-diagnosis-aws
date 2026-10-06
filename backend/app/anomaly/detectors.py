"""Univariate anomaly detectors.

All detectors score a point against a *trailing* baseline window measured in minutes, so the same
configuration works for 60 s and 300 s data and no detector looks into the future. Missing points
simply shrink the window; a point with fewer than `min_history_points` baseline values is not
scored (sparse data never raises an error).

Every anomaly carries: score, baseline, observed value, method, timestamp, metric, resource.
Per method, `baseline` / `score` mean:

  zscore          trailing mean        |x - mean| / std
  mad             trailing median      0.6745 * |x - median| / MAD   (modified z-score)
  moving_average  trailing mean        |x - mean| / max(|mean|, 2 * std)  (relative deviation)
  rolling_std     trailing mean        std(last k points incl. x) / baseline std
  isolation_forest training-window mean  -decision_function of a forest fit on the training window

Spreads (std, MAD) are floored at max(floor_relative * |median|, floor_absolute) so flat series
(e.g. a constant task count) do not divide by zero and a real step still scores highly.
"""

from __future__ import annotations

import statistics
from abc import abstractmethod
from collections.abc import Sequence
from datetime import timedelta

import numpy as np

from app.config import AnomalySettings
from app.contracts.anomaly import Anomaly, MetricSeries
from app.interfaces.detector import Detector

_MAD_TO_STD = 0.6745


class TrailingWindowDetector(Detector):
    """Shared engine: walk the series, build each point's baseline from preceding points."""

    def __init__(
        self,
        threshold: float,
        baseline_minutes: int = 30,
        min_history_points: int = 5,
        exclude_anomalies_from_baseline: bool = True,
        floor_relative: float = 0.01,
        floor_absolute: float = 1e-6,
    ):
        self.threshold = threshold
        self.window = timedelta(minutes=baseline_minutes)
        self.min_history = min_history_points
        self.exclude = exclude_anomalies_from_baseline
        self.floor_relative = floor_relative
        self.floor_absolute = floor_absolute

    def floor(self, history: Sequence[float]) -> float:
        return max(self.floor_relative * abs(statistics.median(history)), self.floor_absolute)

    @abstractmethod
    def score(self, history: list[float], recent: list[float], x: float) -> tuple[float, float]:
        """Return (score, baseline) for value `x` given its baseline `history`."""

    def detect(self, series: MetricSeries) -> list[Anomaly]:
        pts = series.points
        flagged: set[int] = set()
        anomalies: list[Anomaly] = []
        lo = 0
        for i, p in enumerate(pts):
            while pts[lo].timestamp < p.timestamp - self.window:
                lo += 1
            history = [pts[j].value for j in range(lo, i) if not (self.exclude and j in flagged)]
            if len(history) < self.min_history:
                continue
            recent = [q.value for q in pts[max(0, i - self._recent_points() + 1) : i + 1]]
            s, baseline = self.score(history, recent, p.value)
            if s >= self.threshold:
                flagged.add(i)
                anomalies.append(
                    Anomaly(
                        resource_id=series.resource_id,
                        metric=series.metric,
                        timestamp=p.timestamp,
                        score=round(float(s), 6),
                        baseline=round(float(baseline), 6),
                        observed=p.value,
                        method=self.name,
                    )
                )
        return anomalies

    def _recent_points(self) -> int:
        return 1


class ZScoreDetector(TrailingWindowDetector):
    name = "zscore"

    def score(self, history, recent, x):
        mean = statistics.fmean(history)
        std = max(statistics.pstdev(history), self.floor(history))
        return abs(x - mean) / std, mean


class MadDetector(TrailingWindowDetector):
    """Modified z-score (Iglewicz & Hoaglin): robust to outliers already in the baseline."""

    name = "mad"

    def score(self, history, recent, x):
        med = statistics.median(history)
        mad = statistics.median(abs(v - med) for v in history)
        mad = max(mad, self.floor(history) * _MAD_TO_STD)
        return _MAD_TO_STD * abs(x - med) / mad, med


class MovingAverageDetector(TrailingWindowDetector):
    """Relative deviation from the trailing moving average."""

    name = "moving_average"

    def score(self, history, recent, x):
        mean = statistics.fmean(history)
        denom = max(abs(mean), 2 * statistics.pstdev(history), self.floor(history))
        return abs(x - mean) / denom, mean


class RollingStdDetector(TrailingWindowDetector):
    """Volatility change: std of the last k points versus the baseline std.

    Catches oscillation (e.g. tasks repeatedly restarting) that a mean-based test can miss.
    """

    name = "rolling_std"

    def __init__(self, threshold: float, recent_points: int = 3, **kw):
        super().__init__(threshold, **kw)
        self.recent_points = recent_points

    def _recent_points(self) -> int:
        return self.recent_points

    def score(self, history, recent, x):
        if len(recent) < self.recent_points:
            return 0.0, statistics.fmean(history)
        base = max(statistics.pstdev(history), self.floor(history))
        return statistics.pstdev(recent) / base, statistics.fmean(history)


class IsolationForestDetector(Detector):
    """Isolation Forest fit on the series' first `train_minutes` (assumed normal), scoring the rest.

    Features per point: standardized value and standardized first difference. Deterministic via
    `random_state`. Series with fewer than `min_train_points` training points are skipped.
    """

    name = "isolation_forest"

    def __init__(
        self,
        threshold: float = 0.0,
        train_minutes: int = 30,
        min_train_points: int = 5,
        n_estimators: int = 100,
        random_state: int = 0,
        floor_relative: float = 0.01,
        floor_absolute: float = 1e-6,
    ):
        self.threshold = threshold
        self.train_window = timedelta(minutes=train_minutes)
        self.min_train = min_train_points
        self.n_estimators = n_estimators
        self.random_state = random_state
        self.floor_relative = floor_relative
        self.floor_absolute = floor_absolute

    def detect(self, series: MetricSeries) -> list[Anomaly]:
        from sklearn.ensemble import IsolationForest  # noqa: PLC0415 - heavy import, used lazily

        pts = series.points
        if len(pts) < self.min_train + 1:
            return []
        cutoff = pts[0].timestamp + self.train_window
        n_train = sum(1 for p in pts if p.timestamp < cutoff)
        if n_train < self.min_train or n_train >= len(pts):
            return []
        values = np.array([p.value for p in pts], dtype=float)
        train = values[:n_train]
        mean = float(train.mean())
        floor = max(self.floor_relative * abs(float(np.median(train))), self.floor_absolute)
        std = max(float(train.std()), floor)
        diffs = np.concatenate([[0.0], np.diff(values)])
        features = np.column_stack([(values - mean) / std, diffs / std])
        forest = IsolationForest(
            n_estimators=self.n_estimators, contamination="auto", random_state=self.random_state
        )
        forest.fit(features[:n_train])
        scores = -forest.decision_function(features[n_train:])
        out = []
        for offset, s in enumerate(scores):
            if s > self.threshold:
                p = pts[n_train + offset]
                out.append(
                    Anomaly(
                        resource_id=series.resource_id,
                        metric=series.metric,
                        timestamp=p.timestamp,
                        score=round(float(s), 6),
                        baseline=round(mean, 6),
                        observed=p.value,
                        method=self.name,
                    )
                )
        return out


METHODS = ("zscore", "mad", "moving_average", "rolling_std", "isolation_forest")


def build_detector(method: str, settings: AnomalySettings) -> Detector:
    common = {
        "baseline_minutes": settings.baseline_minutes,
        "min_history_points": settings.min_history_points,
        "exclude_anomalies_from_baseline": settings.exclude_anomalies_from_baseline,
        "floor_relative": settings.floor_relative,
        "floor_absolute": settings.floor_absolute,
    }
    if method == "zscore":
        return ZScoreDetector(settings.zscore.threshold, **common)
    if method == "mad":
        return MadDetector(settings.mad.threshold, **common)
    if method == "moving_average":
        return MovingAverageDetector(settings.moving_average.threshold, **common)
    if method == "rolling_std":
        p = settings.rolling_std
        return RollingStdDetector(p.threshold, recent_points=p.recent_points, **common)
    if method == "isolation_forest":
        p = settings.isolation_forest
        return IsolationForestDetector(
            threshold=p.threshold,
            train_minutes=p.train_minutes,
            min_train_points=p.min_train_points,
            n_estimators=p.n_estimators,
            random_state=p.random_state,
            floor_relative=settings.floor_relative,
            floor_absolute=settings.floor_absolute,
        )
    raise ValueError(f"unknown anomaly method {method!r}; choose from {METHODS}")


def build_detectors(settings: AnomalySettings) -> list[Detector]:
    return [build_detector(m, settings) for m in settings.methods]
