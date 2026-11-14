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
