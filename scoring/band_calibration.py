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
    """Current band→survival map: the refreshed calibration, else the fallback."""
    with _cache_lock:
        cached = _cache
    return dict(cached) if cached is not None else dict(FALLBACK_BAND_CONFIDENCE)


def set_band_confidence(mapping: dict[RiskBand, float] | None) -> None:
    """Install a calibrated map (``None`` restores the fallback). Used by refresh/tests."""
    global _cache
    with _cache_lock:
        _cache = dict(mapping) if mapping is not None else None


def _survival_by_band(session: Session) -> dict[RiskBand, tuple[int, float]]:
    """``{band: (evaluated, survival_rate)}`` from persisted, evaluated outcomes."""
    rows = session.execute(
        select(
            models.RiskOutcome.risk_band,
            func.count(models.RiskOutcome.id),
            func.sum(func.cast(models.RiskOutcome.survived, Integer)),
        )
        .where(models.RiskOutcome.evaluated_at.is_not(None))
        .group_by(models.RiskOutcome.risk_band)
    ).all()
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


def refresh_band_calibration(session: Session, *, min_samples: int | None = None) -> dict[str, Any]:
    """Recompute the band map from persisted outcomes and record its status.

    Returns a summary dict (``validated``, per-band samples/rates, the map in
    use). Safe to call every data-lake pass: it is two aggregate queries.
    """
    minimum = (
        min_samples if min_samples is not None else get_settings().risk_confidence_band_min_samples
    )
    observed = _survival_by_band(session)
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
