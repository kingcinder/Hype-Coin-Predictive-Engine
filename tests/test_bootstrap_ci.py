"""Tests for bootstrap_ci function in validation.metrics."""

import math

from validation.metrics import bootstrap_ci


def _mean(xs):
    return sum(xs) / len(xs)


def test_ci_contains_true_mean():
    groups = [[1.0, 1.0], [0.0, 0.0], [1.0], [0.0], [1.0, 0.0]] * 20
    lo, hi = bootstrap_ci(groups, _mean, n_boot=500)
    assert lo <= 0.5 <= hi


def test_empty_is_nan():
    lo, hi = bootstrap_ci([], _mean)
    assert math.isnan(lo) and math.isnan(hi)


def test_deterministic():
    g = [[1.0], [0.0], [1.0]] * 10
    assert bootstrap_ci(g, _mean, n_boot=200) == bootstrap_ci(g, _mean, n_boot=200)
