"""Phase 9 validation/benchmark harness.

Observational and measurement only: this package reads the engine's persisted
point-in-time rows and emits versioned reports. It never writes to engine
tables and never couples its own correctness to the code it measures (all
metric functions are pure and numpy-only).
"""
