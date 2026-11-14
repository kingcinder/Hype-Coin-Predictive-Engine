"""Phase B4 pre-registered verdict rules (asset-level API)."""

from __future__ import annotations

from validation.metrics import verdict


def test_insufficient_data_below_min_assets() -> None:
    assert verdict(0.5, 0.4, 0.6, 0.8, 0.7, 0.9, 10, True) == "insufficient_data"


def test_beats_baseline_lower_is_better() -> None:
    # Upper CI edge is below the baseline's lower CI edge.
    assert verdict(0.5, 0.4, 0.6, 0.8, 0.7, 0.9, 40, True) == "beats_baseline"


def test_worse_than_baseline_lower_is_better() -> None:
    # Lower CI edge is above the baseline's upper CI edge.
    assert verdict(0.9, 0.8, 1.0, 0.5, 0.4, 0.6, 40, True) == "worse_than_baseline"


def test_indistinguishable_when_intervals_overlap() -> None:
    assert verdict(0.7, 0.5, 0.9, 0.8, 0.7, 0.9, 40, True) == "indistinguishable"


def test_nan_input_is_insufficient_data() -> None:
    assert verdict(float("nan"), 0.4, 0.6, 0.8, 0.7, 0.9, 40, True) == "insufficient_data"
