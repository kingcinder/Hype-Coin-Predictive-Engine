from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HazardFit:
    mean_time_to_collapse: float | None
    event_count: int
    at_risk_count: int
    curve: tuple[float, ...] = field(default_factory=tuple)


class DiscreteHazardModel:
    """Hand-built empirical survival model for time-to-collapse.

    No external survival-analysis dependency: the model buckets the forward window
    into hourly bins, computes the empirical hazard per bin (events / at-risk), and
    derives the mean time-to-collapse from the survival curve. Censored observations
    (no collapse within the window) contribute to at-risk counts but not events.
    """

    def __init__(self, forward_hours: int) -> None:
        self.forward_hours = forward_hours

    def fit(self, times_hours: list[float], events: list[bool]) -> HazardFit:
        """``times_hours`` are hours from decision to collapse for events, and the
        forward window for censored (non-event) observations."""
        if len(times_hours) != len(events) or not times_hours:
            return HazardFit(None, 0, 0)
        event_times = [
            max(0.0, min(float(time), float(self.forward_hours)))
            for time, event in zip(times_hours, events, strict=True)
            if event
        ]
        if not event_times:
            return HazardFit(None, 0, len(times_hours))
        at_risk = float(len(times_hours))
        survival = 1.0
        curve: list[float] = [survival]
        for hour in range(1, self.forward_hours + 1):
            hazard = sum(1 for time in event_times if hour - 1 < time <= hour) / max(at_risk, 1.0)
            survival *= max(0.0, 1.0 - hazard)
            curve.append(round(survival, 6))
        # Expected time-to-event from the discrete survival curve.
        expected = sum(curve[1:]) + 0.5 * curve[0]
        return HazardFit(
            mean_time_to_collapse=round(expected, 3),
            event_count=len(event_times),
            at_risk_count=len(times_hours),
            curve=tuple(curve),
        )

    @staticmethod
    def expected_hours(fit: HazardFit, p_collapse: float) -> float | None:
        if fit.mean_time_to_collapse is None:
            return None
        return fit.mean_time_to_collapse if p_collapse >= 0.5 else None
