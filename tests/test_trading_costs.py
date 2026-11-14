from decimal import Decimal as D

from trading.costs import position_size_sol, simulate_buy, simulate_sell


def test_flat_price_round_trip_new_token_exact():
    b = simulate_buy(
        size_sol=D("1"),
        quote_price=D("1"),
        slip_pct=D("50"),
        fee_pct=D("1"),
        priority_sol=D("0.001"),
    )
    assert b.fee_sol == D("0.010000000")
    assert b.qty == D("0.659333333333")
    proceeds, s = simulate_sell(
        qty=b.qty, quote_price=D("1"), slip_pct=D("50"), fee_pct=D("1"), priority_sol=D("0.001")
    )
    assert s.fee_sol == D("0.003296667")
    assert proceeds == D("0.325370000")
    assert proceeds - D("1") == D("-0.674630000")


def test_size_one_percent_risk_twenty_percent_stop():
    assert position_size_sol(bankroll=D("10"), risk_pct=D("1"), stop_pct=D("20")) == D(
        "0.500000000"
    )


def test_size_capped_at_bankroll():
    assert position_size_sol(bankroll=D("1"), risk_pct=D("50"), stop_pct=D("1")) == D("1.000000000")


def test_bad_stop_rejected():
    import pytest

    for bad in (D("0"), D("100"), D("-5")):
        with pytest.raises(ValueError):
            position_size_sol(bankroll=D("10"), risk_pct=D("1"), stop_pct=bad)
