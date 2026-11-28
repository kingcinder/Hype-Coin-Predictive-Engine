"""Band → survival confidence, calibrated from persisted outcomes.

Confidence used to be a hardcoded ``_BAND_CONFIDENCE`` table (92/72/48/22/5)
with a comment claiming it came from "the observed collapse rates per band".
Nothing recomputed it, nothing checked it, and nothing said whether it still
held: the numbers were an assertion, not a measurement.

This module derives the table from the outcomes the engine already records —
``RiskOutcome.survived`` per ``risk_band`` for evaluated rows — and reports
whether the calibration is *validated*:

* every band has at least ``risk_confidence_band_min_samples`` evaluated
  outcomes, and
* survival is monotone across the band order (GREEN ≥ YELLOW ≥ ORANGE ≥ RED ≥
  BLACK), i.e. confidence still ranks risk correctly.

Until both hold, the fallback table is used and the state stays unvalidated, so
every read path can say so instead of presenting the numbers as validated. The
result is persisted as a ``confidence_band_calibration`` SystemHealth row (the
API reads that row for its ``score_validation`` flags) and cached in-process for
the scoring hot path.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from common.config import get_settings
from common.enums import RiskBand
from common.logging import get_logger
from storage import models
from storage.repository import record_health

log = get_logger(__name__)

COMPONENT = "confidence_band_calibration"

#: Fallback survival probabilities, used until the persisted outcomes validate.
#: Deliberately conservative (never claims certainty) and documented as unvalidated.
FALLBACK_BAND_CONFIDENCE: dict[RiskBand, float] = {
    RiskBand.GREEN: 92.0,
    RiskBand.YELLOW: 72.0,
    RiskBand.ORANGE: 48.0,
    RiskBand.RED: 22.0,
    RiskBand.BLACK: 5.0,
}

_BAND_ORDER: tuple[RiskBand, ...] = (
    RiskBand.GREEN,
    RiskBand.YELLOW,
    RiskBand.ORANGE,
    RiskBand.RED,
    RiskBand.BLACK,
)

_cache: dict[RiskBand, float] | None = None
_cache_lock = threading.Lock()


def band_confidence_map() -> dict[RiskBand, float]:
    """Current band-to-survival map: the refreshed calibration, else the fallback."""
    with _cache_lock:
        return dict(_cache) if _cache is not None else dict(FALLBACK_BAND_CONFIDENCE)


def band_confidence_source() -> str:
    """Where the current map came from: calibrated or fallback.

    Thread-safe: reads the cache under the same lock that installs it.
    Callers that stamp a Score or API row with this value must call it at
    scoring time, next to band_confidence_map(), or the stamp can disagree
    with the map that produced the confidence.
    """
    with _cache_lock:
        return "calibrated" if _cache is not None else "fallback"


def set_band_confidence(mapping: dict[RiskBand, float] | None) -> None:
    """Install a calibrated map (``None`` restores the fallback). Used by refresh/tests."""
    global _cache
    with _cache_lock:
        _cache = dict(mapping) if mapping is not None else None


def _survival_by_band(session: Session, *, cutoff=None) -> dict[RiskBand, tuple[int, float]]:
    """``{band: (evaluated, survival_rate)}`` from persisted, evaluated outcomes."""
    query = select(
        models.RiskOutcome.risk_band,
        func.count(models.RiskOutcome.id),
        func.sum(func.cast(models.RiskOutcome.survived, Integer)),
    ).where(models.RiskOutcome.evaluated_at.is_not(None))
    if cutoff is not None:
        query = query.where(models.RiskOutcome.scored_at < cutoff)
    rows = session.execute(query.group_by(models.RiskOutcome.risk_band)).all()
    out: dict[RiskBand, tuple[int, float]] = {}
    for band_name, total, survived in rows:
        try:
            band = RiskBand(str(band_name))
        except ValueError:
            continue  # an unknown band label cannot calibrate anything
        count = int(total or 0)
        if count == 0:
            continue
        out[band] = (count, float(survived or 0) / count)
    return out


def calibration_cutoff(session: Session):
    """Walk-forward cutoff for calibration, mirroring validation/harness.py.

    cutoff = latest evaluated scored_at minus max(30 percent of the scored
    window, 7 days). Outcomes before it calibrate the map; outcomes at or
    after it are the evaluation set. Returns None when the evaluated
    outcomes do not span a usable window, in which case calibration must
    stay unvalidated rather than fall back to fitting the test set.
    """
    from datetime import timedelta

    rows = session.execute(
        select(
            func.min(models.RiskOutcome.scored_at), func.max(models.RiskOutcome.scored_at)
        ).where(models.RiskOutcome.evaluated_at.is_not(None))
    ).one()
    t0, t1 = rows
    if t0 is None or t1 is None or t1 <= t0:
        return None
    window_hours = (t1 - t0).total_seconds() / 3600.0
    eval_hours = max(window_hours * 0.30, 7 * 24)
    return t1 - timedelta(hours=eval_hours)


def refresh_band_calibration(
    session: Session, *, min_samples: int | None = None, cutoff=None
) -> dict[str, Any]:
    """Recompute the band map from persisted outcomes and record its status.

    Returns a summary dict (validated, per-band samples/rates, the map in
    use). Safe to call every data-lake pass: it is two aggregate queries.

    Leakage rule: calibrating on the same outcomes the validation harness
    evaluates is leakage - the calibrated map would be fit to its own test
    set. Production callers must pass cutoff (see calibration_cutoff):
    only outcomes scored before the cutoff calibrate the map, outcomes at
    or after it are the evaluation set. cutoff=None keeps the old
    all-outcomes behaviour for tests and backfills only, and must never
    be used to claim a validated calibration on live data.
    """
    minimum = (
        min_samples if min_samples is not None else get_settings().risk_confidence_band_min_samples
    )
    observed = _survival_by_band(session, cutoff=cutoff)
    samples = {band.value: count for band, (count, _) in observed.items()}
    rates = {band.value: round(rate, 4) for band, (_, rate) in observed.items()}

    enough = all(observed.get(band, (0, 0.0))[0] >= minimum for band in _BAND_ORDER)
    ordered = all(
        observed.get(upper, (0, 0.0))[1] >= observed.get(lower, (0, 0.0))[1]
        for upper, lower in zip(_BAND_ORDER, _BAND_ORDER[1:], strict=False)
    )
    validated = bool(enough and ordered)

    if validated:
        calibrated = {
            band: round(min(95.0, max(5.0, observed[band][1] * 100.0)), 2) for band in _BAND_ORDER
        }
        set_band_confidence(calibrated)
    else:
        calibrated = dict(FALLBACK_BAND_CONFIDENCE)
        set_band_confidence(None)

    summary: dict[str, Any] = {
        "cutoff": cutoff.isoformat() if cutoff is not None else None,
        "validated": validated,
        "min_samples": minimum,
        "samples": samples,
        "survival_rate": rates,
        "band_confidence": {band.value: value for band, value in calibrated.items()},
        "reason": (
            "validated: every band has ≥ min samples and survival is monotone"
            if validated
            else "unvalidated: falling back to the conservative defaults"
        ),
    }
    record_health(
        session,
        component=COMPONENT,
        state="ok" if validated else "yellow",
        message=(f"validated={validated} min_samples={minimum} samples={samples} survival={rates}"),
    )
    log.info("band_confidence_refreshed", **{k: summary[k] for k in ("validated", "samples")})
    return summary


# ---------------------------------------------------------------------------
# Phase-conditioned confidence calibration (added 2026-11-20, v4 improvement)
#
# Diagnosis (validation-2026-11-20-v3): confidence ECE was 0.1331 against a
# 0.10 ceiling. The dominant term was lifecycle_phase at score time — 77.5%
# of evaluated rows were already in the dead/collapse phase, and survival
# within a (risk band, lifecycle phase) cell is nearly deterministic
# (dead/collapse -> 0% survival; ignition/saturation -> 94-100%). The old
# confidence eroded a band-level fallback table with coverage/flag/data-layer
# penalties (up to ~40 points), which measure evidence completeness, not
# survival probability — so the most predictable rows (dead tokens) got
# mid-range confidence and dominated the calibration error.
#
# Fix: confidence becomes the calibrated survival probability conditioned on
# (risk band, lifecycle phase at score time), fit ONLY on v3_bounded
# evaluated outcomes at or before an explicit cutoff (the harness reference
# set — out-of-sample by construction; the harness measures the result on
# the post-cutoff eval set). Evidence completeness moves out of confidence
# and into uncertainty, where it belongs (see scoring/formulas.py).
#
# Phase ranks are the numeric values from pump_physics phase_rank
# (SEEDING 0, IGNITION 1, PARABOLIC 2, SATURATION 3, COLLAPSE 4,
# DEAD/RUGGED/SURVIVOR 5) — the same values stored in Feature rows as
# feature_name="lifecycle_phase".
# ---------------------------------------------------------------------------

_PHASE_CONFIDENCE_MIN_SAMPLES = 10
_PHASE_CONFIDENCE_EMBARGO_HOURS = 48
_phase_confidence_cache: dict[tuple[RiskBand, int], float] | None = None
_phase_confidence_lock = threading.Lock()


def phase_confidence_map() -> dict[tuple[RiskBand, int], float] | None:
    """Return the installed (band, phase-rank) -> survival-% map, or None."""
    with _phase_confidence_lock:
        return dict(_phase_confidence_cache) if _phase_confidence_cache is not None else None


def set_phase_confidence(mapping: dict[tuple[RiskBand, int], float] | None) -> None:
    """Install (or clear, with None) the phase-conditioned confidence map."""
    global _phase_confidence_cache
    with _phase_confidence_lock:
        _phase_confidence_cache = dict(mapping) if mapping is not None else None


def _calibrated_survival(survived: int, total: int) -> float:
    """Survival rate as a 0-100 confidence value, clamped to [1, 99].

    No Laplace smoothing: the clamp alone keeps a 0/n cell at 1% (not 0%,
    acknowledging the finite sample) and an n/n cell at 99%. A +1/+2 prior
    would put a 0/10 cell at 8.3% — a large error on the dead-phase cells,
    which are the majority of the eval set.
    """
    if total <= 0:
        return 50.0
    return float(min(99.0, max(1.0, 100.0 * survived / total)))


def refresh_phase_confidence(
    session: Session,
    *,
    cutoff: datetime | None = None,
    label_version: str = "v3_bounded",
    min_samples: int = _PHASE_CONFIDENCE_MIN_SAMPLES,
) -> dict[str, Any]:
    """Fit the (risk band, lifecycle phase) -> survival map on the reference set.

    The reference set is ``label_version`` *resolved* evaluated outcomes
    (``survived`` or ``collapsed`` — ambiguous rows, neither flag set, are
    excluded, matching the harness confidence cell) with ``scored_at`` at or
    before ``cutoff`` minus a 48h embargo (mirroring the harness partition
    in ``calibration_cutoff``). Only evaluated rows count
    (``evaluated_at`` set); censored/unevaluated rows are excluded.

    Fallback chain per (band, phase) cell, first level with >= min_samples:
    the cell itself -> the phase marginal (all bands) -> the band marginal
    (all phases) -> the static FALLBACK_BAND_CONFIDENCE table. The map is
    built for all 30 (band, phase) pairs so a lookup never misses.

    The fitted map is installed in-process (``set_phase_confidence``) and a
    summary is recorded in SystemHealth under
    ``phase_confidence_calibration``. Returns the summary dict.
    """
    from datetime import timedelta

    if cutoff is None:
        cutoff = calibration_cutoff(session)
    reference_end = cutoff - timedelta(hours=_PHASE_CONFIDENCE_EMBARGO_HOURS)

    rows = (
        session.query(
            models.RiskOutcome.risk_band,
            models.Feature.feature_value,
            models.RiskOutcome.survived,
            models.RiskOutcome.collapsed,
        )
        .join(
            models.Feature,
            (models.Feature.asset_id == models.RiskOutcome.asset_id)
            & (models.Feature.decision_ts == models.RiskOutcome.scored_at)
            & (models.Feature.feature_name == "lifecycle_phase")
            & (models.Feature.missing_flag.is_(False)),
        )
        .filter(
            models.RiskOutcome.evaluated_at.isnot(None),
            models.RiskOutcome.scored_at <= reference_end,
            models.RiskOutcome.label_version == label_version,
            (models.RiskOutcome.survived.is_(True) | models.RiskOutcome.collapsed.is_(True)),
        )
        .all()
    )

    cells: dict[tuple[RiskBand, int], list[int]] = {}
    phase_totals: dict[int, list[int]] = {}
    band_totals: dict[RiskBand, list[int]] = {}
    skipped = 0
    for band_raw, phase_raw, survived_flag, _collapsed_flag in rows:
        try:
            band = RiskBand(band_raw)
            phase = int(float(phase_raw))
        except (TypeError, ValueError):
            skipped += 1
            continue
        survived = 1 if survived_flag else 0
        for bucket in (
            cells.setdefault((band, phase), [0, 0]),
            phase_totals.setdefault(phase, [0, 0]),
            band_totals.setdefault(band, [0, 0]),
        ):
            bucket[0] += survived
            bucket[1] += 1

    def _level_value(band: RiskBand, phase: int) -> tuple[float, str]:
        cell = cells.get((band, phase))
        if cell is not None and cell[1] >= min_samples:
            return _calibrated_survival(cell[0], cell[1]), "cell"
        phase_bucket = phase_totals.get(phase)
        if phase_bucket is not None and phase_bucket[1] >= min_samples:
            return _calibrated_survival(phase_bucket[0], phase_bucket[1]), "phase"
        band_bucket = band_totals.get(band)
        if band_bucket is not None and band_bucket[1] >= min_samples:
            return _calibrated_survival(band_bucket[0], band_bucket[1]), "band"
        return float(FALLBACK_BAND_CONFIDENCE[band]), "fallback"

    mapping: dict[tuple[RiskBand, int], float] = {}
    provenance: dict[str, dict[str, Any]] = {}
    for band in RiskBand:
        for phase in range(6):
            value, level = _level_value(band, phase)
            mapping[(band, phase)] = value
            cell = cells.get((band, phase))
            provenance[f"{band.value}:{phase}"] = {
                "confidence": value,
                "level": level,
                "cell_n": cell[1] if cell else 0,
                "cell_survived": cell[0] if cell else 0,
            }

    set_phase_confidence(mapping)
    total_rows = sum(bucket[1] for bucket in band_totals.values())
    summary: dict[str, Any] = {
        "version": "phase-v1-20261120",
        "cutoff": cutoff.isoformat(),
        "reference_end": reference_end.isoformat(),
        "label_version": label_version,
        "min_samples": min_samples,
        "reference_rows": total_rows,
        "skipped_rows": skipped,
        "cells": provenance,
    }
    record_health(
        session,
        component="phase_confidence_calibration",
        state="ok",
        message=(
            f"version={summary['version']} label_version={label_version} "
            f"reference_rows={total_rows} cutoff={cutoff.isoformat()} "
            f"reference_end={reference_end.isoformat()}"
        ),
    )
    return summary
