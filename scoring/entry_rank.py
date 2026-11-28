"""Entry rank composite v1 (mission-refactor Task 5, spec §3).

The entry ranking is a rank-sum composite: each component is
percentile-ranked within the day cohort and the percentiles are summed.
The component set is the validated Ticket 1 trio
(``unique_buyers_estimate``, ``buy_sell_ratio``, ``liquidity_depth`` —
the same trio ``scripts/forward_validate_ranksum.py`` forward-validated)
plus ``net_inflow_ratio``, the reserve-growth feature whose inputs are
mechanically hard to fake, plus ``unique_buyers_acceleration`` (Task
14), the second difference of the buyer estimate. The acceleration
term is special: a wash-flagged row drops it entirely (percentile 0.0,
with the drop recorded in ``components``) — wash infrastructure must
not be able to buy an acceleration rank.

Before ranking, every row passes through :func:`wash_lite_adjust`: when
``volume_acceleration`` per unique buyer exceeds the configured
threshold, the row's ``buy_sell_ratio`` is pulled toward 1.0 and the row
is flagged. Same-transaction round-trip exclusion would need swap-level
data the lake does not persist per snapshot; the flag is the honest v1.

Social presence (Task 13, spec §3 item 3) enters as a *prior*, never a
component: :func:`social_presence_at_first_observation` derives
Telegram / X / website / github flags from the coin's first observation
only (point-in-time — later profile updates never rewrite them), and a
candidate with Telegram or website presence gets its finished rank-sum
multiplied by ``Settings.entry_social_prior_multiplier``. The
contingency falsification (``scripts/social_prior_falsification.py``)
guards the multiplier: at 1.0 (the demoted state) presence no longer
scales anything and only breaks exact rank-sum ties.

Percentile convention: midrank — ``(count_below + 0.5 * count_equal) /
cohort_size`` — so ties share their percentile and a single-row cohort
reads 0.5. A component missing from a row scores percentile 0.0:
absence is not neutral.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.config import Settings
from common.time import ensure_utc
from ingestion.normalizers import extract_profile_links
from storage import models

ENTRY_RANK_COMPONENTS: tuple[str, ...] = (
    "net_inflow_ratio",
    "buy_sell_ratio",
    "unique_buyers_estimate",
    "liquidity_depth",
    "unique_buyers_acceleration",
)


@dataclass(frozen=True)
class EntryCandidate:
    asset_id: int
    rank_sum: float
    components: dict[str, float]  # percentile rank per component, 0..1
    wash_flagged: bool


@dataclass(frozen=True)
class SocialPresence:
    website: bool
    github: bool
    telegram: bool
    x: bool


def social_presence_at_first_observation(session: Session, asset_id: int) -> SocialPresence:
    """Flags from the asset's FIRST observation only (point-in-time).

    When a DexScreener profile evidencing this asset exists at or
    before ``first_seen_at``, the earliest such profile decides all
    four flags: the stored ``website_url`` / ``github_url`` columns are
    *not* point-in-time (``upsert_asset`` fills them in when a later
    profile adds one), so they must not override what the first
    observation actually showed. Only when no profile evidence exists
    at or before first sight do the stored URLs stand in — Telegram / X
    then read absent, since nothing point-in-time ever recorded them.
    Later profile updates never rewrite the flags.
    """
    absent = SocialPresence(website=False, github=False, telegram=False, x=False)
    asset = session.get(models.Asset, asset_id)
    if asset is None:
        return absent
    first_seen = ensure_utc(asset.first_seen_at)
    chain = session.get(models.Chain, asset.chain_id)
    chain_slug = chain.slug if chain is not None else None
    evidence = session.scalars(
        select(models.RawEvidenceItem)
        .join(models.Source, models.Source.id == models.RawEvidenceItem.source_id)
        .where(
            models.Source.name == "dexscreener",
            models.RawEvidenceItem.observed_at <= first_seen,
        )
        .order_by(models.RawEvidenceItem.observed_at.asc(), models.RawEvidenceItem.id.asc())
    ).all()
    for row in evidence:
        profiles = (row.payload or {}).get("profiles") or []
        for item in profiles:
            if not isinstance(item, dict):
                continue
            if str(item.get("tokenAddress") or "") != asset.address:
                continue
            item_chain = str(item.get("chainId") or "").lower()
            if item_chain and chain_slug and item_chain != chain_slug:
                continue
            links = extract_profile_links(item)
            return SocialPresence(
                website=links.website_url is not None,
                github=links.github_url is not None,
                telegram=links.telegram_url is not None,
                x=links.x_url is not None,
            )
    return SocialPresence(
        website=asset.website_url is not None,
        github=asset.github_url is not None,
        telegram=False,
        x=False,
    )


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
    rows: list[tuple[int, dict[str, float]]],
    *,
    settings: Settings,
    presence: dict[int, SocialPresence] | None = None,
) -> list[EntryCandidate]:
    """Percentile-rank each component within the cohort, sum, sort desc.

    Missing component = percentile 0.0 (absence is not neutral). Rows
    are wash-adjusted first, so a flagged row's ``buy_sell_ratio``
    percentile is computed from its damped value — and a flagged row's
    ``unique_buyers_acceleration`` term is dropped outright (percentile
    0.0, drop recorded as ``unique_buyers_acceleration_wash_dropped``).

    Social-presence prior: when ``presence`` is supplied, a candidate
    with Telegram or website presence at first observation gets its
    finished rank_sum multiplied by
    ``settings.entry_social_prior_multiplier`` — after the rank-sum,
    before sorting; absent presence data means no multiplier. In the
    demoted state (multiplier == 1.0, the falsification's verdict) the
    multiplication is a no-op and presence instead breaks exact
    rank_sum ties, ahead of the asset_id tiebreak. All other ties
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
    prior_asset_ids: set[int] = set()
    for asset_id, features, wash_flagged in adjusted:
        components = {
            name: (
                _percentile_rank(features[name], cohort_values[name]) if name in features else 0.0
            )
            for name in ENTRY_RANK_COMPONENTS
        }
        if wash_flagged:
            # The acceleration term is dropped for wash-flagged rows, not
            # merely damped: percentile 0.0 whatever the raw value says.
            components["unique_buyers_acceleration"] = 0.0
        rank_sum = sum(components[name] for name in ENTRY_RANK_COMPONENTS)
        if wash_flagged:
            # Recorded beside the percentiles; the marker is not itself a
            # percentile and never enters rank_sum.
            components["unique_buyers_acceleration_wash_dropped"] = 1.0
        candidate_presence = presence.get(asset_id) if presence else None
        if candidate_presence is not None and (
            candidate_presence.telegram or candidate_presence.website
        ):
            # The prior scales the finished rank-sum; it is not a
            # component and never appears in ``components``.
            prior_asset_ids.add(asset_id)
            rank_sum *= settings.entry_social_prior_multiplier
        candidates.append(
            EntryCandidate(
                asset_id=asset_id,
                rank_sum=rank_sum,
                components=components,
                wash_flagged=wash_flagged,
            )
        )
    demoted = settings.entry_social_prior_multiplier == 1.0
    candidates.sort(
        key=lambda candidate: (
            -candidate.rank_sum,
            0 if (demoted and candidate.asset_id in prior_asset_ids) else 1,
            candidate.asset_id,
        )
    )
    return candidates
