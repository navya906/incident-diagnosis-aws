"""Statistics for experiment reports: bootstrap confidence intervals, paired comparisons,
exact McNemar test, calibration (ECE, Brier).

Bootstrap resampling is over incidents (the unit of analysis); when an experiment has several
runs per condition, values are averaged per incident first. Resampling uses NumPy's PCG64 with
an explicit seed; the NumPy version is recorded in each experiment manifest because NumPy does
not guarantee identical streams across versions (DECISIONS D80).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np


def bootstrap_ci(
    values: Sequence[float], iterations: int = 2000, seed: int = 0, alpha: float = 0.05
) -> tuple[float, float, float]:
    """(mean, lower, upper) percentile bootstrap interval of the mean."""
    x = np.asarray(values, dtype=float)
    if x.size == 0:
        return (math.nan, math.nan, math.nan)
    mean = float(x.mean())
    if x.size == 1:
        return (mean, mean, mean)
    rng = np.random.Generator(np.random.PCG64(seed))
    idx = rng.integers(0, x.size, size=(iterations, x.size))
    means = x[idx].mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return (mean, float(lo), float(hi))


def paired_bootstrap_diff(
    a: Sequence[float],
    b: Sequence[float],
    iterations: int = 2000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float, float, float]:
    """Mean of (a - b) over the same incidents, its interval, and a two-sided bootstrap p-value
    (share of resampled mean differences on the other side of zero, doubled, capped at 1)."""
    x = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    if x.size == 0:
        return (math.nan, math.nan, math.nan, math.nan)
    mean = float(x.mean())
    if x.size == 1 or np.all(x == x[0]):
        return (mean, mean, mean, 1.0 if mean == 0 else 0.0)
    rng = np.random.Generator(np.random.PCG64(seed))
    idx = rng.integers(0, x.size, size=(iterations, x.size))
    means = x[idx].mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    p = 2 * min(float((means <= 0).mean()), float((means >= 0).mean()))
    return (mean, float(lo), float(hi), min(1.0, p))


def mcnemar_exact(a_correct: Sequence[bool], b_correct: Sequence[bool]) -> dict:
    """Exact (binomial) McNemar test on paired binary outcomes."""
    b01 = sum(1 for x, y in zip(a_correct, b_correct, strict=True) if x and not y)
    b10 = sum(1 for x, y in zip(a_correct, b_correct, strict=True) if y and not x)
    n = b01 + b10
    if n == 0:
        return {"a_only": 0, "b_only": 0, "p_value": 1.0}
    k = min(b01, b10)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return {"a_only": b01, "b_only": b10, "p_value": min(1.0, 2 * tail)}


def expected_calibration_error(
    confidence: Sequence[float], correct: Sequence[bool], bins: int = 10
) -> float:
    """ECE with equal-width bins on [0, 1]: sum over bins of |accuracy - mean confidence| x
    bin share."""
    c = np.asarray(confidence, dtype=float)
    y = np.asarray(correct, dtype=float)
    if c.size == 0:
        return math.nan
    edges = np.linspace(0, 1, bins + 1)
    which = np.clip(np.digitize(c, edges[1:-1], right=True), 0, bins - 1)
    ece = 0.0
    for b in range(bins):
        mask = which == b
        if mask.any():
            ece += mask.mean() * abs(y[mask].mean() - c[mask].mean())
    return float(ece)


def brier_score(confidence: Sequence[float], correct: Sequence[bool]) -> float:
    c = np.asarray(confidence, dtype=float)
    y = np.asarray(correct, dtype=float)
    return float(((c - y) ** 2).mean()) if c.size else math.nan


# ----------------------------------------------------------------------------- clustered / Holm
def _cluster_means(x: np.ndarray, clusters: Sequence[str], iterations: int, seed: int):
    labels = sorted(set(clusters))
    groups = [np.flatnonzero(np.asarray(clusters) == c) for c in labels]
    sums = np.array([x[g].sum() for g in groups])
    sizes = np.array([len(g) for g in groups], dtype=float)
    rng = np.random.Generator(np.random.PCG64(seed))
    pick = rng.integers(0, len(groups), size=(iterations, len(groups)))
    return sums[pick].sum(axis=1) / sizes[pick].sum(axis=1), len(groups)


def cluster_bootstrap_ci(
    values: Sequence[float],
    clusters: Sequence[str],
    iterations: int = 2000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """Mean with a cluster (block) bootstrap interval: whole clusters (e.g. all incidents of one
    fault type) are resampled, so correlated incidents are not treated as independent."""
    x = np.asarray(values, dtype=float)
    if x.size == 0:
        return (math.nan, math.nan, math.nan)
    mean = float(x.mean())
    means, k = _cluster_means(x, clusters, iterations, seed)
    if k < 2:
        return (mean, mean, mean)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return (mean, float(lo), float(hi))


def paired_cluster_bootstrap_diff(
    a: Sequence[float],
    b: Sequence[float],
    clusters: Sequence[str],
    iterations: int = 2000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float, float, float]:
    """Paired difference (a - b) with a cluster bootstrap interval and two-sided p-value."""
    x = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    if x.size == 0:
        return (math.nan, math.nan, math.nan, math.nan)
    mean = float(x.mean())
    if np.all(x == x[0]):
        return (mean, mean, mean, 1.0 if mean == 0 else 0.0)
    means, k = _cluster_means(x, clusters, iterations, seed)
    if k < 2:
        return (mean, mean, mean, math.nan)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    p = 2 * min(float((means <= 0).mean()), float((means >= 0).mean()))
    return (mean, float(lo), float(hi), min(1.0, p))


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values (same order as the input; NaN stays NaN)."""
    idx = [i for i, p in enumerate(pvalues) if not math.isnan(p)]
    order = sorted(idx, key=lambda i: pvalues[i])
    m = len(order)
    adjusted = [math.nan] * len(pvalues)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted
