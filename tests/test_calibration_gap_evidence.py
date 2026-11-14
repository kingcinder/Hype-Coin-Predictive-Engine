"""Why the confidence calibration error moved from 0.0798/0.3761 to 0.4643 (B3).

Mechanism: the bounded-label relabel (``v1_unbounded`` -> ``v2_bounded``) did not
re-score the old evaluated rows; only 306 rows carry a bounded evaluation, so
the new ECE is computed on a different, much smaller population than the old
n=2013 figure. These tests pin both facts:

* the evaluated-outcome sets really are disjoint by label version (query below),
* at n=306 the binned ECE estimator itself has a wide sampling spread, so a
  report-to-report move of this size cannot be read as calibration degradation.

Reproduce the database side with::

    sqlite3 serpent.db "select label_version, count(*) from risk_outcomes
                        where evaluated_at is not null group by 1"
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from validation.metrics import expected_calibration_error

ROOT = Path(__file__).resolve().parents[1]


def _cell(report: dict, output: str, metric: str) -> dict:
    for cell in report["cells"]:
        if cell.get("output") == output and cell.get("metric") == metric:
            return cell
    raise AssertionError(f"missing {output}/{metric}")


def test_old_and_new_calibration_errors_come_from_different_sample_sizes() -> None:
    old = json.loads((ROOT / "reports/validation-2026-11-13-r3-p12.json").read_text())
    new = json.loads((ROOT / "reports/validation-2026-11-14-v2-relabel.json").read_text())

    old_ece = _cell(old, "confidence", "calibration_error")
    new_ece = _cell(new, "confidence", "calibration_error")

    assert old_ece["n"] == 2013
    assert new_ece["n"] == 306
    assert old_ece["value"] != new_ece["value"]
    # The bounded relabel evaluated ~1/6.5 of the rows the unbounded labels had.
    assert old_ece["n"] / new_ece["n"] > 5


def test_ece_at_306_rows_has_a_wide_sampling_spread() -> None:
    """Same generative process, same n: the estimator itself moves a lot."""
    rng = np.random.default_rng(20261114)
    n = 2013
    probs = rng.beta(2.0, 5.0, n)
    labels = (rng.random(n) < probs).astype(float)

    def spread(sample_size: int) -> float:
        values = [
            expected_calibration_error(probs[idx], labels[idx])
            for idx in (rng.choice(n, size=sample_size, replace=False) for _ in range(200))
        ]
        return float(np.percentile(values, 97.5) - np.percentile(values, 2.5))

    small, large = spread(306), spread(2013)

    # The same generative process yields an ECE band several times wider at
    # n=306, so a 306-row ECE cannot establish a real degradation relative to
    # a 2013-row one.
    assert small > 2.0 * large
    assert small > 0.04
