"""Trading view: paper ledger, live-gate state, and the validation warning.

The engine's risk scores are unvalidated research outputs (the latest report
ranks worse than its baselines), so every decision surface must say so before
an operator arms live trading.
"""

from __future__ import annotations

import streamlit as st

from ui.api_client import api_get


def trading_view() -> None:
    st.subheader("Trading")
    status = api_get("/trading/status") or {}

    if not status.get("scores_validated", False):
        st.error(
            "⚠️ **Scores are not validated.** "
            f"{status.get('scores_validation_reason', 'no validation report available')}"
        )
    else:
        st.info(f"Scores validated: {status.get('scores_validation_reason', '')}")

    col1, col2, col3 = st.columns(3)
    col1.metric("LIVE_TRADING_ENABLED", "on" if status.get("env_flag") else "off")
    col2.metric("Gate", "open" if status.get("gate_open") else "closed")
    col3.metric("Daily loss (SOL)", status.get("daily_loss_sol") or "0")

    st.caption(
        "Live orders require `LIVE_TRADING_ENABLED=1`, an explicit arm phrase, a "
        "bankroll cap, and a daily-loss cap. The kill switch cancels submitted "
        "orders and closes the gate. Copy signals are logged for human approval "
        "only. No profit is claimed or guaranteed."
    )

    ledger = api_get("/paper/ledger") or {}
    open_positions = ledger.get("open") or []
    closed = ledger.get("closed") or []
    col4, col5 = st.columns(2)
    col4.metric("Open paper positions", len(open_positions))
    col5.metric("Closed paper trades", len(closed))

    if open_positions:
        st.dataframe(open_positions, width="stretch", hide_index=True)
    else:
        st.info("No open paper positions.")
