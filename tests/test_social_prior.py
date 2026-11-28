"""Social-presence prior tests (mission-refactor Task 13, spec §3 item 3).

Presence flags are what was true at the coin's FIRST observation —
Telegram / X / website links in the earliest DexScreener profile
evidence at or before ``first_seen_at`` — applied as a modest prior
multiplier on the entry rank-sum, never as a standalone signal. A link
that appears only in a later profile update must not set the flag:
later updates never rewrite first-observation presence. The
falsification instrument is a contingency table over historical
launches (Telegram-present vs not × pump label
``forward_max_gain_pct >= 1.0``); these tests exercise it on seeded
fixtures only — the live window-C run is a separate, recorded step.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from common.config import Settings, get_settings
from forecast.labels import LABEL_FORWARD_MAX_GAIN, upsert_label
from ingestion.normalizers import extract_profile_links
from scoring.entry_rank import (
    ENTRY_RANK_COMPONENTS,
    SocialPresence,
    rank_entry_cohort,
    social_presence_at_first_observation,
)
from scripts import social_prior_falsification
from storage.repository import store_raw_evidence, upsert_asset
from tests.conftest import seed_reference

T0 = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
DAY = timedelta(days=1)


# ---------------------------------------------------------------------------
# extract_profile_links
# ---------------------------------------------------------------------------


def test_extract_profile_links_captures_telegram_and_x():
    payload = {
        "links": [
            {"label": "telegram", "url": "https://t.me/example"},
            {"label": "twitter", "url": "https://x.com/example"},
            {"label": "website", "url": "https://example.com"},
        ]
    }
    links = extract_profile_links(payload)
    assert links.telegram_url == "https://t.me/example"
    assert links.x_url == "https://x.com/example"
    assert links.website_url == "https://example.com"


def test_extract_profile_links_keeps_website_and_github():
    payload = {
        "links": [
            {"label": "website", "url": "https://example.com"},
            {"label": "github", "url": "https://github.com/example/repo"},
        ]
    }
    links = extract_profile_links(payload)
    assert links.website_url == "https://example.com"
    assert links.github_url == "https://github.com/example/repo"
    assert links.telegram_url is None
    assert links.x_url is None


def test_extract_profile_links_empty_payload_all_none():
    links = extract_profile_links({})
    assert links.website_url is None
    assert links.github_url is None
    assert links.telegram_url is None
    assert links.x_url is None


# ---------------------------------------------------------------------------
# social_presence_at_first_observation
# ---------------------------------------------------------------------------


def _profile_item(address: str, links: list[dict[str, str]]) -> dict:
    return {"chainId": "solana", "tokenAddress": address, "links": links}


def _store_profiles(session, source, observed_at: datetime, items: list[dict]) -> None:
    store_raw_evidence(session, source=source, payload={"profiles": items}, observed_at=observed_at)
    session.flush()


def test_presence_flags_frozen_at_first_observation(session):
    # A Telegram link that appears only in a LATER profile update must
    # not set the flag: presence is what was true at first observation.
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="Frozen11111111111111111111111111111111111",
        symbol="FRZN",
        name="Frozen Fixture",
        first_seen_at=T0,
    )
    control = upsert_asset(
        session,
        chain_id=chain.id,
        address="Control1111111111111111111111111111111111",
        symbol="CTRL",
        name="Control Fixture",
        first_seen_at=T0,
    )
    session.flush()
    # First observation: neither asset shows a Telegram link; the control
    # shows a website.
    _store_profiles(
        session,
        source,
        T0,
        [
            _profile_item(asset.address, [{"label": "website", "url": "https://frzn.example"}]),
            _profile_item(
                control.address,
                [
                    {"label": "website", "url": "https://ctrl.example"},
                    {"label": "telegram", "url": "https://t.me/ctrl"},
                ],
            ),
        ],
    )
    # A day later a profile update adds Telegram to the frozen asset.
    _store_profiles(
        session,
        source,
        T0 + DAY,
        [
            _profile_item(
                asset.address,
                [
                    {"label": "website", "url": "https://frzn.example"},
                    {"label": "telegram", "url": "https://t.me/frzn"},
                ],
            )
        ],
    )
    session.commit()

    frozen = social_presence_at_first_observation(session, asset.id)
    assert frozen.telegram is False
    assert frozen.website is True  # present at first observation

    control_presence = social_presence_at_first_observation(session, control.id)
    assert control_presence.telegram is True
    assert control_presence.website is True


def test_presence_ignores_stored_url_written_by_later_update(session):
    # upsert_asset fills website_url in when a later profile adds one;
    # the stored column is therefore NOT point-in-time. When profile
    # evidence exists, the flags come from the earliest profile only.
    chain, source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="Late111111111111111111111111111111111111",
        symbol="LATE",
        name="Late Fixture",
        first_seen_at=T0,
        website_url="https://late.example",  # as a later update would leave it
    )
    session.flush()
    _store_profiles(
        session,
        source,
        T0,
        [_profile_item(asset.address, [{"label": "telegram", "url": "https://t.me/late"}])],
    )
    session.commit()

    presence = social_presence_at_first_observation(session, asset.id)
    assert presence.website is False  # not present at first observation
    assert presence.telegram is True


def test_presence_falls_back_to_stored_urls_without_profile_evidence(session):
    chain, _source = seed_reference(session)
    asset = upsert_asset(
        session,
        chain_id=chain.id,
        address="Stored11111111111111111111111111111111111",
        symbol="STOR",
        name="Stored Fixture",
        first_seen_at=T0,
        website_url="https://stored.example",
        github_url="https://github.com/example/stored",
    )
    session.commit()

    presence = social_presence_at_first_observation(session, asset.id)
    assert presence.website is True
    assert presence.github is True
    assert presence.telegram is False
    assert presence.x is False


def test_presence_unknown_asset_all_false(session):
    presence = social_presence_at_first_observation(session, 999_999)
    assert presence == SocialPresence(website=False, github=False, telegram=False, x=False)


# ---------------------------------------------------------------------------
# prior multiplier on rank_entry_cohort
# ---------------------------------------------------------------------------


def _full_row(base: float) -> dict[str, float]:
    row = {name: base for name in ENTRY_RANK_COMPONENTS}
    row["volume_acceleration"] = 5.0
    return row


def test_prior_multiplier_applies_after_rank_sum():
    # Two otherwise-identical candidates; the Telegram-present one is
    # multiplied by entry_social_prior_multiplier and outranks.
    rows = [(1, _full_row(10.0)), (2, _full_row(10.0))]
    presence = {2: SocialPresence(website=False, github=False, telegram=True, x=False)}
    candidates = rank_entry_cohort(rows, settings=get_settings(), presence=presence)
    assert [candidate.asset_id for candidate in candidates] == [2, 1]
    by_id = {candidate.asset_id: candidate for candidate in candidates}
    # Identical rows: every component midrank is 0.5, rank_sum 2.5.
    assert by_id[1].rank_sum == pytest.approx(2.5)
    assert by_id[2].rank_sum == pytest.approx(2.5 * 1.25)


def test_prior_website_presence_also_multiplies():
    rows = [(1, _full_row(10.0)), (2, _full_row(10.0))]
    presence = {2: SocialPresence(website=True, github=False, telegram=False, x=False)}
    candidates = rank_entry_cohort(rows, settings=get_settings(), presence=presence)
    by_id = {candidate.asset_id: candidate for candidate in candidates}
    assert by_id[2].rank_sum == pytest.approx(2.5 * 1.25)


def test_prior_absent_presence_data_no_multiplier():
    rows = [(1, _full_row(10.0)), (2, _full_row(10.0))]
    candidates = rank_entry_cohort(rows, settings=get_settings(), presence={})
    by_id = {candidate.asset_id: candidate for candidate in candidates}
    assert by_id[1].rank_sum == pytest.approx(2.5)
    assert by_id[2].rank_sum == pytest.approx(2.5)
    # No presence data at all: ordering is the plain asset_id tiebreak.
    assert [candidate.asset_id for candidate in candidates] == [1, 2]


def test_prior_demoted_to_tiebreaker_when_multiplier_is_one():
    # Demoted state (plan's demotion rule): multiplier 1.0 — presence no
    # longer scales rank_sum, it only breaks exact ties.
    settings = Settings(_env_file=None, entry_social_prior_multiplier=1.0)
    rows = [(1, _full_row(10.0)), (2, _full_row(10.0))]
    presence = {2: SocialPresence(website=False, github=False, telegram=True, x=False)}
    candidates = rank_entry_cohort(rows, settings=settings, presence=presence)
    by_id = {candidate.asset_id: candidate for candidate in candidates}
    assert by_id[1].rank_sum == pytest.approx(2.5)
    assert by_id[2].rank_sum == pytest.approx(2.5)
    assert [candidate.asset_id for candidate in candidates] == [2, 1]


# ---------------------------------------------------------------------------
# falsification script (fixtures)
# ---------------------------------------------------------------------------


def _seed_launch(
    session,
    chain_id: int,
    *,
    address: str,
    first_seen_at: datetime,
    max_gain: float,
) -> int:
    asset = upsert_asset(
        session,
        chain_id=chain_id,
        address=address,
        symbol=address[:4].upper(),
        name=f"{address[:4]} Fixture",
        first_seen_at=first_seen_at,
    )
    session.flush()
    decision = first_seen_at + timedelta(hours=1)
    upsert_label(
        session,
        asset_id=asset.id,
        ts=decision,
        label_type=LABEL_FORWARD_MAX_GAIN,
        label_value=f"{max_gain:.6f}",
        decision_ts=decision,
        source="forecast:test",
    )
    return asset.id


def _seed_cohort(
    session, *, telegram_pumps: int, telegram_total: int, plain_pumps: int, plain_total: int
):
    chain, source = seed_reference(session)
    telegram_items = []
    plain_items = []
    for i in range(telegram_total):
        address = f"Tg{i:036d}"
        _seed_launch(
            session,
            chain.id,
            address=address,
            first_seen_at=T0,
            max_gain=1.5 if i < telegram_pumps else 0.1,
        )
        telegram_items.append(
            _profile_item(address, [{"label": "telegram", "url": f"https://t.me/tg{i}"}])
        )
    for i in range(plain_total):
        address = f"Pl{i:036d}"
        _seed_launch(
            session,
            chain.id,
            address=address,
            first_seen_at=T0,
            max_gain=1.5 if i < plain_pumps else 0.1,
        )
        plain_items.append(_profile_item(address, []))
    _store_profiles(session, source, T0, telegram_items)
    _store_profiles(session, source, T0, plain_items)
    session.commit()


def test_falsification_lift_passes_and_writes_report(session, tmp_path):
    # P(pump | telegram) = 20/40 = 0.50; P(pump | none) = 5/40 = 0.125;
    # lift 4.0 >= 2.0 with n(telegram) = 40 >= 30 -> exit 0.
    _seed_cohort(session, telegram_pumps=20, telegram_total=40, plain_pumps=5, plain_total=40)
    exit_code = social_prior_falsification.run_falsification(
        session=session,
        settings=get_settings(),
        as_of=T0 + DAY,
        reports_dir=tmp_path,
    )
    assert exit_code == 0
    json_path = tmp_path / f"social-prior-falsification-{(T0 + DAY).strftime('%Y%m%d')}.json"
    md_path = json_path.with_suffix(".md")
    assert json_path.exists()
    assert md_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["telegram_present"] == 40
    assert payload["telegram_pumped"] == 20
    assert payload["no_telegram"] == 40
    assert payload["no_telegram_pumped"] == 5
    assert payload["lift"] == pytest.approx(4.0)


def test_falsification_low_lift_exits_1_and_writes_report(session, tmp_path):
    # Equal pump rates: lift 1.0 < 2.0 -> exit 1 (demotion signal), and
    # the report is still written.
    _seed_cohort(session, telegram_pumps=10, telegram_total=40, plain_pumps=10, plain_total=40)
    exit_code = social_prior_falsification.run_falsification(
        session=session,
        settings=get_settings(),
        as_of=T0 + DAY,
        reports_dir=tmp_path,
    )
    assert exit_code == 1
    json_path = tmp_path / f"social-prior-falsification-{(T0 + DAY).strftime('%Y%m%d')}.json"
    assert json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["lift"] == pytest.approx(1.0)


def test_falsification_small_telegram_sample_exits_1(session, tmp_path):
    # Lift is huge but n(telegram) = 10 < 30: the gate cannot pass on a
    # thin sample, however good it looks.
    _seed_cohort(session, telegram_pumps=10, telegram_total=10, plain_pumps=0, plain_total=40)
    exit_code = social_prior_falsification.run_falsification(
        session=session,
        settings=get_settings(),
        as_of=T0 + DAY,
        reports_dir=tmp_path,
    )
    assert exit_code == 1
    payload = json.loads(
        (tmp_path / f"social-prior-falsification-{(T0 + DAY).strftime('%Y%m%d')}.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["passed"] is False
    assert payload["telegram_present"] == 10
