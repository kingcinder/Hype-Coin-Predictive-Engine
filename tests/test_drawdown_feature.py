"""Ticket 1 step 3 (M4): drawdown_from_24h_peak feature + assess_risk rule.

Before this change the risk rules had no price-trajectory input at all:
five_min/one_hour returns fed only the hype momentum component, so a pure
price crater with intact reported liquidity accrued zero risk points.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from common.enums import RiskBand
from features.factory import compute_market_block
from risk_engine.rules import assess_risk

DECISION = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _market_row(ts: datetime, price: float) -> SimpleNamespace:
    return SimpleNamespace(
        ts=ts,
        observed_at=ts,
        source_id=1,
        price_usd=price,
        volume_usd=1_000.0,
        buys=10,
        sells=10,
        pair_id=7,
        pair_address="Pair111",
    )


def _drawdown(rows: list[SimpleNamespace]) -> object:
    values = compute_market_block(
        asset=SimpleNamespace(id=1),
        pairs=[],
        market_rows=rows,
        liquidity_rows=[],
        decision_ts=DECISION,
    )
    return values["drawdown_from_24h_peak"]


def test_drawdown_from_peak_within_24h() -> None:
    rows = [
        _market_row(DECISION - timedelta(hours=23), 100.0),
        _market_row(DECISION - timedelta(hours=12), 200.0),
        _market_row(DECISION - timedelta(hours=1), 120.0),
    ]
    feature = _drawdown(rows)
    assert feature.missing is False
    assert feature.value == 0.4  # (200 - 120) / 200


def test_drawdown_zero_when_latest_is_the_peak() -> None:
    rows = [
        _market_row(DECISION - timedelta(hours=5), 100.0),
        _market_row(DECISION - timedelta(hours=1), 150.0),
    ]
    feature = _drawdown(rows)
    assert feature.missing is False
    assert feature.value == 0.0


def test_drawdown_missing_when_no_rows_inside_24h_window() -> None:
    rows = [
        _market_row(DECISION - timedelta(hours=30), 100.0),
        _market_row(DECISION - timedelta(hours=26), 50.0),
    ]
    feature = _drawdown(rows)
    assert feature.missing is True


def _clean_features(**overrides: float) -> dict[str, float]:
    features = {
        "liquidity_depth": 1_000_000.0,
        "suspicious_contract_flags": 0.0,
        "top_holder_concentration": 0.0,
        "pair_age_minutes": 1_000.0,
        "spread_estimate": 0.0,
        "buy_sell_ratio": 1.0,
        "holder_count": 1_000.0,
        "volatility": 0.0,
        "liquidity_withdrawal_signal": 0.0,
        "lp_removal_signal": 0.0,
        "recidivism_score": 0.0,
        "collapse_probability_24h": 0.0,
        "lifecycle_phase": 1.0,
        "drawdown_from_24h_peak": 0.0,
    }
    features.update(overrides)
    return features


def test_drawdown_30pct_adds_10_points() -> None:
    result = assess_risk(_clean_features(drawdown_from_24h_peak=0.30))
    assert result.score == 10.0
    assert any("24h peak" in reason for reason in result.reasons)


def test_drawdown_55pct_adds_25_points_and_reaches_yellow() -> None:
    result = assess_risk(_clean_features(drawdown_from_24h_peak=0.55))
    assert result.score == 25.0
    assert result.band is RiskBand.YELLOW


def test_drawdown_10pct_adds_nothing() -> None:
    result = assess_risk(_clean_features(drawdown_from_24h_peak=0.10))
    assert result.score == 0.0
    assert result.band is RiskBand.GREEN


def test_drawdown_missing_is_tracked_as_missing_input() -> None:
    features = _clean_features()
    del features["drawdown_from_24h_peak"]
    result = assess_risk(features)
    assert "drawdown_from_24h_peak" in result.missing_features
