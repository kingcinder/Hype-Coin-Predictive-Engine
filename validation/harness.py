"""Harness orchestrator — reads engine rows, computes benchmark cells.

Read-only: never writes to engine tables. Reuses the engine's own persisted
point-in-time rows (scores, risk_outcomes, forecasts, labels, lifecycle,
market snapshots, ensemble state) per design doc §2.1.2. Metric computation is
split into pure helpers so synthetic self-tests exercise the same code path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, overload

import numpy as np
from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from common.config import get_settings
from forecast.labels import LABEL_FORWARD_MAX_GAIN, LABEL_HOURS_TO_PEAK
from storage import models
from validation.baselines import (
    base_rate_proportion,
    base_rate_wilson_ci,
    random_ranking_concordance,
    random_topk_precision_ci,
)
from validation.leakage import LeakageReport, check_feature_leakage
from validation.metrics import (
    BAND_RANKS,
    WilsonCI,
    bootstrap_ci,
    bootstrap_ci_generic,
    brier_base_rate,
    brier_score,
    concordance_index,
    confidence_calibration_error,
    expected_calibration_error,
    harrell_c_index,
    ordinal_band_distance,
    precision_at_k,
    verdict,
    wilson_ci,
)
from validation.metrics import (
    bootstrap_rows_ci as _bootstrap_ci,
)
from validation.report import MetricCell, ValidationReport

# Design doc §2.1.3: embargo must be >= the 24h forward horizon.
EMBARGO_HOURS = 48
PURGE_HOURS = 24
# Fraction of the scored window held out for evaluation (min 7 days).
EVAL_FRACTION = 0.30
MIN_EVAL_WINDOW_DAYS = 7
# Methodology §4.3: ECE trust ceiling — the threshold a probability output
# must beat to be worth trusting, not a perfect-calibration strawman.
CALIBRATION_CEILING = 0.10

# Live-token stratification: a token is "live" at score time when its
# lifecycle phase is pre-collapse (rank < 4); "dead" once it has reached
# collapse or a terminal exit.  "unknown" belongs to neither stratum.
# The headline cells are dominated by dead-token structure (dead tokens
# stay dead), so the edge is also measured on live tokens alone.
LIVE_PHASES = frozenset({"seeding", "ignition", "parabolic", "saturation"})
DEAD_PHASES = frozenset({"collapse", "dead", "rugged", "survivor"})


@dataclass
class OutcomeRow:
    """One evaluated score with its realized outcome."""

    asset_id: int
    decision_ts: datetime
    score_id: int | None
    predicted_band: str
    risk_score: float | None
    exit_risk: float | None
    confidence: float | None
    uncertainty: float | None
    collapsed: bool
    rugged: bool
    survived: bool
    label_version: str = "v3_bounded"
    # Lifecycle phase at score time (RiskOutcome.lifecycle_phase_at_score).
    phase_at_score: str | None = None


@dataclass
class ForecastRow:
    asset_id: int
    decision_ts: datetime
    # The feature timestamp the forecast was actually made FROM (persisted in
    # Forecast.details["source_features_ts"]). This is the true decision point
    # for outcome joins: labels are keyed by their own ts, and joining a
    # forecast to a label at the training-run timestamp would (almost) never
    # match. Falls back to decision_ts for legacy rows without the field.
    source_features_ts: datetime | None
    p_collapse_24h: float
    p_ignition_24h: float
    expected_hours_to_collapse: float | None
    expected_hours_to_peak: float | None


# Spec §5 binary head: an entry "hit" is a max forward gain of +100%.
ENTRY_GAIN_THRESHOLD = 1.0
# Spec §6: the entry precision cell reads the top 20 by research_priority.
ENTRY_PRECISION_K = 20


@dataclass
class EntryRow:
    """One scored decision point with its realized forward path.

    Built from Task 2's continuous labels (``forward_max_gain_pct`` /
    ``hours_to_peak``) joined to the score and forecast at the same
    decision timestamp. Missing joins stay ``None`` — an unscored or
    unforecast point is reported as such, never fabricated.
    """

    asset_id: int
    decision_ts: datetime
    research_priority: float | None
    p_ignition_24h: float | None
    max_gain_pct: float  # from forward_max_gain_pct label
    hours_to_peak: float | None  # from hours_to_peak label
    phase_at_score: str | None  # live-stratum membership via LIVE_PHASES


@dataclass
class ExitRow:
    """One held episode with its realized exit outcomes, in gain terms.

    All gains are proceeds-relative (spec §4): ``pev_gain_pct`` is
    Task 7's 10%-reference executable peak gain
    (``executable_peak_gain_s10``) — what selling at the executable
    peak would have kept — and ``clairvoyant_gain_pct`` is the raw
    price-peak gain, the unexecutable upper bound. ``policy_gain_pct``
    is the exit policy under test, net of costs; ``ladder_gain_pct``
    and ``horizon_gain_pct`` are the fixed-ladder and fixed-horizon
    benchmarks, also net.
    """

    asset_id: int
    decision_ts: datetime
    pev_gain_pct: float  # executable peak gain (10% reference)
    clairvoyant_gain_pct: float  # raw price-peak gain (unexecutable bound)
    policy_gain_pct: float  # net of costs, the policy under test
    ladder_gain_pct: float  # fixed-ladder benchmark, net
    horizon_gain_pct: float  # sell-all at entry + forward window, net


@overload
def _as_utc(ts: None) -> None: ...


@overload
def _as_utc(ts: datetime) -> datetime: ...


def _as_utc(ts: datetime | None) -> datetime | None:
    """Normalize a possibly-naive datetime to aware UTC for dict-key joins."""
    if ts is None:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _ts_key(value: object) -> datetime | None:
    """Coerce a datetime or ISO-8601 string timestamp to an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return _as_utc(parsed)
        except ValueError:
            return None
    return None


# ─────────────────────────────────────────────────────────────── pure metrics


def evaluate_probabilities(
    probs: np.ndarray,
    labels: np.ndarray,
    *,
    k: int = 10,
    output: str,
    regime: str,
    min_samples: int = 30,
    seed: int = 7,
) -> list[MetricCell]:
    """Full metric block for a probability output vs. binary outcomes."""
    probs = np.asarray(probs, dtype=float)
    labels = np.asarray(labels, dtype=float)
    n = len(labels)
    b = base_rate_proportion(labels) if n else float("nan")
    cells: list[MetricCell] = []

    def cell(
        metric: str,
        value: float,
        ci: tuple[float, float],
        baseline: float,
        baseline_ci: tuple[float, float],
        higher_is_better: bool,
        note: str = "",
    ) -> MetricCell:
        return MetricCell(
            output=output,
            metric=metric,
            regime=regime,
            n=n,
            value=value,
            ci_low=ci[0],
            ci_high=ci[1],
            baseline_value=baseline,
            baseline_ci_low=baseline_ci[0],
            baseline_ci_high=baseline_ci[1],
            verdict=verdict(
                value,
                WilsonCI(*ci),
                baseline,
                WilsonCI(*baseline_ci),
                higher_is_better=higher_is_better,
                n=n,
                min_samples=min_samples,
            ),
            note=note,
        )

    if n < min_samples or n == 0:
        # Emit ALL four metric cells as insufficient_data so the report is
        # structurally complete, not silently missing metrics.
        return [
            cell(
                "brier",
                float("nan"),
                (float("nan"), float("nan")),
                brier_base_rate(b) if np.isfinite(b) else float("nan"),
                (float("nan"), float("nan")),
                higher_is_better=False,
                note="insufficient samples",
            ),
            cell(
                "ece",
                float("nan"),
                (float("nan"), float("nan")),
                CALIBRATION_CEILING,
                (CALIBRATION_CEILING, CALIBRATION_CEILING),
                higher_is_better=False,
                note="insufficient samples",
            ),
            cell(
                f"precision@{k}",
                float("nan"),
                (float("nan"), float("nan")),
                float("nan") if not np.isfinite(b) else b,
                (float("nan"), float("nan")),
                higher_is_better=True,
                note="insufficient samples",
            ),
            cell(
                "concordance",
                float("nan"),
                (float("nan"), float("nan")),
                random_ranking_concordance(n),
                (0.5, 0.5),
                higher_is_better=True,
                note="insufficient samples",
            ),
        ]

    # Brier (lower better) vs base-rate constant predictor b(1-b).
    brier = brier_score(probs, labels)
    bb = brier_base_rate(b)
    ci = _bootstrap_ci(probs, labels, brier_score)
    cells.append(
        cell(
            "brier",
            brier,
            ci,
            bb,
            _bootstrap_ci(np.full(n, b), labels, brier_score),
            higher_is_better=False,
        )
    )

    # ECE (lower better) — compared against the TRUST CEILING, not against a
    # perfect-calibration strawman. `better_than_baseline` means "within the
    # ceiling the methodology says is trustworthy".
    ece = expected_calibration_error(probs, labels)
    ece_ci = _bootstrap_ci(probs, labels, expected_calibration_error)
    ceiling = CALIBRATION_CEILING
    note = "within trust ceiling (<=0.10)" if ece <= ceiling else "above trust ceiling"
    cells.append(
        cell("ece", ece, ece_ci, ceiling, (ceiling, ceiling), higher_is_better=False, note=note)
    )

    # Precision@K vs random ranking — the correct null is the simulated
    # hypergeometric distribution of a random top-k draw (a single top-k is a
    # draw, not the rate itself; see baselines.random_topk_precision_ci).
    k_eff = min(k, n)
    prec = precision_at_k(probs, labels, k_eff)
    prec_ci = _bootstrap_ci(probs, labels, lambda p, lab: precision_at_k(p, lab, k_eff))
    null_ci = random_topk_precision_ci(labels, k_eff)
    cells.append(
        cell(f"precision@{k}", prec, prec_ci, b, (null_ci.low, null_ci.high), higher_is_better=True)
    )

    # Concordance vs random ranking 0.5 (higher better).
    conc = concordance_index(probs, labels)
    conc_ci = _bootstrap_ci(probs, labels, concordance_index)
    cells.append(
        cell(
            "concordance",
            conc,
            conc_ci,
            random_ranking_concordance(n),
            (0.5, 0.5),
            higher_is_better=True,
        )
    )

    return cells


def _gain_lift(priorities: np.ndarray, hits: np.ndarray) -> float:
    """Top-decile hit rate ÷ overall hit rate (spec §6).

    A hit-rate ratio, never a mean-gain ratio: mean MFE is dominated by a
    handful of extreme runners and would certify a ranking whose typical
    top pick never doubles.
    """
    n = len(hits)
    if n == 0:
        return float("nan")
    base = float(np.mean(hits))
    if not np.isfinite(base) or base <= 0:
        return float("nan")
    order = np.argsort(-np.asarray(priorities, dtype=float), kind="stable")
    decile = max(1, math.ceil(n * 0.1))
    return float(np.mean(np.asarray(hits, dtype=float)[order][:decile]) / base)


def _evaluate_entry_priority(
    rows: list[EntryRow],
    *,
    output: str,
    regime: str,
    min_samples: int,
) -> list[MetricCell]:
    """precision@20 + gain-lift cells for one row set (all rows or live)."""
    ranked_pairs: list[tuple[float, EntryRow]] = []
    for r in rows:
        if r.research_priority is not None:
            ranked_pairs.append((r.research_priority, r))
    ranked_pairs.sort(key=lambda pair: pair[0], reverse=True)
    ranked = [r for _priority, r in ranked_pairs]
    n = len(ranked)
    if n < min_samples or n == 0:
        return [
            _insufficient_cell(output, "precision_at_20", regime, n, note="insufficient samples"),
            _insufficient_cell(
                output, "gain_lift_top_decile", regime, n, note="insufficient samples"
            ),
        ]
    # `ranked` is priority-sorted, so the hit array's head is the top group.
    hits = np.array(
        [1.0 if r.max_gain_pct >= ENTRY_GAIN_THRESHOLD else 0.0 for r in ranked], dtype=float
    )
    base = base_rate_proportion(hits)
    base_ci = base_rate_wilson_ci(hits)

    k_eff = min(ENTRY_PRECISION_K, n)
    hits_top = float(hits[:k_eff].sum())
    precision = hits_top / k_eff
    precision_ci = wilson_ci(hits_top, k_eff)
    cells = [
        MetricCell(
            output=output,
            metric="precision_at_20",
            regime=regime,
            n=n,
            value=precision,
            ci_low=precision_ci.low,
            ci_high=precision_ci.high,
            baseline_value=base,
            baseline_ci_low=base_ci.low,
            baseline_ci_high=base_ci.high,
            verdict=verdict(
                precision,
                precision_ci,
                base,
                base_ci,
                higher_is_better=True,
                n=n,
                min_samples=min_samples,
            ),
            note=(
                f"share of the top {k_eff} by research_priority reaching the "
                "§5 gain threshold (max_gain_pct >= 1.0)"
            ),
        )
    ]

    priorities = np.array([p for p, _r in ranked_pairs], dtype=float)
    lift = _gain_lift(priorities, hits)
    lift_ci = _bootstrap_ci(priorities, hits, _gain_lift)
    cells.append(
        MetricCell(
            output=output,
            metric="gain_lift_top_decile",
            regime=regime,
            n=n,
            value=lift,
            ci_low=lift_ci[0],
            ci_high=lift_ci[1],
            baseline_value=1.0,
            baseline_ci_low=1.0,
            baseline_ci_high=1.0,
            verdict=verdict(
                lift,
                WilsonCI(*lift_ci),
                1.0,
                WilsonCI(1.0, 1.0),
                higher_is_better=True,
                n=n,
                min_samples=min_samples,
            ),
            note="top-decile hit rate / overall hit rate at the §5 gain threshold",
        )
    )
    return cells


def _median_hours_to_peak_cell(
    rows: list[EntryRow],
    live_rows: list[EntryRow],
    *,
    regime: str,
    min_samples: int,
) -> MetricCell:
    """Lead-time cell: median realized hours_to_peak over ignited live rows.

    "Ignited" uses the engine's own ignition definition (max forward gain
    reaching ``forecast_ignition_threshold``). The baseline is the median
    over ALL ignited rows, so the cell measures whether live-token signals
    arrive earlier than the typical ignition, not earlier than themselves.
    """
    output = "entry_priority:live"
    threshold = get_settings().forecast_ignition_threshold
    live_hours = np.array(
        [
            r.hours_to_peak
            for r in live_rows
            if r.max_gain_pct >= threshold and r.hours_to_peak is not None
        ],
        dtype=float,
    )
    all_hours = np.array(
        [
            r.hours_to_peak
            for r in rows
            if r.max_gain_pct >= threshold and r.hours_to_peak is not None
        ],
        dtype=float,
    )
    n = len(live_hours)
    if n < min_samples or len(all_hours) == 0:
        return _insufficient_cell(
            output,
            "median_hours_to_peak",
            regime,
            n,
            note="insufficient ignited live rows with a realized hours_to_peak",
        )
    value = float(np.median(live_hours))
    baseline = float(np.median(all_hours))
    ci = _bootstrap_ci(live_hours, live_hours, lambda v, _d: float(np.median(v)))
    base_ci = _bootstrap_ci(all_hours, all_hours, lambda v, _d: float(np.median(v)))
    return MetricCell(
        output=output,
        metric="median_hours_to_peak",
        regime=regime,
        n=n,
        value=value,
        ci_low=ci[0],
        ci_high=ci[1],
        baseline_value=baseline,
        baseline_ci_low=base_ci[0],
        baseline_ci_high=base_ci[1],
        verdict=verdict(
            value,
            WilsonCI(*ci),
            baseline,
            WilsonCI(*base_ci),
            higher_is_better=False,
            n=n,
            min_samples=min_samples,
        ),
        note="median realized hours_to_peak over ignited live rows; lower = earlier signal",
    )


def evaluate_entry(rows: list[EntryRow], *, regime: str, min_samples: int = 30) -> list[MetricCell]:
    """Entry-mission cells (spec §6) over already-split eval rows.

    ``rows`` must be window C only — the embargoed eval tail produced by
    ``run_harness``'s walk-forward split. This function never re-splits:
    screening (window A) and baselines (window B) live outside it, and any
    feature screened by Tasks 5/13/14 enters as a frozen component list,
    never as a number fitted on window C.
    """
    settings = get_settings()
    cells: list[MetricCell] = []

    # p_ignition_24h probability block against Task 2's ignition outcomes
    # (max forward gain reaching the ignition threshold). Reuses
    # evaluate_probabilities; its ECE cell is the calibration_error cell
    # the entry gate reads.
    prob_rows = [r for r in rows if r.p_ignition_24h is not None]
    probs = np.array([r.p_ignition_24h for r in prob_rows], dtype=float)
    ignition_labels = np.array(
        [1.0 if r.max_gain_pct >= settings.forecast_ignition_threshold else 0.0 for r in prob_rows],
        dtype=float,
    )
    for cell in evaluate_probabilities(
        probs,
        ignition_labels,
        output="p_ignition_24h",
        regime=regime,
        min_samples=min_samples,
    ):
        if cell.metric == "ece":
            cell.metric = "calibration_error"
        cells.append(cell)

    cells.extend(
        _evaluate_entry_priority(
            rows, output="entry_priority", regime=regime, min_samples=min_samples
        )
    )
    live_rows = [r for r in rows if (r.phase_at_score or "") in LIVE_PHASES]
    cells.extend(
        _evaluate_entry_priority(
            live_rows, output="entry_priority:live", regime=regime, min_samples=min_samples
        )
    )
    cells.append(
        _median_hours_to_peak_cell(rows, live_rows, regime=regime, min_samples=min_samples)
    )
    return cells


def captured_fraction(realized_gain_pct: float, pev_gain_pct: float) -> float:
    """realized / pev; pev <= 0 -> 0.0 when realized <= 0 else 1.0 (no
    executable upside existed; keeping anything is full capture)."""
    if pev_gain_pct <= 0:
        return 0.0 if realized_gain_pct <= 0 else 1.0
    return realized_gain_pct / pev_gain_pct


def regret_decomposition(row: ExitRow) -> dict[str, float]:
    """Additive split of the total regret (clairvoyant - policy, net).

    ``executability_gap`` is the part no policy could have kept (the
    clairvoyant price peak overstates what was extractable);
    ``exit_timing_regret`` is PEV minus the policy's net gain;
    ``cost_drag`` is the residual. ``policy_gain_pct`` is net and
    ExitRow carries no gross policy series, so the residual is 0.0 by
    construction today: timing regret absorbs the full PEV-to-net gap
    until a gross series exists to separate costs from timing. The
    three components always sum to the total regret.
    """
    executability_gap = row.clairvoyant_gain_pct - row.pev_gain_pct
    exit_timing_regret = row.pev_gain_pct - row.policy_gain_pct
    total_regret = row.clairvoyant_gain_pct - row.policy_gain_pct
    return {
        "executability_gap": executability_gap,
        "exit_timing_regret": exit_timing_regret,
        "cost_drag": total_regret - executability_gap - exit_timing_regret,
    }


def evaluate_exit(rows: list[ExitRow], *, regime: str, min_samples: int = 30) -> list[MetricCell]:
    """Exit-mission cells (spec §4/§6) over held-out episodes.

    Every captured-fraction cell reads the policy against PEV with the
    fixed ladder's same-statistic captured fraction as the baseline —
    mean, median, and p25 (the gate reads median and p25: a policy
    that helps typical trades and hurts bad ones fails where the money
    is lost). ``mean_total_regret`` is lower-is-better against the
    ladder's mean total regret; ``horizon_captured_fraction`` is the
    fixed-horizon benchmark on the same construction, informational.
    """
    output = "exit_policy"
    metrics = (
        "captured_fraction",
        "median_captured_fraction",
        "p25_captured_fraction",
        "mean_total_regret",
        "horizon_captured_fraction",
    )
    n = len(rows)
    if n < min_samples or n == 0:
        return [
            _insufficient_cell(output, metric, regime, n, note="insufficient samples")
            for metric in metrics
        ]

    policy_cf = np.array(
        [captured_fraction(r.policy_gain_pct, r.pev_gain_pct) for r in rows], dtype=float
    )
    ladder_cf = np.array(
        [captured_fraction(r.ladder_gain_pct, r.pev_gain_pct) for r in rows], dtype=float
    )
    horizon_cf = np.array(
        [captured_fraction(r.horizon_gain_pct, r.pev_gain_pct) for r in rows], dtype=float
    )
    policy_regret = np.array(
        [r.clairvoyant_gain_pct - r.policy_gain_pct for r in rows], dtype=float
    )
    ladder_regret = np.array(
        [r.clairvoyant_gain_pct - r.ladder_gain_pct for r in rows], dtype=float
    )

    def cf_cell(
        metric: str,
        stat: Any,
        value_arr: np.ndarray,
        baseline_arr: np.ndarray,
        note: str,
    ) -> MetricCell:
        value = float(stat(value_arr))
        baseline = float(stat(baseline_arr))
        ci = _bootstrap_ci(value_arr, baseline_arr, lambda v, _b: float(stat(v)))
        base_ci = _bootstrap_ci(value_arr, baseline_arr, lambda _v, b: float(stat(b)))
        return MetricCell(
            output=output,
            metric=metric,
            regime=regime,
            n=n,
            value=value,
            ci_low=ci[0],
            ci_high=ci[1],
            baseline_value=baseline,
            baseline_ci_low=base_ci[0],
            baseline_ci_high=base_ci[1],
            verdict=verdict(
                value,
                WilsonCI(*ci),
                baseline,
                WilsonCI(*base_ci),
                higher_is_better=True,
                n=n,
                min_samples=min_samples,
            ),
            note=note,
        )

    cells = [
        cf_cell(
            "captured_fraction",
            np.mean,
            policy_cf,
            ladder_cf,
            "mean captured fraction of the policy vs PEV; baseline is the "
            "fixed ladder's mean captured fraction",
        ),
        cf_cell(
            "median_captured_fraction",
            np.median,
            policy_cf,
            ladder_cf,
            "median captured fraction of the policy vs PEV; baseline is the "
            "fixed ladder's median (gate cell, spec §6)",
        ),
        cf_cell(
            "p25_captured_fraction",
            lambda a: float(np.percentile(a, 25)),
            policy_cf,
            ladder_cf,
            "25th-percentile captured fraction of the policy vs PEV; "
            "baseline is the fixed ladder's p25 (gate cell, spec §6)",
        ),
    ]

    regret_value = float(np.mean(policy_regret))
    regret_baseline = float(np.mean(ladder_regret))
    regret_ci = _bootstrap_ci(policy_regret, ladder_regret, lambda v, _b: float(np.mean(v)))
    regret_base_ci = _bootstrap_ci(policy_regret, ladder_regret, lambda _v, b: float(np.mean(b)))
    cells.append(
        MetricCell(
            output=output,
            metric="mean_total_regret",
            regime=regime,
            n=n,
            value=regret_value,
            ci_low=regret_ci[0],
            ci_high=regret_ci[1],
            baseline_value=regret_baseline,
            baseline_ci_low=regret_base_ci[0],
            baseline_ci_high=regret_base_ci[1],
            verdict=verdict(
                regret_value,
                WilsonCI(*regret_ci),
                regret_baseline,
                WilsonCI(*regret_base_ci),
                higher_is_better=False,
                n=n,
                min_samples=min_samples,
            ),
            note="mean clairvoyant-minus-policy gain (net); baseline is the "
            "ladder's mean total regret — see regret_decomposition for the "
            "executability / timing / cost split",
        )
    )
    cells.append(
        cf_cell(
            "horizon_captured_fraction",
            np.mean,
            horizon_cf,
            ladder_cf,
            "mean captured fraction of the fixed-horizon sell vs PEV (benchmark, informational)",
        )
    )
    return cells


def evaluate_band_outcomes(
    outcomes: list[OutcomeRow],
    *,
    regime: str,
    min_samples: int = 30,
    seed: int = 7,
    stratum: str | None = None,
) -> list[MetricCell]:
    """Ordinal band evaluation + per-band collapse precision (methodology §2.1).

    With ``stratum`` set, the same cells are computed on a phase-at-score
    subset only (``"live"`` = pre-collapse phases, ``"dead"`` = collapse
    or terminal), with outputs prefixed ``risk_band:<stratum>``.  The
    baseline for a stratified cell is that stratum's own base rate,
    never the global one.
    """
    cells: list[MetricCell] = []
    output_prefix = f"risk_band:{stratum}" if stratum else "risk_band"
    if stratum == "live":
        stratum_note = (
            " live-token stratum: phase at score in {ignition, parabolic, saturation, seeding}"
        )
    elif stratum == "dead":
        stratum_note = " dead-token stratum: phase at score in {collapse, dead, rugged, survivor}"
    else:
        stratum_note = ""
    # Actual band: collapsed/rugged -> BLACK(4); survived -> GREEN(0);
    # ambiguous (unknown lifecycle) -> excluded from ordinal distance.
    pred_ranks = np.array([BAND_RANKS.get(o.predicted_band, np.nan) for o in outcomes], dtype=float)
    actual_ranks = np.array(
        [4.0 if (o.collapsed or o.rugged) else (0.0 if o.survived else np.nan) for o in outcomes],
        dtype=float,
    )
    valid = ~np.isnan(pred_ranks) & ~np.isnan(actual_ranks)
    if int(valid.sum()) >= min_samples:
        pred = pred_ranks[valid]
        actual = actual_ranks[valid]
        dist = ordinal_band_distance(pred, actual)
        # Baseline: always predict GREEN (0) — the laziest ordinal classifier.
        baseline_dist = ordinal_band_distance(np.zeros_like(actual), actual)

        # Asset-level bootstrap: resample ASSETS, not rows, so a token with
        # many scores cannot masquerade as many independent observations.
        def _dist(p: np.ndarray, a: np.ndarray) -> float:
            return ordinal_band_distance(p, a)

        valid_assets: dict[int, list[tuple[float, float]]] = {}
        for i in np.where(valid)[0]:
            valid_assets.setdefault(outcomes[i].asset_id, []).append(
                (float(pred_ranks[i]), float(actual_ranks[i]))
            )
        groups = [[(a, y) for a, y in rows] for rows in valid_assets.values()]

        def _group_dist(pairs: list[tuple[float, float]]) -> float:
            return ordinal_band_distance(
                np.array([p for p, _ in pairs]), np.array([y for _, y in pairs])
            )

        def _group_baseline(pairs: list[tuple[float, float]]) -> float:
            return ordinal_band_distance(np.zeros(len(pairs)), np.array([y for _, y in pairs]))

        ci = (
            bootstrap_ci(groups, _group_dist)
            if len(groups) > 1
            else _bootstrap_ci(pred, actual, _dist)
        )
        b_ci = (
            bootstrap_ci(groups, _group_baseline)
            if len(groups) > 1
            else _bootstrap_ci(np.zeros_like(actual), actual, _dist)
        )
        unknown = int((~valid).sum())
        n_assets = len(groups)
        cells.append(
            MetricCell(
                output=output_prefix,
                metric="ordinal_distance",
                regime=regime,
                n=int(valid.sum()),
                value=dist,
                ci_low=ci[0],
                ci_high=ci[1],
                baseline_value=baseline_dist,
                baseline_ci_low=b_ci[0],
                baseline_ci_high=b_ci[1],
                verdict=verdict(
                    dist,
                    ci[0],
                    ci[1],
                    baseline_dist,
                    b_ci[0],
                    b_ci[1],
                    n_assets,
                    True,
                    min_assets=min_samples,
                ),
                note=f"{unknown} ambiguous outcomes excluded from ordinal distance" + stratum_note,
                n_assets=n_assets,
            )
        )

    # Per-band collapse precision vs global base rate.
    labels = np.array([1.0 if (o.collapsed or o.rugged) else 0.0 for o in outcomes])
    b = base_rate_proportion(labels)
    band_base_ci = base_rate_wilson_ci(labels)
    for band in BAND_RANKS:
        idx = [i for i, o in enumerate(outcomes) if o.predicted_band == band]
        if not idx:
            continue
        band_labels = labels[idx]
        band_b = base_rate_proportion(band_labels)
        band_ci = wilson_ci(float(band_labels.sum()), len(band_labels))
        band_groups: dict[int, list[float]] = {}
        for row_index in idx:
            band_groups.setdefault(outcomes[row_index].asset_id, []).append(
                float(labels[row_index])
            )
        if len(band_groups) > 1:
            grouped_ci = bootstrap_ci(
                list(band_groups.values()), lambda values: float(np.mean(values))
            )
            band_ci = WilsonCI(*grouped_ci)
        unknown_band = sum(
            1
            for i in idx
            if not (outcomes[i].collapsed or outcomes[i].rugged or outcomes[i].survived)
        )
        cells.append(
            MetricCell(
                output=f"{output_prefix}:{band}",
                metric="collapse_precision",
                regime=regime,
                n=len(idx),
                value=band_b,
                ci_low=band_ci.low,
                ci_high=band_ci.high,
                baseline_value=b,
                baseline_ci_low=band_base_ci.low,
                baseline_ci_high=band_base_ci.high,
                verdict=verdict(
                    band_b,
                    band_ci.low,
                    band_ci.high,
                    b,
                    band_base_ci.low,
                    band_base_ci.high,
                    len(band_groups),
                    False,
                    min_assets=min_samples,
                ),
                note=f"{unknown_band} ambiguous outcomes treated as non-collapse (reported)"
                + stratum_note,
                n_assets=len(band_groups),
            )
        )
    return cells


def evaluate_hazard(
    forecasts: list[ForecastRow],
    outcomes_by_asset_ts: dict[tuple[int, datetime], tuple[bool, float]],
    *,
    regime: str,
    min_samples: int = 30,
) -> list[MetricCell]:
    """Harrell C-index for time-to-collapse with right-censoring (§1.6/§2.4).

    ``outcomes_by_asset_ts`` maps (asset, forecast_ts) -> (collapsed, trough_hours),
    keyed by the forecast's *source feature timestamp* (the ts the forecast
    was made from), not the training-run timestamp.
    A forecast's event time is the realized trough hours when the asset
    collapsed inside the window; otherwise it is right-censored at the 24h
    horizon. The risk score is expected_hours_to_collapse (shorter = higher
    risk), negated for the C-index's higher-risk-higher convention.
    Forecasts with expected_hours_to_collapse None (p_collapse < 0.5) are the
    model's "no collapse within the horizon" prediction: they enter with risk
    24.0 (the censoring horizon, i.e. the lowest-risk prediction) instead of
    being dropped — dropping them would compute concordance only on the
    high-risk subset.
    """
    cells: list[MetricCell] = []
    risks: list[float] = []
    times: list[float] = []
    events: list[float] = []
    n_censored = 0
    matched = 0
    for f in forecasts:
        key_ts = _as_utc(f.source_features_ts)
        if key_ts is None:
            continue  # no source timestamp: cannot match a label row
        outcome = outcomes_by_asset_ts.get((f.asset_id, key_ts))
        if outcome is None:
            continue
        collapsed, trough_hours = outcome
        matched += 1
        risks.append(
            float(f.expected_hours_to_collapse)
            if f.expected_hours_to_collapse is not None
            else 24.0
        )
        if collapsed:
            # event at the realized trough (bounded by the 24h horizon)
            times.append(min(float(trough_hours) if trough_hours is not None else 24.0, 24.0))
            events.append(1.0)
        else:
            # right-censored at the forward horizon — a censored observation,
            # never a negative.
            times.append(24.0)
            events.append(0.0)
            n_censored += 1
    n = len(risks)
    if n < min_samples:
        return [
            MetricCell(
                output="hazard_time_to_collapse",
                metric="c_index",
                regime=regime,
                n=n,
                value=float("nan"),
                ci_low=float("nan"),
                ci_high=float("nan"),
                baseline_value=0.5,
                baseline_ci_low=0.5,
                baseline_ci_high=0.5,
                verdict="insufficient_data",
                note=f"{matched} forecast/outcome pairs matched; {n_censored} right-censored",
            )
        ]
    # Shorter expected hours = higher collapse risk -> negate.
    risks_arr = -np.array(risks)
    times_arr = np.array(times)
    events_arr = np.array(events)
    c_index = harrell_c_index(risks_arr, times_arr, events_arr)
    ci = bootstrap_ci_generic(
        lambda r, t, e: harrell_c_index(r, t, e),
        risks_arr,
        times_arr,
        events_arr,
    )
    cells.append(
        MetricCell(
            output="hazard_time_to_collapse",
            metric="c_index",
            regime=regime,
            n=n,
            value=c_index,
            ci_low=ci[0],
            ci_high=ci[1],
            baseline_value=0.5,
            baseline_ci_low=0.5,
            baseline_ci_high=0.5,
            verdict=verdict(
                c_index, WilsonCI(*ci), 0.5, WilsonCI(0.5, 0.5), higher_is_better=True, n=n
            ),
            note=f"{n_censored} right-censored",
        )
    )
    return cells


def evaluate_confidence_uncertainty(
    outcomes: list[OutcomeRow],
    *,
    regime: str,
    min_samples: int = 30,
    seed: int = 7,
) -> list[MetricCell]:
    """Confidence calibration + uncertainty inverse-calibration (§2.2)."""
    cells: list[MetricCell] = []
    conf = np.array([o.confidence if o.confidence is not None else np.nan for o in outcomes])
    unc = np.array([o.uncertainty if o.uncertainty is not None else np.nan for o in outcomes])
    surv = np.array([1.0 if o.survived else 0.0 for o in outcomes])
    coll = np.array([1.0 if (o.collapsed or o.rugged) else 0.0 for o in outcomes])

    conf_valid = ~np.isnan(conf)
    if int(conf_valid.sum()) >= min_samples:
        err = confidence_calibration_error(conf[conf_valid], surv[conf_valid])
        # Asset-level bootstrap: resample ASSETS, not rows, so a token with many
        # scores cannot masquerade as many independent calibration samples.
        conf_groups: dict[int, list[tuple[float, float]]] = {}
        for i in np.where(conf_valid)[0]:
            conf_groups.setdefault(outcomes[i].asset_id, []).append(
                (float(conf[i]), float(surv[i]))
            )
        groups = list(conf_groups.values())

        def _group_calibration_error(pairs: list[tuple[float, float]]) -> float:
            return confidence_calibration_error(
                np.array([c for c, _ in pairs]), np.array([s for _, s in pairs])
            )

        ci = (
            bootstrap_ci(groups, _group_calibration_error)
            if len(groups) > 1
            else _bootstrap_ci(
                conf[conf_valid], surv[conf_valid], lambda c, s: confidence_calibration_error(c, s)
            )
        )
        # Compared against the same trust ceiling as ECE (methodology §4.3) —
        # 'better' means the confidence score is calibrated within the ceiling,
        # not that it is literally perfect.
        ceiling = CALIBRATION_CEILING
        cells.append(
            MetricCell(
                output="confidence",
                metric="calibration_error",
                regime=regime,
                n=int(conf_valid.sum()),
                value=err,
                ci_low=ci[0],
                ci_high=ci[1],
                baseline_value=ceiling,
                baseline_ci_low=ceiling,
                baseline_ci_high=ceiling,
                verdict=verdict(
                    err,
                    ci[0],
                    ci[1],
                    ceiling,
                    ceiling,
                    ceiling,
                    len(groups),
                    True,
                    min_assets=min_samples,
                ),
                note="confidence/100 vs observed survival frequency; trust ceiling 0.10",
                n_assets=len(groups),
            )
        )

    unc_valid = ~np.isnan(unc)
    if int(unc_valid.sum()) >= min_samples:
        # Inverse calibration: high uncertainty should accompany *worse* risk
        # predictions. Correlation between uncertainty and per-sample risk
        # prediction error (|risk - outcome|), with a bootstrap CI.
        risk = np.array([o.risk_score if o.risk_score is not None else np.nan for o in outcomes])
        both = unc_valid & ~np.isnan(risk)
        if int(both.sum()) >= min_samples:
            pred = risk[both] / 100.0
            actual = coll[both]
            err_per_sample = np.abs(pred - actual)
            corr = _safe_corrcoef(unc[both], err_per_sample)
            ci = _bootstrap_ci(unc[both], err_per_sample, lambda u, e: _safe_corrcoef(u, e))
            cells.append(
                MetricCell(
                    output="uncertainty",
                    metric="error_correlation",
                    regime=regime,
                    n=int(both.sum()),
                    value=corr,
                    ci_low=ci[0],
                    ci_high=ci[1],
                    baseline_value=0.0,
                    baseline_ci_low=-0.15,
                    baseline_ci_high=0.15,
                    verdict=verdict(
                        corr,
                        WilsonCI(*ci),
                        0.0,
                        WilsonCI(-0.15, 0.15),
                        higher_is_better=True,
                        n=int(both.sum()),
                    ),
                    note="positive = uncertainty tracks prediction error (desired)",
                )
            )
        else:
            cells.append(
                _insufficient_cell("uncertainty", "error_correlation", regime, int(both.sum()))
            )
    return cells


def ensemble_weight_tracking(
    weight_history: list[dict[str, Any]],
    scorer_accuracy_history: dict[str, list[dict[str, Any]]],
    scorer_names: tuple[str, ...] = ("rule", "ml", "heuristic"),
) -> dict[str, float]:
    """Meta-metric (methodology §2.5): do weights track trailing scorer accuracy?

    Correlation between each scorer's weight trajectory and the *harness's own*
    per-window accuracy estimates (computed from RiskOutcome/Score rows, not
    from the ensemble's self-reported accuracy — which would be circular).
    Positive correlation = weights track accuracy; ~0/negative = drift.
    """
    result: dict[str, float] = {}
    for name in scorer_names:
        w_seq: list[float] = []
        acc_seq: list[float] = []
        for entry in weight_history:
            w = (entry.get("weights") or {}).get(name)
            if w is None:
                continue
            entry_ts = entry.get("ts")
            # Latest accuracy observation at or before this weight timestamp;
            # timestamps may be datetime or ISO strings — coerce via _ts_key.
            # A FUTURE accuracy observation must never be used: at the weight
            # time it was not knowable, and selecting it would leak the future
            # into this "at or before" meta-metric.
            best: float | None = None
            best_ts = None
            entry_ts_parsed = _ts_key(entry_ts)
            for acc_row in scorer_accuracy_history.get(name, []):
                acc_ts = _ts_key(acc_row.get("ts"))
                if acc_ts is None or entry_ts_parsed is None:
                    continue
                if acc_ts > entry_ts_parsed:
                    continue
                if best_ts is None or acc_ts > best_ts:
                    best_ts = acc_ts
                    best = acc_row.get("accuracy")
            if best is None:
                continue
            w_seq.append(float(w))
            acc_seq.append(float(best))
        if len(w_seq) < 3:
            result[name] = float("nan")
            continue
        # A constant weight series (e.g. a scorer pinned at its floor) has zero
        # variance — correlation is undefined, reported as nan, never fabricated.
        result[name] = _safe_corrcoef(np.array(w_seq), np.array(acc_seq))
    return result


# ──────────────────────────────────────────────────────────────── data loading


def load_outcomes(session: Session) -> list[OutcomeRow]:
    """Load evaluated, bounded (v3) risk outcomes joined to their scores.

    Confirmatory metrics use only timestamp-bounded labels. ``v1_unbounded``
    rows are retrospective/exploratory and are excluded.
    """
    rows = session.execute(
        select(models.RiskOutcome, models.Score)
        .join(models.Score, models.Score.id == models.RiskOutcome.score_id)
        .where(
            models.RiskOutcome.evaluated_at.is_not(None),
            models.RiskOutcome.label_version == "v3_bounded",
        )
    ).all()
    out: list[OutcomeRow] = []
    for outcome, score in rows:
        out.append(
            OutcomeRow(
                asset_id=outcome.asset_id,
                decision_ts=_as_utc(score.decision_ts),
                score_id=outcome.score_id,
                predicted_band=outcome.risk_band,
                risk_score=score.risk,
                exit_risk=score.exit_risk,
                confidence=score.confidence,
                uncertainty=score.uncertainty,
                collapsed=bool(outcome.collapsed),
                rugged=bool(outcome.rugged),
                survived=bool(outcome.survived),
                label_version=outcome.label_version or "v3_bounded",
                phase_at_score=outcome.lifecycle_phase_at_score or None,
            )
        )
    return out


def load_unresolved_outcome_count(session: Session) -> int:
    """Count outcome rows past their window that still have no label.

    An unresolved row keeps ``evaluated_at IS NULL`` because no lifecycle phase
    inside its window established collapse or survival. It is withheld from
    every metric, so the count is reported rather than silently dropped.
    """
    window = timedelta(hours=get_settings().risk_outcome_window_hours)
    now = datetime.now(UTC)
    scored_ats = session.scalars(
        select(models.RiskOutcome.scored_at).where(
            models.RiskOutcome.evaluated_at.is_(None),
            models.RiskOutcome.censored_at.is_(None),
            models.RiskOutcome.label_version == "v3_bounded",
        )
    ).all()
    count = 0
    for ts in scored_ats:
        scored_at = _as_utc(ts)
        if scored_at is not None and scored_at + window <= now:
            count += 1
    return count


def load_forecasts(session: Session) -> list[ForecastRow]:
    rows = session.scalars(select(models.Forecast)).all()
    out: list[ForecastRow] = []
    for f in rows:
        details = f.details or {}
        # The true decision point for outcome joins (see ForecastRow).
        source_features_ts = _ts_key(details.get("source_features_ts"))
        if source_features_ts is None:
            source_features_ts = _as_utc(f.decision_ts)
        out.append(
            ForecastRow(
                asset_id=f.asset_id,
                decision_ts=_as_utc(f.decision_ts),
                source_features_ts=source_features_ts,
                p_collapse_24h=f.p_collapse_24h,
                p_ignition_24h=f.p_ignition_24h,
                expected_hours_to_collapse=f.expected_hours_to_collapse,
                expected_hours_to_peak=f.expected_hours_to_peak,
            )
        )
    return out


def load_entry_rows(session: Session) -> list[EntryRow]:
    """Entry-mission rows from Task 2's continuous forward labels.

    One row per (asset, ts) carrying a ``forward_max_gain_pct`` label.
    ``research_priority`` joins from the score at the same decision ts;
    ``p_ignition_24h`` from the forecast made FROM that ts (its source
    feature timestamp — the ForecastRow join key, since a forecast's
    persisted decision_ts is the training-run time); the live-stratum
    phase from the risk outcome recorded at the same score, where present.
    """
    gains: dict[tuple[int, datetime], float] = {}
    hours: dict[tuple[int, datetime], float] = {}
    for label in session.scalars(
        select(models.Label).where(
            models.Label.label_type.in_((LABEL_FORWARD_MAX_GAIN, LABEL_HOURS_TO_PEAK))
        )
    ).all():
        label_ts = _as_utc(label.ts)
        if label_ts is None:
            continue
        try:
            value = float(label.label_value)
        except (TypeError, ValueError):
            continue
        if label.label_type == LABEL_FORWARD_MAX_GAIN:
            gains[(label.asset_id, label_ts)] = value
        else:
            hours[(label.asset_id, label_ts)] = value
    priorities: dict[tuple[int, datetime], float] = {}
    for score in session.scalars(select(models.Score)).all():
        score_ts = _as_utc(score.decision_ts)
        if score_ts is not None:
            priorities[(score.asset_id, score_ts)] = float(score.research_priority)
    ignition: dict[tuple[int, datetime], float] = {}
    for forecast in load_forecasts(session):
        forecast_ts = _as_utc(forecast.source_features_ts)
        if forecast_ts is not None:
            ignition[(forecast.asset_id, forecast_ts)] = float(forecast.p_ignition_24h)
    phases: dict[tuple[int, datetime], str] = {}
    for outcome in load_outcomes(session):
        outcome_ts = _as_utc(outcome.decision_ts)
        if outcome_ts is not None and outcome.phase_at_score:
            phases[(outcome.asset_id, outcome_ts)] = outcome.phase_at_score
    rows: list[EntryRow] = []
    for (asset_id, ts), max_gain in sorted(gains.items()):
        rows.append(
            EntryRow(
                asset_id=asset_id,
                decision_ts=ts,
                research_priority=priorities.get((asset_id, ts)),
                p_ignition_24h=ignition.get((asset_id, ts)),
                max_gain_pct=max_gain,
                hours_to_peak=hours.get((asset_id, ts)),
                phase_at_score=phases.get((asset_id, ts)),
            )
        )
    return rows


def load_exit_rows(session: Session) -> list[ExitRow]:
    """Exit-mission rows — stub until Task 9.

    Real rows need the Task 9 exit policy replayed over held episodes
    (policy / ladder / horizon gains against Task 7's executable
    peaks). Until that policy exists there are no honest rows to load,
    so this returns ``[]``: the exit evaluator reports
    ``insufficient_data`` and ``exit_validated`` reads False.
    """
    del session  # No reads until the Task 9 loader lands.
    return []


def load_forecast_outcomes(session: Session) -> dict[tuple[int, datetime], tuple[bool, float]]:
    """Collapse-window outcome per (asset, forecast_ts) from the label table.

    A forecast at ts is scored 'collapsed' when a collapse label exists at ts
    (label_value == '1'). No label row at that (asset, ts) → unobserved, so the
    forecast is excluded rather than treated as a negative (methodology §3.2).
    Returns (collapsed, trough_hours) where trough_hours is the time from the
    label ts to the trough — here approximated by the label's forward window
    since labels carry no explicit trough timestamp.
    """
    out: dict[tuple[int, datetime], tuple[bool, float]] = {}
    for label in session.scalars(
        select(models.Label).where(models.Label.label_type == "collapse")
    ).all():
        ts = _as_utc(label.ts)
        if ts is None:
            continue
        out[(label.asset_id, ts)] = (label.label_value == "1", 24.0)
    return out


def load_ensemble_history(session: Session) -> list[dict[str, Any]]:
    state = session.scalar(select(models.EnsembleState).limit(1))
    if state is None:
        return []
    return [dict(entry) for entry in (state.weight_history or [])]


def load_scorer_accuracy_history(
    session: Session, outcomes: list[OutcomeRow]
) -> dict[str, list[dict[str, Any]]]:
    """Harness-computed trailing accuracy per scorer, per UTC week.

    For each evaluated outcome in each UTC week, each scorer's band prediction
    is compared against the realized outcome — the same correctness rule the
    ensemble uses (GREEN/YELLOW+survived = correct; RED/ORANGE/BLACK+collapse =
    correct). ML and heuristic bands are read from the persisted
    ``RiskOutcome.details`` (ml_risk_band / heuristic_band with their
    ``*_prediction`` flags) so all three scorers are tracked, not just rule.
    Timestamps are emitted as ISO week-start datetimes (parseable by
    ``_ts_key``) — never opaque ``%W`` week strings.
    """
    from datetime import UTC

    def _week_start(ts: datetime) -> datetime:
        # Monday 00:00 UTC of ts's ISO week.
        monday = ts - timedelta(days=ts.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC)

    weeks: dict[datetime, dict[str, list[bool]]] = {}
    for o in outcomes:
        ts = _as_utc(o.decision_ts)
        if ts is None:
            continue
        week_key = _week_start(ts)
        actual = (
            "positive"
            if o.survived and not (o.collapsed or o.rugged)
            else ("negative" if (o.collapsed or o.rugged) else None)
        )
        if actual is None:
            continue
        week = weeks.setdefault(week_key, {})
        # rule scorer: the recorded band IS the rule band.
        week.setdefault("rule", []).append(
            (o.predicted_band in ("GREEN", "YELLOW")) == (actual == "positive")
        )

    # ML / heuristic bands live on the RiskOutcome rows, not OutcomeRow —
    # reload them per outcome so the meta-metric covers all three scorers.
    details_by_score: dict[int, dict[str, Any]] = {}
    for outcome in session.scalars(
        select(models.RiskOutcome).where(models.RiskOutcome.label_version == "v3_bounded")
    ).all():
        details_by_score[outcome.score_id] = outcome.details or {}
    for o in outcomes:
        ts = _as_utc(o.decision_ts)
        if ts is None or o.score_id is None:
            continue
        week_key = _week_start(ts)
        actual = (
            "positive"
            if o.survived and not (o.collapsed or o.rugged)
            else ("negative" if (o.collapsed or o.rugged) else None)
        )
        if actual is None:
            continue
        week = weeks.setdefault(week_key, {})
        details = details_by_score.get(o.score_id)
        if not details:
            continue
        if details.get("ml_prediction") and details.get("ml_risk_band"):
            week.setdefault("ml", []).append(
                (details["ml_risk_band"] in ("GREEN", "YELLOW")) == (actual == "positive")
            )
        if details.get("heuristic_prediction") and details.get("heuristic_band"):
            week.setdefault("heuristic", []).append(
                (details["heuristic_band"] in ("GREEN", "YELLOW")) == (actual == "positive")
            )

    result: dict[str, list[dict[str, Any]]] = {name: [] for name in ("rule", "ml", "heuristic")}
    for week_key, scorer_entries in sorted(weeks.items()):
        for name, correct_flags in scorer_entries.items():
            if len(correct_flags) >= 5:
                acc = sum(correct_flags) / len(correct_flags)
                result.setdefault(name, []).append(
                    {
                        "ts": week_key.isoformat(),
                        "accuracy": acc,
                        "n": len(correct_flags),
                    }
                )
    return result


def classify_regime(session: Session, window_end: datetime) -> str:
    """Deterministic regime rule from the engine's own aggregated market rows.

    Design doc §2.1.4: median cross-asset hourly return over the trailing 72h.
    Prices within each hour bucket are ordered by ts before computing the
    bucket's return.
    """
    start = window_end - timedelta(hours=72)
    rows = session.execute(
        select(
            models.MarketSnapshot.pair_id,
            models.MarketSnapshot.ts,
            models.MarketSnapshot.price_usd,
        )
        .where(
            models.MarketSnapshot.ts >= start,
            models.MarketSnapshot.ts <= window_end,
            models.MarketSnapshot.price_usd.is_not(None),
            models.MarketSnapshot.price_usd > 0,
        )
        .order_by(models.MarketSnapshot.ts)
    ).all()
    if not rows:
        return "unclassified"
    hourly: dict[tuple[object, datetime], list[tuple[datetime, float]]] = {}
    for _, ts, price in rows:
        if price is None:
            continue  # the query already excludes NULL prices; keep the type narrowed
        hour = ts.replace(minute=0, second=0, microsecond=0)
        hourly.setdefault((ts.date(), hour), []).append((ts, float(price)))
    returns: list[float] = []
    for key in sorted(hourly):
        series = sorted(hourly[key], key=lambda pair: pair[0])
        if len(series) < 2:
            continue
        returns.append((series[-1][1] / series[0][1]) - 1.0)
    if not returns:
        return "unclassified"
    median = float(np.median(returns))
    if median > 0.02:
        return "bull"
    if median < -0.02:
        return "bear"
    return "mixed"


def audit_feature_leakage(
    session: Session, outcomes: list[OutcomeRow]
) -> tuple[LeakageReport, int]:
    """Run the point-in-time leakage audit on the engine's own persisted rows.

    Joins Feature rows to each outcome's (asset_id, decision_ts) and scans the
    top-concordance features for values observed AFTER their decision time.
    A feature's missing placeholder is excluded, because its numeric zero is
    not an observed value. Market and contract-derived values use source-row
    availability rather than feature materialization time when they can be
    reproduced from point-in-time inputs.
    Returns (report, n_samples audited).
    """
    samples: list[tuple[int, datetime, float]] = []
    seen: set[tuple[int, datetime]] = set()
    for o in outcomes:
        ts = _as_utc(o.decision_ts)
        if o.asset_id is None or ts is None:
            continue
        key = (o.asset_id, ts)
        if key in seen:
            continue
        seen.add(key)
        samples.append((o.asset_id, ts, 1.0 if (o.collapsed or o.rugged) else 0.0))
    if not samples:
        return LeakageReport(), 0
    rows = session.execute(
        select(
            models.Feature.asset_id,
            models.Feature.decision_ts,
            models.Feature.feature_name,
            models.Feature.feature_value,
            models.Feature.observed_at,
            models.Feature.missing_flag,
        ).where(
            tuple_(models.Feature.asset_id, models.Feature.decision_ts).in_(
                [(asset_id, ts) for asset_id, ts, _ in samples]
            )
        )
    ).all()
    per_feature: dict[str, dict[tuple[int, datetime], tuple[float, datetime | None, bool]]] = {}
    for asset_id, feature_ts, name, value, observed_at, missing in rows:
        per_feature.setdefault(str(name), {})[(int(asset_id), _as_utc(feature_ts))] = (
            float(value) if value is not None else float("nan"),
            _as_utc(observed_at),
            bool(missing),
        )
    source_available_at = _reproducible_source_times(session, samples, per_feature)
    n = len(samples)
    feature_values: dict[str, np.ndarray] = {}
    observed_at_map: dict[str, np.ndarray] = {}
    for name, by_sample in per_feature.items():
        values: list[float] = []
        observed: list[datetime] = []
        for asset_id, ts, _ in samples:
            hit = by_sample.get((asset_id, ts))
            if hit is None:
                values.append(float("nan"))
                observed.append(ts)  # missing feature: no violation possible
            else:
                values.append(float("nan") if hit[2] else hit[0])
                source_ts = source_available_at.get(name, {}).get((asset_id, ts))
                observed.append(
                    source_ts if source_ts is not None else (hit[1] if hit[1] is not None else ts)
                )
        feature_values[name] = np.array(values)
        observed_at_map[name] = np.array(observed, dtype=object)
    decision_arr = np.array([ts for _, ts, _ in samples], dtype=object)
    labels = np.array([label for _, _, label in samples])
    report = check_feature_leakage(feature_values, observed_at_map, decision_arr, labels)
    for name, verified in source_available_at.items():
        entries = per_feature[name]
        non_missing = sum(not row[2] for row in entries.values())
        unverified = non_missing - len(verified)
        if unverified > 0:
            report.source_unverified_values[name] = unverified
    if report.source_unverified_values:
        details = ", ".join(
            f"{name}={count}" for name, count in sorted(report.source_unverified_values.items())
        )
        report.reason = "; ".join(
            part
            for part in (
                report.reason,
                f"source replay could not verify non-missing values: {details}",
            )
            if part
        )
    return report, n


def _reproducible_source_times(
    session: Session,
    samples: list[tuple[int, datetime, float]],
    per_feature: dict[str, dict[tuple[int, datetime], tuple[float, datetime | None, bool]]],
) -> dict[str, dict[tuple[int, datetime], datetime]]:
    """Return source times only when persisted values replay from point-in-time inputs."""
    from features.factory import (
        FeatureFactory,
        FeatureValue,
        _load_pit_market_inputs,
        compute_market_block,
    )

    market_names = {"pair_age_minutes", "volume_acceleration", "liquidity_depth"}
    contract_names = {"suspicious_contract_flags", "deployer_history_available"}
    source_names = market_names | contract_names
    if not source_names.intersection(per_feature):
        return {}
    available: dict[str, dict[tuple[int, datetime], datetime]] = {
        name: {} for name in source_names.intersection(per_feature)
    }
    factory = FeatureFactory()
    for asset_id, decision_ts, _ in samples:
        key = (asset_id, decision_ts)
        candidates = [
            name
            for name in available
            if (hit := per_feature[name].get(key)) is not None and not hit[2]
        ]
        if not candidates:
            continue
        asset = session.get(models.Asset, asset_id)
        if asset is None:
            continue
        market_rows: list[models.MarketSnapshot] = []
        liquidity_rows: list[models.LiquiditySnapshot] = []
        pairs: list[models.Pair] = []
        computed: dict[str, Any] = {}
        if market_names.intersection(candidates):
            pairs, market_rows, liquidity_rows = _load_pit_market_inputs(
                session, asset_id, decision_ts
            )
            computed.update(
                compute_market_block(
                    asset=asset,
                    pairs=pairs,
                    market_rows=market_rows,
                    liquidity_rows=liquidity_rows,
                    decision_ts=decision_ts,
                )
            )
        contracts = []
        flags = []
        if contract_names.intersection(candidates):
            contracts = list(
                session.scalars(
                    select(models.Contract).where(
                        models.Contract.asset_id == asset_id,
                        models.Contract.created_at <= decision_ts,
                        models.Contract.observed_at <= decision_ts,
                    )
                )
            )
            contract_ids = [contract.id for contract in contracts]
            if contract_ids:
                flags = list(
                    session.scalars(
                        select(models.ContractFlag).where(
                            models.ContractFlag.contract_id.in_(contract_ids),
                            models.ContractFlag.observed_at <= decision_ts,
                            models.ContractFlag.ts <= decision_ts,
                        )
                    )
                )
            if "suspicious_contract_flags" in candidates:
                value = factory._contract_flag_count(session, asset_id, decision_ts)
                computed["suspicious_contract_flags"] = FeatureValue(
                    "suspicious_contract_flags",
                    value if value is not None else 0.0,
                    value is None,
                )
            if "deployer_history_available" in candidates:
                value = factory._deployer_history(session, asset_id, decision_ts)
                computed["deployer_history_available"] = FeatureValue(
                    "deployer_history_available",
                    value if value is not None else 0.0,
                    value is None,
                )
        for name in candidates:
            expected = computed.get(name)
            if expected is None:
                continue
            stored = per_feature[name][key][0]
            if expected.missing or not np.isclose(expected.value, stored, rtol=1e-9, atol=1e-9):
                continue
            if name == "pair_age_minutes":
                source_times = [pair.created_at for pair in pairs]
            elif name == "volume_acceleration":
                source_times = [row.observed_at for row in market_rows]
            elif name == "liquidity_depth":
                source_times = [row.observed_at for row in liquidity_rows]
            elif name == "suspicious_contract_flags":
                source_times = [contract.observed_at for contract in contracts]
                source_times.extend(flag.observed_at for flag in flags)
            else:
                source_times = [contract.observed_at for contract in contracts]
            normalized = [_as_utc(value) for value in source_times if value is not None]
            if normalized:
                available[name][key] = max(normalized)
    return available


def run_harness(
    session: Session,
    *,
    git_sha: str = "",
    model_version: str = "",
    min_samples: int = 30,
) -> ValidationReport:
    """Run the full benchmark over the engine's persisted rows (read-only)."""
    outcomes = load_outcomes(session)
    unresolved_outcomes = load_unresolved_outcome_count(session)
    censored_outcomes = session.scalar(
        select(func.count(models.RiskOutcome.id)).where(
            models.RiskOutcome.evaluated_at.is_(None),
            models.RiskOutcome.censored_at.is_not(None),
            models.RiskOutcome.label_version == "v3_bounded",
        )
    )
    v1_resolved = session.scalar(
        select(func.count(models.RiskOutcome.id)).where(
            models.RiskOutcome.evaluated_at.is_not(None),
            models.RiskOutcome.label_version == "v1_unbounded",
        )
    )
    forecasts = load_forecasts(session)
    forecast_outcomes = load_forecast_outcomes(session)
    ensemble_history = load_ensemble_history(session)
    scorer_accuracy_history = load_scorer_accuracy_history(session, outcomes)

    all_ts = [o.decision_ts for o in outcomes]
    cutoff: datetime | None = None
    if all_ts:
        t0 = min(all_ts)
        t1 = max(all_ts)
        window = (t1 - t0).total_seconds() / 3600.0
        if window > 0:
            eval_hours = max(window * EVAL_FRACTION, MIN_EVAL_WINDOW_DAYS * 24)
            cutoff = t1 - timedelta(hours=eval_hours)
            cutoff = _as_utc(cutoff)

    # Walk-forward split with a REAL embargo gap: evaluation = rows after
    # cutoff; embargoed = rows within EMBARGO_HOURS before cutoff, excluded
    # from BOTH sets because their outcome observation windows overlap the
    # eval period; reference = rows at or before cutoff - embargo. The gap
    # (48h >= 24h forward horizon) is the decontamination the partition
    # metadata claims — previously no rows were actually excluded.
    eval_rows: list[OutcomeRow] = []
    embargoed_rows: list[OutcomeRow] = []
    reference_rows: list[OutcomeRow] = []
    if cutoff is None:
        eval_rows = list(outcomes)
    else:
        embargo_floor = cutoff - timedelta(hours=EMBARGO_HOURS)
        for o in outcomes:
            ts = _as_utc(o.decision_ts)
            if ts is None or ts > cutoff:
                eval_rows.append(o)
            elif ts > embargo_floor:
                embargoed_rows.append(o)
            else:
                reference_rows.append(o)
    regime = classify_regime(session, cutoff or datetime.now(UTC))

    report = ValidationReport(
        git_sha=git_sha,
        model_version=model_version,
        database_digest={
            "assets": _count(session, models.Asset),
            "scores": _count(session, models.Score),
            "risk_outcomes": _count(session, models.RiskOutcome),
            "evaluated_outcomes": len(outcomes),
            "forecasts": len(forecasts),
            "labels": _count(session, models.Label),
            "lifecycle_events": _count(session, models.LifecycleEvent),
            "unresolved_outcomes": unresolved_outcomes,
            "resolved_outcomes": len(outcomes),
        },
        partition={
            "cutoff": cutoff.isoformat() if cutoff else None,
            "embargo_hours": EMBARGO_HOURS,
            "purge_hours": PURGE_HOURS,
            "regime": regime,
            "eval_rows": len(eval_rows),
            "embargoed_rows": len(embargoed_rows),
            "reference_rows": len(reference_rows),
        },
        unresolved_outcomes=unresolved_outcomes,
        resolved=len(outcomes),
        censored=int(censored_outcomes or 0),
        retrospective_exploratory={
            "label_version": "v1_unbounded",
            "resolved": int(v1_resolved or 0),
            "text": "no confirmatory claim",
            "note": (
                "v1_unbounded rows are retrospective/exploratory only; they are "
                "excluded from every confirmatory cell."
            ),
        },
    )
    report.notes.append(
        f"resolved={len(outcomes)} censored={int(censored_outcomes or 0)}: "
        "metrics are conditional on resolved outcomes"
    )
    if unresolved_outcomes:
        report.notes.append(
            f"{unresolved_outcomes} unresolved outcome row(s) are past their "
            "observation window with no established label; they are excluded from "
            "every metric and stay unevaluated until the window is backfilled."
        )

    report.cells.extend(evaluate_band_outcomes(eval_rows, regime=regime, min_samples=min_samples))

    # Live-token stratification: the headline cells are dominated by
    # dead-token structure (tokens already dead/collapsed at score time
    # stay dead), so the edge is also measured on live tokens alone — a
    # future verdict cannot be driven by dead tokens alone.  The dead
    # stratum is reported alongside so the contrast is explicit.  Each
    # stratified cell is baselined against its own stratum base rate.
    live_rows = [o for o in eval_rows if (o.phase_at_score or "") in LIVE_PHASES]
    dead_rows = [o for o in eval_rows if (o.phase_at_score or "") in DEAD_PHASES]
    report.cells.extend(
        evaluate_band_outcomes(live_rows, regime=regime, min_samples=min_samples, stratum="live")
    )
    report.cells.extend(
        evaluate_band_outcomes(dead_rows, regime=regime, min_samples=min_samples, stratum="dead")
    )

    # Risk score / exit_risk as collapse discriminators (concordance vs base).
    # Methodology §3.2: ambiguous outcomes (neither collapsed nor survived)
    # are EXCLUDED from binary metrics, never treated as negatives — an
    # unknown outcome is not a survival. The excluded count is reported.
    if eval_rows:
        risk_scores = np.array(
            [o.risk_score if o.risk_score is not None else np.nan for o in eval_rows]
        )
        exit_risks = np.array(
            [o.exit_risk if o.exit_risk is not None else np.nan for o in eval_rows]
        )
        known = np.array(
            [1.0 if (o.collapsed or o.rugged or o.survived) else 0.0 for o in eval_rows]
        ).astype(bool)
        labels = np.array([1.0 if (o.collapsed or o.rugged) else 0.0 for o in eval_rows])
        for name, values in (("risk_score", risk_scores), ("exit_risk", exit_risks)):
            valid = ~np.isnan(values) & known
            excluded = int((~known).sum())
            if int(valid.sum()) < min_samples:
                report.cells.append(
                    _insufficient_cell(
                        name,
                        "concordance",
                        regime,
                        int(valid.sum()),
                        note=f"{excluded} ambiguous outcomes excluded",
                    )
                )
                continue
            conc = concordance_index(values[valid], labels[valid])
            ci = _bootstrap_ci(values[valid], labels[valid], concordance_index)
            report.cells.append(
                MetricCell(
                    output=name,
                    metric="concordance",
                    regime=regime,
                    n=int(valid.sum()),
                    value=conc,
                    ci_low=ci[0],
                    ci_high=ci[1],
                    baseline_value=0.5,
                    baseline_ci_low=0.5,
                    baseline_ci_high=0.5,
                    verdict=verdict(
                        conc,
                        WilsonCI(*ci),
                        0.5,
                        WilsonCI(0.5, 0.5),
                        higher_is_better=True,
                        n=int(valid.sum()),
                    ),
                    note=f"{excluded} ambiguous outcomes excluded (never treated as negative)",
                )
            )

    # Confidence / uncertainty calibration cells (methodology §2.2).
    report.cells.extend(
        evaluate_confidence_uncertainty(
            eval_rows,
            regime=regime,
            min_samples=min_samples,
        )
    )

    # Forecast probability block. A forecast with NO matching label row is
    # EXCLUDED, never treated as a negative (methodology §3.2) — the label
    # tuple is unpacked so a non-collapse (False, ...) is not misread as truthy.
    # The join key is the forecast's SOURCE FEATURE timestamp (the ts the
    # forecast was made from), because labels are keyed by their own ts —
    # joining on the training-run timestamp would (almost) never match.
    matched_probs: list[float] = []
    matched_labels: list[float] = []
    for f in forecasts:
        key_ts = _as_utc(f.source_features_ts)
        if key_ts is None:
            continue  # no source timestamp: cannot match a label row
        outcome = forecast_outcomes.get((f.asset_id, key_ts))
        if outcome is None:
            continue  # unobserved — excluded
        collapsed, _ = outcome
        matched_probs.append(f.p_collapse_24h)
        matched_labels.append(1.0 if collapsed else 0.0)
    if matched_probs:
        report.cells.extend(
            evaluate_probabilities(
                np.array(matched_probs),
                np.array(matched_labels),
                output="collapse_probability_24h",
                regime=regime,
                min_samples=min_samples,
            )
        )
    else:
        report.cells.append(
            _insufficient_cell(
                "collapse_probability_24h",
                "brier",
                regime,
                len(matched_probs),
                note="no Forecast rows persisted — model never trained (insufficient labels)",
            )
        )
        report.cells.append(
            _insufficient_cell(
                "collapse_probability_24h",
                "ece",
                regime,
                len(matched_probs),
                note="no Forecast rows persisted",
            )
        )
        report.cells.append(
            _insufficient_cell(
                "collapse_probability_24h",
                "precision@10",
                regime,
                len(matched_probs),
                note="no Forecast rows persisted",
            )
        )
        report.cells.append(
            _insufficient_cell(
                "collapse_probability_24h",
                "concordance",
                regime,
                len(matched_probs),
                note="no Forecast rows persisted",
            )
        )

    report.cells.extend(
        evaluate_hazard(
            forecasts,
            forecast_outcomes,
            regime=regime,
            min_samples=min_samples,
        )
    )

    # Entry-mission cells (Task 6, spec §6). Entry rows are split with the
    # SAME cutoff and 48h embargo as the outcome rows above — never a
    # separate split. Three-window discipline (spec §2, principle 7):
    # feature screening (Tasks 5/13/14) happens on window A, strictly
    # before the reference side (window B: baselines and any weight
    # fitting); the embargoed eval tail is window C, the only window
    # entry_validated reads. Screening outputs enter here only as frozen
    # component lists, never as numbers fitted on C; where a screening
    # task tests more than one feature, its own report applies
    # Benjamini-Hochberg across the screened set and quotes corrected
    # q-values (the screening scripts own the correction).
    entry_rows = load_entry_rows(session)
    eval_entry_rows: list[EntryRow] = []
    if cutoff is None:
        eval_entry_rows = list(entry_rows)
    else:
        for row in entry_rows:
            ts = _as_utc(row.decision_ts)
            if ts is None or ts > cutoff:
                eval_entry_rows.append(row)
            # Rows at or before the cutoff — the 48h embargo band and
            # the reference side (window B) alike — are excluded from
            # the eval cells by construction of this split.
    report.notes.append(
        "entry windows: A = feature screening (frozen component lists; "
        "Benjamini-Hochberg q-values owned by the screening scripts), "
        "B = reference side of the embargoed split, C = embargoed eval "
        "tail (the only window entry_validated reads)"
    )
    # Forecast-freshness precondition (spec §3/§10): stale forecasts are
    # never evaluated as if current. On staleness the only entry-side
    # output is the freshness cell, and entry_validated reads False.
    settings = get_settings()
    newest_forecast_ts = max(
        (f.decision_ts for f in forecasts if f.decision_ts is not None), default=None
    )
    forecast_stale = newest_forecast_ts is None or (
        datetime.now(UTC) - newest_forecast_ts
        > timedelta(hours=settings.forecast_max_staleness_hours)
    )
    if forecast_stale:
        if newest_forecast_ts is None:
            freshness_note = "stale forecasts: no forecast rows persisted"
        else:
            freshness_note = (
                "stale forecasts: newest forecast decision_ts "
                f"{newest_forecast_ts.isoformat()} is older than "
                "forecast_max_staleness_hours"
            )
        report.cells.append(
            MetricCell(
                output="forecasts",
                metric="freshness",
                regime=regime,
                n=len(forecasts),
                value=float("nan"),
                ci_low=float("nan"),
                ci_high=float("nan"),
                baseline_value=float(settings.forecast_max_staleness_hours),
                baseline_ci_low=float(settings.forecast_max_staleness_hours),
                baseline_ci_high=float(settings.forecast_max_staleness_hours),
                verdict="insufficient_data",
                note=freshness_note,
            )
        )
        report.notes.append(f"entry evaluation skipped: {freshness_note}")
    else:
        report.cells.extend(evaluate_entry(eval_entry_rows, regime=regime, min_samples=min_samples))

    # Ensemble weight-tracking meta-metric (harness-computed accuracy series).
    tracking = ensemble_weight_tracking(ensemble_history, scorer_accuracy_history)
    for name, corr in tracking.items():
        n_hist = len(scorer_accuracy_history.get(name, []))
        report.cells.append(
            MetricCell(
                output=f"ensemble_weight:{name}",
                metric="weight_accuracy_corr",
                regime=regime,
                n=n_hist,
                value=corr if np.isfinite(corr) else float("nan"),
                ci_low=float("nan"),
                ci_high=float("nan"),
                baseline_value=0.0,
                baseline_ci_low=-0.2,
                baseline_ci_high=0.2,
                verdict=(
                    "insufficient_data"
                    if (not np.isfinite(corr) or n_hist < 3)
                    else verdict(
                        corr,
                        WilsonCI(corr - 0.1, corr + 0.1),
                        0.0,
                        WilsonCI(-0.2, 0.2),
                        higher_is_better=True,
                        n=n_hist,
                    )
                ),
                note="positive = weight tracks harness-computed accuracy",
            )
        )
    if not ensemble_history:
        report.cells.append(
            _insufficient_cell(
                "ensemble_weight",
                "weight_accuracy_corr",
                regime,
                0,
                note="no EnsembleState weight history persisted",
            )
        )

    # Feature leakage audit on real engine data: the point-in-time leak
    # detector scans persisted Feature rows joined to the eval outcomes. Each
    # suspected feature lands in suspicious_results and the summary cell
    # carries leakage_suspected=True, so the report cannot be read as genuine
    # without the cross-check.
    leak_report, leak_n = audit_feature_leakage(session, eval_rows)
    report.cells.append(
        MetricCell(
            output="feature_leakage",
            metric="suspected_features",
            regime=regime,
            n=leak_n,
            value=float(len(leak_report.suspected)),
            ci_low=float("nan"),
            ci_high=float("nan"),
            baseline_value=0.0,
            baseline_ci_low=0.0,
            baseline_ci_high=0.0,
            verdict=(
                "insufficient_data"
                if leak_n < min_samples
                else (
                    "worse"
                    if leak_report.flagged
                    else ("insufficient_data" if leak_report.source_unverified_values else "better")
                )
            ),
            leakage_suspected=leak_report.flagged,
            note=(
                leak_report.reason
                if leak_report.flagged or leak_report.source_unverified_values
                else f"point-in-time audit over {leak_n} outcome samples: no suspected leaks"
            ),
        )
    )
    for suspect in leak_report.suspected:
        report.suspicious_results.append(
            {
                "output": f"feature:{suspect.name}",
                "metric": "point_in_time_leak",
                "regime": regime,
                "n": suspect.n_non_missing,
                "value": round(suspect.concordance, 4),
                "reason": (
                    f"concordance {suspect.concordance:.4f} with "
                    f"{suspect.violations} values observed after their decision "
                    f"time (availability {suspect.availability_ratio:.4f})"
                ),
            }
        )

    # Suspicious-good cross-check (Stage 4.4): only higher-is-better metrics
    # where >= 0.95 is genuinely near-perfect (precision, concordance, c_index)
    # are flagged — a brier/ordinal-distance of 0.95 is terrible, not suspicious.
    higher_is_better_metrics = {
        "precision@5",
        "precision@10",
        "precision@25",
        "concordance",
        "c_index",
        "error_correlation",
        "weight_accuracy_corr",
    }
    for cell in report.cells:
        if cell.verdict == "insufficient_data" or cell.n < min_samples:
            continue
        if cell.metric not in higher_is_better_metrics:
            continue
        if np.isfinite(cell.value) and cell.value >= 0.95:
            report.suspicious_results.append(
                {
                    "output": cell.output,
                    "metric": cell.metric,
                    "regime": cell.regime,
                    "n": cell.n,
                    "value": round(cell.value, 4),
                    "reason": (
                        "near-perfect score (>=0.95): cross-check against the "
                        "point-in-time leakage pattern before reporting as genuine"
                    ),
                }
            )
            cell.leakage_suspected = True
            cell.note = (cell.note + " | SUSPICIOUS-GOOD: verify no point-in-time leak").strip(" |")

    return report


def _insufficient_cell(output: str, metric: str, regime: str, n: int, note: str = "") -> MetricCell:
    return MetricCell(
        output=output,
        metric=metric,
        regime=regime,
        n=n,
        value=float("nan"),
        ci_low=float("nan"),
        ci_high=float("nan"),
        baseline_value=float("nan"),
        baseline_ci_low=float("nan"),
        baseline_ci_high=float("nan"),
        verdict="insufficient_data",
        note=note,
    )


def _safe_corrcoef(x: np.ndarray, y: np.ndarray) -> float:
    """Correlation that returns nan on zero-variance input instead of warning."""
    with np.errstate(invalid="ignore", divide="ignore"):
        result = np.corrcoef(x, y)[0, 1]
    return float(result) if np.isfinite(result) else float("nan")


def _count(session: Session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)
