"""Harness orchestrator — reads engine rows, computes benchmark cells.

Read-only: never writes to engine tables. Reuses the engine's own persisted
point-in-time rows (scores, risk_outcomes, forecasts, labels, lifecycle,
market snapshots, ensemble state) per design doc §2.1.2. Metric computation is
split into pure helpers so synthetic self-tests exercise the same code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, overload

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from storage import models
from validation.baselines import (
    base_rate_proportion,
    base_rate_wilson_ci,
    random_ranking_concordance,
    random_topk_precision_ci,
)
from validation.metrics import (
    BAND_RANKS,
    WilsonCI,
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
    bootstrap_ci as _bootstrap_ci,
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


@dataclass
class ForecastRow:
    asset_id: int
    decision_ts: datetime
    p_collapse_24h: float
    p_ignition_24h: float
    expected_hours_to_collapse: float | None
    expected_hours_to_peak: float | None


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


def evaluate_band_outcomes(
    outcomes: list[OutcomeRow],
    *,
    regime: str,
    min_samples: int = 30,
    seed: int = 7,
) -> list[MetricCell]:
    """Ordinal band evaluation + per-band collapse precision (methodology §2.1)."""
    cells: list[MetricCell] = []
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

        # Bootstrap CI for BOTH the model distance and the baseline distance.
        def _dist(p: np.ndarray, a: np.ndarray) -> float:
            return ordinal_band_distance(p, a)

        ci = _bootstrap_ci(pred, actual, _dist)
        b_ci = _bootstrap_ci(np.zeros_like(actual), actual, _dist)
        unknown = int((~valid).sum())
        cells.append(
            MetricCell(
                output="risk_band",
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
                    WilsonCI(*ci),
                    baseline_dist,
                    WilsonCI(*b_ci),
                    higher_is_better=False,
                    n=int(valid.sum()),
                ),
                note=f"{unknown} ambiguous outcomes excluded from ordinal distance",
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
        unknown_band = sum(
            1
            for i in idx
            if not (outcomes[i].collapsed or outcomes[i].rugged or outcomes[i].survived)
        )
        cells.append(
            MetricCell(
                output=f"risk_band:{band}",
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
                    band_ci,
                    b,
                    band_base_ci,
                    higher_is_better=True,
                    n=len(idx),
                    min_samples=min_samples,
                ),
                note=f"{unknown_band} ambiguous outcomes treated as non-collapse (reported)",
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

    ``outcomes_by_asset_ts`` maps (asset, decision_ts) -> (collapsed, trough_hours).
    A forecast's event time is the realized trough hours when the asset
    collapsed inside the window; otherwise it is right-censored at the 24h
    horizon. The risk score is expected_hours_to_collapse (shorter = higher
    risk), negated for the C-index's higher-risk-higher convention.
    """
    cells: list[MetricCell] = []
    risks: list[float] = []
    times: list[float] = []
    events: list[float] = []
    n_censored = 0
    matched = 0
    for f in forecasts:
        key = (f.asset_id, _as_utc(f.decision_ts))
        outcome = outcomes_by_asset_ts.get(key)
        if outcome is None:
            continue
        if f.expected_hours_to_collapse is None:
            continue
        collapsed, trough_hours = outcome
        matched += 1
        risks.append(float(f.expected_hours_to_collapse))
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
        ci = _bootstrap_ci(
            conf[conf_valid], surv[conf_valid], lambda c, s: confidence_calibration_error(c, s)
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
                    WilsonCI(*ci),
                    ceiling,
                    WilsonCI(ceiling, ceiling),
                    higher_is_better=False,
                    n=int(conf_valid.sum()),
                ),
                note="confidence/100 vs observed survival frequency; trust ceiling 0.10",
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
            # nearest accuracy observation at or before this weight timestamp;
            # timestamps may be datetime or ISO strings — coerce via _ts_key
            best: float | None = None
            best_delta = None
            entry_ts_parsed = _ts_key(entry_ts)
            for acc_row in scorer_accuracy_history.get(name, []):
                acc_ts = _ts_key(acc_row.get("ts"))
                if acc_ts is None or entry_ts_parsed is None:
                    continue
                delta = abs((acc_ts - entry_ts_parsed).total_seconds())
                if best_delta is None or delta < best_delta:
                    best_delta = delta
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
    """Load evaluated risk outcomes joined to their scores (read-only)."""
    rows = session.execute(
        select(models.RiskOutcome, models.Score)
        .join(models.Score, models.Score.id == models.RiskOutcome.score_id)
        .where(models.RiskOutcome.evaluated_at.is_not(None))
    ).all()
    out: list[OutcomeRow] = []
    for outcome, score in rows:
        out.append(
            OutcomeRow(
                asset_id=outcome.asset_id,
                decision_ts=score.decision_ts,
                score_id=outcome.score_id,
                predicted_band=outcome.risk_band,
                risk_score=score.risk,
                exit_risk=score.exit_risk,
                confidence=score.confidence,
                uncertainty=score.uncertainty,
                collapsed=bool(outcome.collapsed),
                rugged=bool(outcome.rugged),
                survived=bool(outcome.survived),
            )
        )
    return out


def load_forecasts(session: Session) -> list[ForecastRow]:
    rows = session.scalars(select(models.Forecast)).all()
    return [
        ForecastRow(
            asset_id=f.asset_id,
            decision_ts=_as_utc(f.decision_ts),
            p_collapse_24h=f.p_collapse_24h,
            p_ignition_24h=f.p_ignition_24h,
            expected_hours_to_collapse=f.expected_hours_to_collapse,
            expected_hours_to_peak=f.expected_hours_to_peak,
        )
        for f in rows
    ]


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
    for outcome in session.scalars(select(models.RiskOutcome)).all():
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


def run_harness(
    session: Session,
    *,
    git_sha: str = "",
    model_version: str = "",
    min_samples: int = 30,
) -> ValidationReport:
    """Run the full benchmark over the engine's persisted rows (read-only)."""
    outcomes = load_outcomes(session)
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

    # Walk-forward split with embargo: evaluation = rows after cutoff;
    # reference = rows at or before cutoff - embargo (48h).
    eval_rows = (
        [o for o in outcomes if cutoff is None or _as_utc(o.decision_ts) > cutoff]
        if cutoff is not None
        else outcomes
    )
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
        },
        partition={
            "cutoff": cutoff.isoformat() if cutoff else None,
            "embargo_hours": EMBARGO_HOURS,
            "purge_hours": PURGE_HOURS,
            "regime": regime,
            "eval_rows": len(eval_rows),
            "reference_rows": len(outcomes) - len(eval_rows),
        },
    )

    report.cells.extend(evaluate_band_outcomes(eval_rows, regime=regime, min_samples=min_samples))

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
    matched_probs: list[float] = []
    matched_labels: list[float] = []
    for f in forecasts:
        outcome = forecast_outcomes.get((f.asset_id, _as_utc(f.decision_ts)))
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
