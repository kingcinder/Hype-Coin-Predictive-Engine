"""Re-stamp lifecycle_phase feature observed_at with evidence time.

The ``lifecycle_phase`` feature values are point-in-time correct —
verified: stored values equal the recomputation from lifecycle events
with ``ts <= decision`` AND ``observed_at <= decision`` on 123,389 /
123,389 non-missing rows (100%) — but their ``observed_at`` is the
backfill write time, after the decision time.  The validation harness's
point-in-time leak audit therefore flags the feature (concordance
0.9964, availability 0.0000) even though no value is leaked.

This migration re-stamps ``observed_at`` with the evidence time: the
``observed_at`` of the determining lifecycle event (the latest event
with ``ts <= decision`` AND ``observed_at <= decision`` — the same rule
``features/factory.py::_lifecycle_phase`` uses to compute the value).
Only rows whose stored value equals the recomputed phase rank are
re-stamped; a row whose value does not match the recomputation is left
untouched, so a genuinely wrong value is never laundered into clean
provenance.

Timestamps are stored uniformly as naive UTC ISO strings
(``YYYY-MM-DD HH:MM:SS.ffffff``), so the SQL string comparisons order
identically to parsed datetimes.

Data migration: not reversible — the original backfill write times are
not retained.  ``downgrade()`` is a documented no-op.

Revision ID: 0039_feature_observed_at_evidence_time
Revises: 0038_trading_timestamps
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_feature_observed_at_evidence_time"
down_revision = "0038_trading_timestamps"
branch_labels = None
depends_on = None

# Phase rank, mirroring pump_physics.engine.phase_rank: seeding 0,
# ignition 1, parabolic 2, saturation 3, collapse 4, everything else 5.
_PHASE_RANK_CASE = """CASE le.phase
    WHEN 'seeding' THEN 0
    WHEN 'ignition' THEN 1
    WHEN 'parabolic' THEN 2
    WHEN 'saturation' THEN 3
    WHEN 'collapse' THEN 4
    ELSE 5 END"""

# The determining lifecycle event for a feature row: latest by ts among
# events at or before the decision whose observed_at is also at or
# before the decision — the exact rule _lifecycle_phase uses.
_DETERMINING_EVENT = """SELECT le.observed_at
    FROM lifecycle_events le
    WHERE le.asset_id = features.asset_id
      AND le.ts <= features.decision_ts
      AND le.observed_at <= features.decision_ts
    ORDER BY le.ts DESC
    LIMIT 1"""

_DETERMINING_RANK = (
    "SELECT " + _PHASE_RANK_CASE + "\n"
    "    FROM lifecycle_events le\n"
    "    WHERE le.asset_id = features.asset_id\n"
    "      AND le.ts <= features.decision_ts\n"
    "      AND le.observed_at <= features.decision_ts\n"
    "    ORDER BY le.ts DESC\n"
    "    LIMIT 1"
)


def upgrade() -> None:
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            "UPDATE features\n"
            f"SET observed_at = ({_DETERMINING_EVENT})\n"
            "WHERE feature_name = 'lifecycle_phase'\n"
            "  AND missing_flag = 0\n"
            f"  AND ({_DETERMINING_RANK}) = feature_value"
        )
    )
    print(f"0039: re-stamped {result.rowcount} lifecycle_phase feature rows")


def downgrade() -> None:
    # Data migration: the original backfill write times are not retained,
    # so the re-stamp cannot be reversed.  No-op by design.
    pass
