"""Entry rank composite v1 (mission-refactor Task 5, spec §3).

The entry ranking is a rank-sum composite: each component is
percentile-ranked within the day cohort and the percentiles are summed.
The component set is the validated Ticket 1 trio
(``unique_buyers_estimate``, ``buy_sell_ratio``, ``liquidity_depth`` —
the same trio ``scripts/forward_validate_ranksum.py`` forward-validated)
plus ``net_inflow_ratio``, the reserve-growth feature whose inputs are
mechanically hard to fake.

Before ranking, every row passes through :func:`wash_lite_adjust`: when
``volume_acceleration`` per unique buyer exceeds the configured
threshold, the row's ``buy_sell_ratio`` is pulled toward 1.0 and the row
is flagged. Same-transaction round-trip exclusion would need swap-level
data the lake does not persist per snapshot; the flag is the honest v1.

Percentile convention: midrank — ``(count_below + 0.5 * count_equal) /
cohort_size`` — so ties share their percentile and a single-row cohort
reads 0.5. A component missing from a row scores percentile 0.0:
absence is not neutral.
"""

from __future__ import annotations

from dataclasses import dataclass

from common.config import Settings

ENTRY_RANK_COMPONENTS: tuple[str, ...] = (
    "net_inflow_ratio",
    "buy_sell_ratio",
    "unique_buyers_estimate",
    "liquidity_depth",
)


@dataclass(frozen=True)
class EntryCandidate:
    asset_id: int
    rank_sum: float
    components: dict[str, float]  # percentile rank per component, 0..1
    wash_flagged: bool


def wash_lite_adjust(
    features: dict[str, float], *, settings: Settings
) -> tuple[dict[str, float], bool]:
    """Down-weight buy_sell_ratio when volume-per-buyer screams wash.

    Inputs are existing FEATURE_NAMES only: intensity =
    volume_acceleration / max(unique_buyers_estimate, 1.0); when
    intensity > settings.entry_wash_volume_per_buyer, replace
    buy_sell_ratio with 1.0 + (buy_sell_ratio - 1.0) *
    settings.entry_wash_downweight and flag. (Same-tx round-trip
    exclusion needs swap-level data the lake does not persist
    per-snapshot; the flag is the honest v1.)

    Returns a copy — the caller's dict is never mutated. When the
    intensity inputs are absent the row cannot be judged and is
    returned unflagged.
    """
    adjusted = dict(features)
    volume = features.get("volume_acceleration")
    buyers = features.get("unique_buyers_estimate")
    if volume is None or buyers is None:
        return adjusted, False
    intensity = volume / max(buyers, 1.0)
    if intensity <= settings.entry_wash_volume_per_buyer:
        return adjusted, False
    ratio = adjusted.get("buy_sell_ratio")
    if ratio is not None:
        adjusted["buy_sell_ratio"] = 1.0 + (ratio - 1.0) * settings.entry_wash_downweight
    return adjusted, True


def _percentile_rank(value: float, cohort_values: list[float]) -> float:
    """Midrank percentile of ``value`` within ``cohort_values`` (0..1)."""
    below = sum(1 for other in cohort_values if other < value)
    equal = sum(1 for other in cohort_values if other == value)
    return (below + 0.5 * equal) / len(cohort_values)


def rank_entry_cohort(
    rows: list[tuple[int, dict[str, float]]], *, settings: Settings
) -> list[EntryCandidate]:
    """Percentile-rank each component within the cohort, sum, sort desc.

    Missing component = percentile 0.0 (absence is not neutral). Rows
    are wash-adjusted first, so a flagged row's ``buy_sell_ratio``
    percentile is computed from its damped value. Ties in rank_sum
    break by ascending asset_id so the ordering is deterministic.
    """
    adjusted = [
        (asset_id, *wash_lite_adjust(features, settings=settings)) for asset_id, features in rows
    ]
    cohort_values: dict[str, list[float]] = {
        name: [features[name] for _id, features, _flag in adjusted if name in features]
        for name in ENTRY_RANK_COMPONENTS
    }
    candidates: list[EntryCandidate] = []
    for asset_id, features, wash_flagged in adjusted:
        components = {
            name: (
                _percentile_rank(features[name], cohort_values[name]) if name in features else 0.0
            )
            for name in ENTRY_RANK_COMPONENTS
        }
        candidates.append(
            EntryCandidate(
                asset_id=asset_id,
                rank_sum=sum(components.values()),
                components=components,
                wash_flagged=wash_flagged,
            )
        )
    candidates.sort(key=lambda candidate: (-candidate.rank_sum, candidate.asset_id))
    return candidates
