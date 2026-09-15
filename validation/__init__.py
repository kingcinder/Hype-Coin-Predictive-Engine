"""Phase 9 validation/benchmark harness.

Observational and measurement only: this package reads the engine's persisted
point-in-time rows and emits versioned reports. It never writes to engine
tables and never couples its own correctness to the code it measures (all
metric functions are pure and numpy-only).
"""

from validation.leakage_guard import (
    LeakageViolation,
    check_rows_point_in_time,
    current_decision_ts,
    frozen_decision_ts,
    record_leakage_violation,
    require_decision_ts,
)

__all__ = [
    "LeakageViolation",
    "check_rows_point_in_time",
    "current_decision_ts",
    "frozen_decision_ts",
    "record_leakage_violation",
    "require_decision_ts",
]
