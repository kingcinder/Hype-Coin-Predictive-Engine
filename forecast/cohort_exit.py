"""Cohort-conditional second-stage exit model (Task 17, spec §5/§8).

The forecast engine's phase-conditioned peak hazards are fit on every
labeled decision point, so their curves are dominated by the
pre-ignition mass: rows from assets that never pumped teach the
"peak hazard" what unignited coins do. The exit stack's second stage
refits the same machinery on the IGNITED cohort only — decision
points at or after the asset's IGNITION transition, read off the
sample's own ``lifecycle_phase`` feature (the state machine's record
of where the asset stood at decision time; a missing value is SEEDING
by the state machine's own definition, and terminal exits are not
live post-ignition phases — an asset can jump SEEDING -> DEAD without
ever igniting). Post-ignition rows are the only rows whose time-to-
peak distribution describes a position the exit policy actually holds.

Consumption: :func:`harvest_window_closing_hazard` reads one phase's
survival curve as the discrete hazard that the position's peak lands
in the next hour — the structural-confirmation (completion) vote the
exit policy weighs alongside failed-retest structure at
``Settings.cohort_hazard_complete_threshold``. A phase with no cohort
fit scores 0.0: no second-stage evidence, no vote — and the vote
never fires an exit alone.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from common.config import Settings
from common.time import utc_now
from forecast.engine import ForecastEngine, _phase_slug
from forecast.hazard import HazardFit

# Lifecycle phases at or beyond IGNITION, excluding the terminal
# exits: live phases a held position can actually be in.
_COHORT_PHASE_SLUGS = frozenset({"ignition", "parabolic", "saturation", "collapse"})


def fit_cohort_peak_hazards(session: Session, *, settings: Settings) -> dict[str, HazardFit]:
    """Phase-conditioned peak hazards refit on the IGNITED cohort
    only (decision points at/after the asset's IGNITION transition),
    reusing forecast.hazard.DiscreteHazardModel and the phase
    buckets of forecast.engine._fit_phase_peak_hazards. Fitting on
    the full population is the defect this stage exists to remove:
    pre-ignition rows teach the hazard what unignited coins do."""
    engine = ForecastEngine()
    engine.settings = settings
    samples = engine._collect_samples(session, utc_now())
    cohort = [
        sample
        for sample in samples
        if _phase_slug(sample.features.get("lifecycle_phase")) in _COHORT_PHASE_SLUGS
    ]
    return engine._fit_phase_peak_hazards(session, cohort)


def harvest_window_closing_hazard(
    model: dict[str, HazardFit], *, phase: str, hours_in_position: float
) -> float:
    """Discrete closing hazard for ``phase`` at ``hours_in_position``:
    ``(S(h) - S(h+1)) / S(h)`` off the cohort fit's survival curve —
    the probability the position's peak lands in the next hour, given
    it has not landed yet. Hours beyond the fitted window read the
    last fitted bin. A phase with no cohort fit (seeding, terminal, or
    a post-ignition phase with no cohort samples) and a fit with no
    learnable curve both score 0.0: no second-stage score. Survival
    already exhausted means the peak has certainly passed: 1.0."""
    fit = model.get(phase)
    if fit is None or len(fit.curve) < 2:
        return 0.0
    curve = fit.curve
    hour = min(max(int(hours_in_position), 0), len(curve) - 2)
    survival = curve[hour]
    if survival <= 0.0:
        return 1.0
    return min(1.0, max(0.0, (survival - curve[hour + 1]) / survival))
